"""Import the shows of a public Douban account into Yamtrack.

The second phase of the Douban import. Films are handled by
``integrations.imports.douban_movie``, which walks the same collection; this
module walks it too and imports only what that one skips.

The collection is read from one endpoint:

``GET /rexxar/api/v2/user/<uid>/interests?type=movie&status=done``
    ``total`` says 647 for this account and the real page size is 50 no matter
    what ``count`` asks for, so 13 pages of 50 are walked. They hold **646**
    entries, 476 films and 170 shows; one entry is never sent by the server,
    which is a gap in Douban's own offset paging and not a missed page.

    ``type=movie`` is the whole collection, films and shows together. ``type=tv``
    is not honoured. What separates them is ``subject.subtype``: ``tv`` is a
    show, and this importer takes those and leaves the films to the film import.

One season per subject
----------------------
Douban gives every season its own subject, so "Game of Thrones Season 7" and
"Season 1" are two ids with two titles. 85 of the 170 shows carry an explicit
marker in the title ("第二季", "Season 2", "第三部"), and the rest carry none, so
the same show can appear twice with no way to tell from the title.

So one subject becomes one tracked show, and the title is kept exactly as Douban
wrote it. Merging the subjects of a show back together would mean guessing the
season number out of the title, and both ways that goes wrong are irreversible:
two shows glued into one cannot be pulled apart again by re-running the import,
and a season filed under the wrong number is a wrong watch history that looks
right. A viewer can read "海贼王(真人版) 第二季" as its own entry; a viewer cannot
tell that two of them were quietly merged.

What the collection cannot say
------------------------------
The list entry carries no episode count (checked: the key is absent from all 170
entries), so every show costs one detail call, which is also where the episode
count, the episode length, the first air date and the synopsis come from.

``seasons_count`` does not exist either: all 77 keys were listed on three shows
and there is no season count among them. A Douban subject holds exactly one
season, so the app's three levels become one TV row, one Season row (season 1)
and the episodes of that season.

Why the season and its episodes are written too
-----------------------------------------------
On this build ``TV.progress``, ``TV.start_date`` and ``TV.end_date`` are not
database columns: they are read-only properties summed from the season's
episodes (see ``app.models.TV`` and ``app.models.Season``). Writing only the TV
row would produce a show that is marked completed but shows a progress of zero
and no end date, because there is nothing to sum.

So the watch date lands on the episodes, one per episode of the season, and the
season row is what ``TV.end_date`` reads. That is also the reason the date is not
stored anywhere else: a second copy would be a second truth.

Two fields both called ``rating``, with nothing in common:

- ``interest.rating.value``, 1-5: the user's own star rating. Kept as a note.
- ``subject.rating.value``, 0-10: the community average. This is the score.

A show that fails on its own is recorded and the batch carries on. What was
already done is dumped to ``_tmp/douban_tv_progress.json`` after every show, so a
run that stops halfway resumes instead of starting over.
"""

import json
import logging
import zoneinfo
from collections import defaultdict
from pathlib import Path

import requests
from django.apps import apps
from django.conf import settings
from django.utils import timezone

from app.models import Item, MediaTypes, Sources, Status
from app.providers import douban_movie, douban_tv, services
from app.providers.douban_movie import BLOCKED_STATUS
from integrations.imports import douban_ip_switch, helpers
from integrations.imports.douban_ip_switch import DoubanStillLimitedError
from integrations.imports.helpers import (  # noqa: F401
    MediaImportError,
    MediaImportNetworkError,
    MediaImportUnexpectedError,
)

logger = logging.getLogger(__name__)

INTERESTS_URL = "https://m.douban.com/rexxar/api/v2/user/{uid}/interests"

# The whole collection, whatever ``type`` is asked for: this importer picks the
# shows out of it by ``subject.subtype`` and drops the films.
INTERESTS_TYPE = "movie"
INTERESTS_STATUS = "done"

# Douban hands back 50 entries per page even when ``count`` asks for more
PAGE_SIZE = 50
# Ceiling for the page walk. The collection is 13 pages; this only stops a
# server that keeps answering from spinning forever.
MAX_PAGES = 200

SUBTYPE_MOVIE = "movie"
SUBTYPE_TV = "tv"

# A Douban subject is one season, so the season it becomes is season one.
SEASON_NUMBER = douban_tv.DEFAULT_SEASON

STARS_MAX = 5
STAR_FULL = "★"
STAR_EMPTY = "☆"

# ``interest.create_time``, the moment the show was marked as watched, in Beijing
# time with no zone attached.
WATCHED_AT_FORMAT = "%Y-%m-%d %H:%M:%S"
WATCHED_TIME_ZONE = "Asia/Shanghai"

PROGRESS_FILE = "_tmp/douban_tv_progress.json"
IMPORT_NOTE = "Imported from Douban"
RATING_NOTE_PREFIX = "我的豆瓣评分"


def importer(uid, user, mode, limit=None):
    """Import the shows of a Douban account for one user.

    ``limit`` stops after that many shows, which is how the first run is checked
    on a handful before the whole collection is asked for.
    """
    return DoubanTVImporter(uid, user, mode, limit=limit).import_data()


def watched_at(interest):
    """Return when a show was marked as watched, or None.

    ``create_time`` is Beijing time without a zone, so it is read as such and
    handed over as an aware datetime: ``USE_TZ`` is on, and a naive value would be
    taken as UTC and shown hours off.
    """
    raw = (interest.get("create_time") or "").strip()
    if not raw:
        return None

    try:
        naive = timezone.datetime.strptime(raw, WATCHED_AT_FORMAT)
    except (TypeError, ValueError):
        logger.warning("Douban sent an unreadable watch time: %r", raw)
        return None

    return naive.replace(tzinfo=zoneinfo.ZoneInfo(WATCHED_TIME_ZONE))


def stars_text(value):
    """Return ``★★★★☆`` for a star rating out of five, or None."""
    try:
        # ``round`` of a single argument already gives an int on Python 3
        stars = round(float(value))
    except (TypeError, ValueError):
        return None

    if stars <= 0 or stars > STARS_MAX:
        return None

    return STAR_FULL * stars + STAR_EMPTY * (STARS_MAX - stars)


def rating_note(interest):
    """Return the one line note holding the user's own star rating, or None.

    This is ``interest.rating``, the caller's 1-5 stars, a different field with a
    different meaning than ``subject.rating``, the 0-10 community average that is
    stored as the score.
    """
    rating = interest.get("rating") or {}
    value = rating.get("value")
    text = stars_text(value)

    if not text:
        return None

    return f"{RATING_NOTE_PREFIX}：{text}（{round(float(value))} 星）"


class DoubanTVImporter:
    """Import the shows a Douban account has marked as watched."""

    def __init__(self, uid, user, mode, limit=None):
        """Initialize the importer with the Douban uid, user, mode and limit."""
        self.uid = str(uid).strip()
        self.user = user
        self.mode = mode
        self.limit = limit
        self.warnings = []

        self.existing_media = helpers.get_existing_media(user)
        self.existing_seasons = helpers.get_existing_seasons(user)
        # Existing media changed in "new" mode, by primary key
        self.to_update = defaultdict(dict)
        # Media ids to delete in overwrite mode
        self.to_delete = defaultdict(lambda: defaultdict(set))
        # Bulk creation lists for each media type
        self.bulk_media = defaultdict(list)

        self._progress_path = Path(settings.BASE_DIR).parent / PROGRESS_FILE

        self.done = []
        self.failed = []
        self._previous_failures = self._load_failed()
        self.skipped_movies = 0
        self._remaining = 0

        logger.info(
            "Initialized Douban TV importer for user %s (douban uid %s) with mode %s,"
            " limit %s",
            user.username,
            uid,
            mode,
            limit,
        )

    # ---- collection ----------------------------------------------------

    def import_data(self):
        """Import every show of the collection, films counted and skipped."""
        try:
            interests = self._fetch_interests()
        except services.ProviderAPIError as error:
            # Only what the user can act on is reported instead of crashing the
            # task: too many requests from this address and a hard block. Any
            # other error (no network, a proxy that will not connect, a timeout,
            # a 5xx) imported nothing and is not the user's to fix, so it is
            # raised as a network error: the task ends FAILED and Celery tries
            # again an hour later.
            if douban_tv.is_rate_limited_error(error):
                msg = douban_movie.RATE_LIMIT_MESSAGE
            elif getattr(error, "status_code", None) == BLOCKED_STATUS:
                msg = douban_movie.BLOCKED_MESSAGE
            else:
                raise MediaImportNetworkError(
                    douban_movie.network_error_message(error, "剧集"),
                ) from error
            logger.warning("Douban TV import could not start: %s", msg)
            return {MediaTypes.TV.value: 0}, msg

        logger.info("Douban collection holds %s interests", len(interests))
        # Counted over the whole collection rather than over what this run gets
        # to, so a test run of ten shows still reports the real film count
        self.skipped_movies = sum(
            1
            for interest in interests
            if (interest.get("subject") or {}).get("subtype") == SUBTYPE_MOVIE
        )

        # ``imported`` has to exist before the try: when Douban blocks the run,
        # ``_import_interests`` raises and the handler still reports a count.
        imported = 0
        # A fresh run starts with a clean skip counter, so a batch that was cut
        # off yesterday does not stop after one skipped show today
        douban_ip_switch.reset()
        try:
            imported = self._import_interests(interests)
        except services.ProviderAPIError as error:
            if not douban_tv.is_blocked(error):
                raise
            self._flush()
            self._dump_progress()
            msg = (
                f"豆瓣在导入 {len(self.done)} 部剧集之后开始限流，剩下的 "
                f"{self._remaining} 部没导。已经导入的不会丢，"
                "过一两个小时再跑一次就会接着往下走。"
            )
            logger.warning(msg)
            self._report()
            deduplicated = "\n".join(dict.fromkeys(str(w) for w in self.warnings))
            return {MediaTypes.TV.value: imported}, "\n".join(
                [deduplicated, msg] if deduplicated else [msg],
            )

        self._flush()
        self._report()

        deduplicated = "\n".join(dict.fromkeys(str(w) for w in self.warnings))
        return {MediaTypes.TV.value: imported}, deduplicated

    def _fetch_interests(self):
        """Return every entry of the collection, walking the pages.

        The walk cannot stop on a short page: Douban hands back 49 entries for
        ``start=50`` and a full 50 again after it. What ends it is an empty page,
        or having stepped past ``total``.
        """
        interests = []
        seen = set()
        start = 0
        total = None

        for page in range(1, MAX_PAGES + 1):
            payload = self._fetch_page(start)
            entries = payload.get("interests") or []
            total = payload.get("total") or total

            if not entries:
                break

            for entry in entries:
                subject = entry.get("subject") or {}
                media_id = str(subject.get("id") or "")
                if not media_id or media_id in seen:
                    continue
                seen.add(media_id)
                interests.append(entry)

            logger.info(
                "Douban collection page %s (start=%s): %s entries, %s collected of %s",
                page,
                start,
                len(entries),
                len(interests),
                total,
            )

            start += PAGE_SIZE
            if total and start >= int(total):
                break

        return interests

    def _fetch_page(self, start):
        """Return one page of the collection."""
        url = INTERESTS_URL.format(uid=self.uid)
        params = {
            "type": INTERESTS_TYPE,
            "status": INTERESTS_STATUS,
            "count": PAGE_SIZE,
            "start": start,
        }

        try:
            response = douban_movie.get(url, params=params)
            response.raise_for_status()
        except requests.RequestException as error:
            douban_movie.handle_error(error)

        try:
            return response.json()
        except ValueError as error:
            raise services.ProviderAPIError(Sources.DOUBAN.value, error) from error

    # ---- one entry -----------------------------------------------------

    def _import_interests(self, interests):
        """Import the shows of the collection, return how many were new.

        A show that is cut off by Douban's IP rate limit is not broken, so the
        importer waits and asks again, and only ends the batch when the wait has
        grown long enough to say the limit is really in place. Everything gathered
        so far is written before that happens.

        ``_settled`` is what makes a re-run cheap and correct at the same time: a
        show whose row is in the library with its episodes needs nothing and costs
        no request, while a show that failed, or that was imported before the
        episodes were written, is looked at again.
        """
        done_before = self._load_done()
        if done_before:
            logger.info(
                "Resuming the Douban TV import, %s shows were written before",
                len(done_before),
            )

        imported = 0
        processed = 0
        total_shows = sum(
            1
            for interest in interests
            if (interest.get("subject") or {}).get("subtype") == SUBTYPE_TV
        )
        self._remaining = total_shows

        for interest in interests:
            subject = interest.get("subject") or {}
            media_id = str(subject.get("id") or "")

            if subject.get("subtype") == SUBTYPE_MOVIE:
                # A film, left to the film importer instead of being half
                # imported here as a show
                continue

            if subject.get("subtype") != SUBTYPE_TV:
                self.warnings.append(
                    f"{subject.get('title') or media_id}: 无法判断是电影还是剧集，已跳过。",
                )
                continue

            if self.limit is not None and processed >= self.limit:
                logger.info("Stopping at the limit of %s shows", self.limit)
                break

            if not media_id:
                self.warnings.append("一条收藏没有条目 id，已跳过。")
                continue

            processed += 1
            self._remaining = max(total_shows - processed, 0)

            watched = watched_at(interest)

            if self._settled(media_id, watched):
                logger.debug("Douban show %s needs nothing more", media_id)
                continue

            try:
                if self._process_show(interest, media_id, watched):
                    imported += 1
            except DoubanStillLimitedError:
                # Limited on every exit IP: skipped, so the batch carries on.
                # It is not marked done, so the next run tries it again.
                self._skip_rate_limited(subject, media_id)
                continue
            except services.ProviderAPIError as error:
                if douban_tv.is_blocked(error):
                    # Nothing to do with this show: the whole network is being
                    # throttled, so the same error would repeat for every one
                    # left. Keep it out of ``failed`` and let the next run pick
                    # it up.
                    raise
                logger.warning(
                    "Error processing Douban show %s - %s",
                    subject.get("title"),
                    error,
                )
                self.failed.append({"id": media_id, "reason": str(error)[:200]})
                self._dump_progress()
                continue
            except Exception as error:  # noqa: BLE001
                # One odd show must not end the whole batch. It is not added to
                # ``done`` either, so the next run tries it again.
                logger.warning("Douban show %s failed: %s", media_id, error)
                self.failed.append({"id": media_id, "reason": str(error)[:200]})
                self._dump_progress()
                continue

            self.done.append(media_id)
            self._dump_progress()
            self._flush()

        return imported

    def _skip_rate_limited(self, subject, media_id):
        """Record a show that was limited on every exit IP and carry on.

        It is kept out of ``done`` so the next run tries it again, and its id
        goes into ``failed`` so it still shows up in the report.
        """
        self.warnings.append(
            f"{subject.get('title') or media_id}: 换了 "
            f"{douban_ip_switch.MAX_IP_RETRIES} 个 IP 仍被限流，先跳过。",
        )
        self.failed.append({"id": media_id, "reason": "rate limited on every exit IP"})
        self._dump_progress()

    def _is_tracked(self, media_id):
        """Return whether the user already has this show from Douban."""
        tracked = self.existing_media[MediaTypes.TV.value][Sources.DOUBAN.value]
        return media_id in tracked

    def _season(self, media_id):
        """Return the season row already stored for a show, or None."""
        return self.existing_seasons.get(
            (Sources.DOUBAN.value, str(media_id), SEASON_NUMBER),
        )

    def _settled(self, media_id, watched):
        """Return whether a show a previous run handled needs nothing more.

        A show that was imported before its episodes were written is in the
        library but has no season, which means no end date and a progress of zero.
        Those are looked at again so the watch date lands, which is also how an
        entry from an earlier run is completed: one more ``mode=new`` pass, with
        no request to Douban unless something is actually missing.
        """
        if not self._is_tracked(media_id):
            return False

        season = self._season(media_id)
        if season is None:
            return False

        if not season.episodes.exists():
            return False

        if watched is None:
            # Douban gave no date to begin with, nothing to backfill
            return True

        return season.end_date is not None

    def _process_show(self, interest, media_id, watched):
        """Store one show, its season and its episodes. Return whether it was new."""
        existing_tv = self._existing_tv(media_id)
        note = rating_note(interest)

        if existing_tv and self.mode == "new":
            # The show is already tracked. Only what Douban knows is filled in,
            # and ``sync_existing_media`` leaves anything the user typed alone.
            if self._sync_existing(existing_tv, note):
                self.to_update[MediaTypes.TV.value][existing_tv.pk] = existing_tv
            else:
                logger.debug("Existing Douban show %s is up to date", media_id)
            # The details page reads the provider cache, so it is filled even
            # when nothing is written
            self._cache_metadata(media_id)
            self._process_season_and_episodes(media_id, existing_tv, watched)
            return False

        if existing_tv and self.mode == "overwrite":
            self.to_delete[MediaTypes.TV.value][Sources.DOUBAN.value].add(media_id)

        metadata = self._metadata(media_id)
        tv_item = self._store_tv_item(metadata)

        tv_instance = apps.get_model(app_label="app", model_name=MediaTypes.TV.value)(
            item=tv_item,
            user=self.user,
            score=metadata["score"],
            status=Status.COMPLETED.value,
            notes=self._notes(note),
        )
        # The history row of an imported show is stamped with its watch date, not
        # the moment of the import, so the timeline reads correctly
        tv_instance._history_date = watched or timezone.now()
        self.bulk_media[MediaTypes.TV.value].append(tv_instance)

        self._process_season_and_episodes(media_id, tv_instance, watched)
        return True

    def _process_season_and_episodes(self, media_id, tv_instance, watched):
        """Store the one season of a show and the episodes that were watched.

        A Douban subject is one season, and an entry in this collection means the
        whole season was watched, so every episode of it is written with the watch
        date. That is not decoration: ``Season.progress`` counts the episodes that
        exist, ``Season.end_date`` is the latest of their dates, and
        ``TV.end_date`` / ``TV.progress`` are summed from the seasons. Without the
        episode rows the show would show "no progress" and no end date even though
        it was imported as completed.
        """
        metadata = self._metadata(media_id)
        count = metadata["max_progress"]

        season_instance = self._season(media_id)

        if season_instance is None:
            season_item = self._season_item(media_id, metadata)
            season_instance = apps.get_model(
                app_label="app",
                model_name=MediaTypes.SEASON.value,
            )(
                item=season_item,
                user=self.user,
                related_tv=tv_instance,
                score=metadata["score"],
                status=Status.COMPLETED.value,
            )
            season_instance._history_date = watched or timezone.now()
            self.bulk_media[MediaTypes.SEASON.value].append(season_instance)
            # Remembered for the rest of this run, so the second pass over the
            # same id does not try to create the season twice
            self.existing_seasons[
                (Sources.DOUBAN.value, str(media_id), SEASON_NUMBER)
            ] = season_instance
        else:
            season_item = season_instance.item

        self._add_episodes(
            media_id,
            season_instance,
            season_item,
            count,
            watched,
            metadata,
        )

    def _season_item(self, media_id, metadata):
        """Create or update the item of the season of a show and return it."""
        item, _ = Item.objects.update_or_create(
            media_id=str(media_id),
            source=Sources.DOUBAN.value,
            media_type=MediaTypes.SEASON.value,
            season_number=SEASON_NUMBER,
            defaults={
                "title": metadata["title"],
                "image": metadata["image"] or settings.IMG_NONE,
                "synopsis": metadata["synopsis"],
                "genres": metadata.get("genres") or [],
                "directors": metadata.get("directors") or [],
                "cast": metadata.get("cast") or [],
                "release_date": self._release_date(metadata),
            },
        )
        return item

    def _add_episodes(
        self,
        media_id,
        season_instance,
        season_item,
        count,
        watched,
        metadata,
    ):
        """Store the episodes of the season that are not stored yet.

        Only missing episodes are added, so re-running the import never moves a
        watch date the user set by hand. ``Episode`` rows are named by episode
        number and the number is what ``Season.progress`` reads, so the range is
        one to the episode count Douban reports.
        """
        existing_numbers = set(
            Item.objects.filter(
                media_id=str(media_id),
                source=Sources.DOUBAN.value,
                media_type=MediaTypes.EPISODE.value,
                season_number=SEASON_NUMBER,
            ).values_list("episode_number", flat=True),
        )

        missing = [
            number for number in range(1, count + 1) if number not in existing_numbers
        ]
        if not missing:
            return

        episode_model = apps.get_model(
            app_label="app",
            model_name=MediaTypes.EPISODE.value,
        )

        new_items = []
        for number in missing:
            episode_item, _ = Item.objects.get_or_create(
                media_id=str(media_id),
                source=Sources.DOUBAN.value,
                media_type=MediaTypes.EPISODE.value,
                season_number=SEASON_NUMBER,
                episode_number=number,
                defaults={
                    "title": metadata["title"],
                    "image": metadata["image"] or settings.IMG_NONE,
                },
            )
            new_items.append(episode_item)
            episode = episode_model(
                item=episode_item,
                related_season=season_instance,
                end_date=watched,
            )
            episode._history_date = watched or timezone.now()
            self.bulk_media[MediaTypes.EPISODE.value].append(episode)

        self._add_events(season_item, count, metadata)

    def _add_events(self, season_item, count, metadata):
        """Give the season a calendar event per episode, so its length is known.

        A show's episode count on the list page is not stored as a number: it is
        summed from the ``Event`` rows whose item is the **season** item (see
        ``MediaManager._annotate_tv_released_episodes``, which filters on
        ``item__media_type=season``). Without them a Douban show shows "30" where
        a TMDB show shows "30 / 30", because nothing says how many episodes the
        season has.

        The event date is the season's first air date: Douban does not date the
        episodes of a show, and a date in the past is what marks an episode as
        released, which is right for an entry taken from a "watched" list.

        A season with no first air date gets no events: an invented date would
        put a made up day on the calendar, and the count staying unknown is
        honest. That is 8 of the 170 shows.
        """
        from events.models import Event  # noqa: PLC0415

        date = self._release_date(metadata)
        if date is None:
            return

        air_datetime = timezone.datetime(
            date.year,
            date.month,
            date.day,
            12,
            0,
            tzinfo=zoneinfo.ZoneInfo(WATCHED_TIME_ZONE),
        )

        existing = set(
            Event.objects.filter(
                item=season_item,
                content_number__isnull=False,
            ).values_list("content_number", flat=True),
        )

        events = [
            Event(
                item=season_item,
                content_number=number,
                datetime=air_datetime,
            )
            for number in range(1, count + 1)
            if number not in existing
        ]
        if events:
            Event.objects.bulk_create(events, ignore_conflicts=True)

    def _existing_tv(self, media_id):
        """Return the show row the user already tracks, or None."""
        tracked = self.existing_media[MediaTypes.TV.value][Sources.DOUBAN.value]
        return tracked.get(media_id)

    def _metadata(self, media_id):
        """Return the metadata of a show, from the cache or from Douban.

        A rate limit is not waited out any more: the exit IP is switched through
        the local pool and the same show is asked again, up to three times. A
        show that is limited on every exit IP is skipped by the caller, and only
        a long run of such skips stops the batch. See ``douban_ip_switch``.
        """
        return douban_ip_switch.fetch_metadata(MediaTypes.TV.value, media_id)

    def _store_tv_item(self, metadata):
        """Create or update the item of a show and return it."""
        item, _ = Item.objects.update_or_create(
            media_id=metadata["media_id"],
            source=Sources.DOUBAN.value,
            media_type=MediaTypes.TV.value,
            defaults={
                "title": metadata["title"],
                "image": metadata["image"] or settings.IMG_NONE,
                "synopsis": metadata["synopsis"],
                "genres": metadata.get("genres") or [],
                "directors": metadata.get("directors") or [],
                "cast": metadata.get("cast") or [],
                "release_date": self._release_date(metadata),
            },
        )
        return item

    def _release_date(self, metadata):
        """Return the first air date of a show as a date, or None."""
        raw = metadata.get("release_date")
        if not raw:
            return None

        try:
            return timezone.datetime.strptime(raw, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None

    def _notes(self, note):
        """Return the notes of a new entry: the import mark plus the stars."""
        if not note:
            return IMPORT_NOTE

        return f"{IMPORT_NOTE}\n{note}"

    def _sync_existing(self, existing, note):
        """Fill in what an already tracked show is missing.

        ``sync_existing_media`` only fills the score and notes the entry is
        missing and moves the status forward, so a rating or a note typed by hand
        is never overwritten. That guard is what keeps ``mode=new`` safe to re-run.

        A show has no end date of its own on this build: the date is read from the
        season's episodes, which is why the season is written even when the show
        row already exists.
        """
        return helpers.sync_existing_media(
            existing,
            {
                "notes": note,
                "status": Status.COMPLETED.value,
            },
        )

    def _cache_metadata(self, media_id):
        """Fill the provider cache of a show that is already tracked.

        Opening the details page of a Douban show calls the provider, which asks
        Douban again. Filling the cache with what has just been read saves one of
        the slow, rate limited calls per show. A cache that cannot be reached is
        treated as empty, and a failure to fill it is only a warning.
        """
        if self._is_cached(media_id):
            return

        try:
            self._metadata(media_id)
        except Exception as error:  # noqa: BLE001
            logger.warning("Could not refresh Douban show %s: %s", media_id, error)

    def _is_cached(self, media_id):
        """Return whether the provider already has this show in its cache."""
        return douban_tv.cache_get(self._cache_key(media_id)) is not None

    def _cache_key(self, media_id):
        """Return the key ``app.providers.douban_tv`` stores a show under."""
        return f"{Sources.DOUBAN.value}_{MediaTypes.TV.value}_{media_id}"

    # ---- progress file -------------------------------------------------

    def _read_progress(self):
        """Return the progress file as a dict, empty when it is unreadable."""
        try:
            data = json.loads(self._progress_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

        return data if isinstance(data, dict) else {}

    def _load_failed(self):
        """Return the failures a previous run recorded."""
        failed = self._read_progress().get("failed")
        return (
            [item for item in failed if isinstance(item, dict)]
            if isinstance(failed, list)
            else []
        )

    def _load_done(self):
        """Return the show ids a previous run already imported.

        Only shows that were actually written land in ``done``, so a show that
        failed is never skipped: the next run tries it again.
        """
        if self.mode != "new":
            return set()

        done = self._read_progress().get("done")
        return {str(media_id) for media_id in done} if isinstance(done, list) else set()

    def _dump_progress(self):
        """Write the running state of the import to the progress file."""
        done_set = set(self.done)
        failed = [
            item for item in self._previous_failures if item.get("id") not in done_set
        ]
        failed.extend(self.failed)
        by_id = {}
        for item in failed:
            by_id[str(item.get("id"))] = item

        try:
            self._progress_path.parent.mkdir(parents=True, exist_ok=True)
            self._progress_path.write_text(
                json.dumps(
                    {
                        "done": self.done,
                        "failed": list(by_id.values()),
                        "skipped_movies": self.skipped_movies,
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError as error:
            logger.warning("Could not write the Douban TV progress file: %s", error)

    # ---- writing -------------------------------------------------------

    def _flush(self):
        """Write the gathered media to the database and clear the queues."""
        helpers.cleanup_existing_media(self.to_delete, self.user)
        helpers.bulk_create_media(self.bulk_media, self.user)
        helpers.bulk_update_media(
            self.to_update,
            helpers.EXISTING_MEDIA_UPDATE_FIELDS,
            self.user,
        )
        self.bulk_media.clear()
        self.to_delete.clear()
        self.to_update.clear()

    def _report(self):
        """Add the summary lines of a finished run to the warnings."""
        if self.skipped_movies:
            self.warnings.append(
                f"跳过了 {self.skipped_movies} 部电影（第一期做过了），这次只导入了剧集。",
            )

        if self.failed:
            ids = "、".join(item["id"] for item in self.failed[:10])
            more = " 等" if len(self.failed) > 10 else ""
            self.warnings.append(
                f"{len(self.failed)} 部剧集没能导入（{ids}{more}），"
                "下次导入会重试，已经导入的不受影响。",
            )
