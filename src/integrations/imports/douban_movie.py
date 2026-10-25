"""Import the films of a public Douban account into Yamtrack.

Douban has no export file, but the mobile web app reads the same collection
from ``rexxar``, an API that answers without a login as long as the request
looks like the iPhone app: see ``app.providers.douban_movie`` for the headers
and for what each field turned out to be on 2026-10-22.

The collection is read from one endpoint:

``GET /rexxar/api/v2/user/<uid>/interests?type=movie&status=done``
    ``total`` says 647 for this account and the real page size is 50 no matter
    what ``count`` asks for, so 13 pages of 50 are walked. They hold **646**
    entries: one is never sent by the server, which is a gap in Douban's own
    offset paging and not a missed page.

    ``type=movie`` is the whole collection, films and shows together. **Do not
    switch to other type values**: ``type=tv`` is not honoured and answers with
    a mix of both. What actually separates them is ``subject.subtype``, which is
    ``movie`` or ``tv`` and nothing else. This collection is 476 films and 170
    shows, and only the films are imported: shows are counted and reported, and
    will be handled separately.

Two fields both called ``rating``, with nothing in common:

- ``interest.rating.value``, 1-5: the user's own star rating. Kept as a note,
  never as the score.
- ``subject.rating.value``, 0-10: the community average. This is the score.

One detail call per film is needed, because the list entry does not carry the
running time, the synopsis or the languages. Three seconds apart answered 12 of
12 times; anything faster risks an hours long block, so the provider's throttle
is reused for the list pages as well.

A film that fails on its own is recorded and the batch carries on. What was
already done is dumped to ``_tmp/douban_movie_progress.json`` after every film,
so a run that stops halfway resumes instead of starting over.

``interest.create_time`` is when the film was marked as watched, a Beijing
timestamp with seconds (``"2026-10-21 13:25:07"``). It becomes ``end_date``,
which is where Yamtrack keeps "when this was watched": a film has no start, so
``start_date`` stays empty. The whole timestamp is kept, not just the day,
because ``end_date`` is a ``DateTimeField`` and the hour is what the details
page prints. An entry the user already has keeps its own date: the import only
fills a date that is missing.
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
from app.providers import douban_movie, services
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

# The whole collection, whatever ``type`` is asked for: ``type=tv`` is not
# honoured and mixes films back in, ``type=movie`` already means "everything".
INTERESTS_TYPE = "movie"
INTERESTS_STATUS = "done"

# Douban hands back 50 entries per page even when ``count`` asks for more
PAGE_SIZE = 50
# Ceiling for the page walk. The collection is 13 pages; this only stops a
# server that keeps answering from spinning forever.
MAX_PAGES = 200

# Films are watched, which in Yamtrack is a single unit of progress
MOVIE_PROGRESS = 1
# Yamtrack scores out of ten, Douban's community average already does
SCORE_MAX = 10
# Douban rates with one to five stars
STARS_MAX = 5
STAR_FULL = "★"
STAR_EMPTY = "☆"

SUBTYPE_MOVIE = "movie"
SUBTYPE_TV = "tv"

# ``interest.create_time``, the moment the film was marked as watched. Douban
# sends it in Beijing time with no zone attached.
WATCHED_AT_FORMAT = "%Y-%m-%d %H:%M:%S"
WATCHED_TIME_ZONE = "Asia/Shanghai"

# Where a long import stopped, so the next run picks up what is still missing.
# It sits next to the database, outside of the source tree.
PROGRESS_FILE = "_tmp/douban_movie_progress.json"
IMPORT_NOTE = "Imported from Douban"
RATING_NOTE_PREFIX = "我的豆瓣评分"


def importer(uid, user, mode, limit=None):
    """Import the films of a Douban account for one user.

    ``limit`` stops after that many films, which is how the first run is
    checked on a handful before the whole collection is asked for.
    """
    return DoubanMovieImporter(uid, user, mode, limit=limit).import_data()


def watched_at(interest):
    """Return when a film was marked as watched, or None.

    ``create_time`` is Beijing time without a zone, so it is read as such and
    handed over as an aware datetime: ``USE_TZ`` is on, and a naive value would
    be taken as UTC and shown two to eight hours off on the details page.
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
    """Return ``★★★★☆`` for a star rating out of five, or None.

    The value Douban sends is a whole number of stars. A half star is shown as
    a filled one, since the note is there to be read at a glance.
    """
    try:
        stars = int(round(float(value)))
    except (TypeError, ValueError):
        return None

    if stars <= 0 or stars > STARS_MAX:
        return None

    return STAR_FULL * stars + STAR_EMPTY * (STARS_MAX - stars)


def rating_note(interest):
    """Return the one line note holding the user's own star rating, or None.

    This is ``interest.rating``, the caller's 1-5 stars. It is a different
    field with a different meaning than ``subject.rating``, the 0-10 community
    average that is stored as the score.
    """
    rating = interest.get("rating") or {}
    value = rating.get("value")
    text = stars_text(value)

    if not text:
        return None

    return f"{RATING_NOTE_PREFIX}：{text}（{int(round(float(value)))} 星）"


class DoubanMovieImporter:
    """Import the films a Douban account has marked as watched."""

    def __init__(self, uid, user, mode, limit=None):
        """Initialize the importer with the Douban uid, user, mode and limit."""
        self.uid = str(uid).strip()
        self.user = user
        self.mode = mode
        self.limit = limit
        self.warnings = []

        self.existing_media = helpers.get_existing_media(user)
        # Existing media changed in "new" mode, by primary key
        self.to_update = defaultdict(dict)
        # Media ids to delete in overwrite mode
        self.to_delete = defaultdict(lambda: defaultdict(set))
        # Bulk creation lists for each media type
        self.bulk_media = defaultdict(list)

        # Where this run keeps its state. Set before anything reads it.
        self._progress_path = Path(settings.BASE_DIR).parent / PROGRESS_FILE

        # Films written by this run, kept on disk so a second run resumes
        self.done = []
        self.failed = []
        # Failures of earlier runs, carried over so none silently disappears
        self._previous_failures = self._load_failed()
        self.skipped_shows = 0
        # Films still to do, used by the message of a run that got throttled
        self._remaining = 0

        logger.info(
            "Initialized Douban movie importer for user %s (douban uid %s) "
            "with mode %s, limit %s",
            user.username,
            uid,
            mode,
            limit,
        )

    # ---- collection ----------------------------------------------------

    def import_data(self):
        """Import every film of the collection, shows counted and skipped."""
        try:
            interests = self._fetch_interests()
        except services.ProviderAPIError as error:
            # Only what the user can act on is reported instead of crashing the
            # task: too many requests from this address (wait and retry) and a
            # hard block (wait a few hours). Both mean "come back later", so
            # they keep the old "0 imported, here is why" answer.
            if douban_movie.is_rate_limited_error(error):
                msg = douban_movie.RATE_LIMIT_MESSAGE
            elif getattr(error, "status_code", None) == BLOCKED_STATUS:
                msg = douban_movie.BLOCKED_MESSAGE
            else:
                # Anything else - no network, a proxy that will not connect, a
                # timeout, a 5xx - is not the user's to fix and it imported
                # nothing. It is raised as a network error so the task ends
                # FAILED *and* Celery tries again an hour later, instead of
                # being dressed up as a finished task with nothing to do.
                raise MediaImportNetworkError(
                    douban_movie.network_error_message(error, "电影"),
                ) from error
            logger.warning("Douban movie import could not start: %s", msg)
            return {MediaTypes.MOVIE.value: 0}, msg

        logger.info("Douban collection holds %s interests", len(interests))
        # Counted over the whole collection rather than over what this run gets
        # to, so a test run of twenty films still reports the real number of
        # shows that will be left for the second phase
        self.skipped_shows = sum(
            1
            for interest in interests
            if (interest.get("subject") or {}).get("subtype") == SUBTYPE_TV
        )

        # ``imported`` has to exist before the try: when Douban blocks the run,
        # ``_import_interests`` raises and the handler still reports a count.
        # It used to be assigned inside the try only, so the throttled path died
        # with UnboundLocalError instead of returning the partial result.
        imported = 0
        # A fresh run starts with a clean skip counter, so a batch that was cut
        # off yesterday does not stop after one skipped film today
        douban_ip_switch.reset()
        try:
            imported = self._import_interests(interests)
        except services.ProviderAPIError as error:
            if not douban_movie.is_blocked(error):
                raise
            # Douban cut the run off. Store what was gathered, keep the rest
            # for the next run rather than failing every remaining film.
            self._flush()
            self._dump_progress()
            msg = (
                f"豆瓣在导入 {len(self.done)} 部之后开始限流，剩下的 "
                f"{self._remaining} 部没导。已经导入的不会丢，"
                "过一两个小时再跑一次就会接着往下走。"
            )
            logger.warning(msg)
            self._report()
            deduplicated = "\n".join(dict.fromkeys(str(w) for w in self.warnings))
            return {MediaTypes.MOVIE.value: imported}, "\n".join(
                [deduplicated, msg] if deduplicated else [msg],
            )

        self._flush()
        self._report()

        deduplicated = "\n".join(dict.fromkeys(str(w) for w in self.warnings))
        return {MediaTypes.MOVIE.value: imported}, deduplicated

    def _fetch_interests(self):
        """Return every entry of the collection, walking the pages.

        The walk cannot stop on a page shorter than ``PAGE_SIZE``: Douban hands
        back 49 entries for ``start=50`` and a full 50 again after it, so a
        short page is normal in the middle of the collection. What ends the walk
        is an empty page, or having stepped past ``total``.
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
                # The offset paging of Douban occasionally repeats an entry,
                # and the same subject must not be imported twice
                if not media_id or media_id in seen:
                    continue
                seen.add(media_id)
                interests.append(entry)

            logger.info(
                "Douban collection page %s (start=%s): %s entries, %s collected"
                " of %s",
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
        """Import the films of the collection, return how many were new.

        Douban starts answering its IP rate limit after roughly fifty detail
        calls. A film that is cut off that way is not broken, so the importer
        waits and asks again, and only ends the batch when the wait has grown
        long enough to say the limit is really in place. Everything gathered so
        far is written before that happens.

        ``_settled`` is what makes a re-run cheap and correct at the same time:
        a film whose entry is in the library with its watch date needs nothing
        and costs no request, while a film that failed, or that was imported
        before the date was written, is looked at again.
        """
        done_before = self._load_done()
        if done_before:
            logger.info(
                "Resuming the Douban movie import, %s films were written before",
                len(done_before),
            )

        imported = 0
        processed = 0
        total_films = sum(
            1
            for interest in interests
            if (interest.get("subject") or {}).get("subtype") == SUBTYPE_MOVIE
        )
        self._remaining = total_films

        for interest in interests:
            subject = interest.get("subject") or {}
            media_id = str(subject.get("id") or "")

            if subject.get("subtype") == SUBTYPE_TV:
                # A show. This importer only handles films, so it is left for
                # the second phase instead of being half imported. The count of
                # the whole collection is reported at the end.
                continue

            if subject.get("subtype") != SUBTYPE_MOVIE:
                self.warnings.append(
                    f"{subject.get('title') or media_id}: 无法判断是电影还是剧集，已跳过。",
                )
                continue

            if self.limit is not None and processed >= self.limit:
                logger.info("Stopping at the limit of %s films", self.limit)
                break

            if not media_id:
                self.warnings.append("一条收藏没有条目 id，已跳过。")
                continue

            processed += 1
            self._remaining = max(total_films - processed, 0)

            if self._settled(interest, media_id, watched_at(interest)):
                logger.debug("Douban film %s needs nothing more", media_id)
                continue

            try:
                if self._process_movie(interest, media_id):
                    imported += 1
            except DoubanStillLimitedError:
                # Limited on every exit IP: skipped, so the batch carries on.
                # It is not marked done, so the next run tries it again.
                self._skip_rate_limited(subject, media_id)
                continue
            except services.ProviderAPIError as error:
                if douban_movie.is_blocked(error):
                    # Nothing to do with this film: the whole network is being
                    # throttled, so the same error would repeat for every one
                    # left. Keep it out of ``failed`` and let the next run
                    # pick it up.
                    raise
                logger.warning(
                    "Error processing Douban film %s - %s",
                    subject.get("title"),
                    error,
                )
                self.failed.append({"id": media_id, "reason": str(error)[:200]})
                self._dump_progress()
                continue
            except Exception as error:  # noqa: BLE001
                # One odd film must not end the whole batch. It is not added to
                # ``done`` either, so the next run tries it again instead of
                # skipping it forever.
                logger.warning("Douban film %s failed: %s", media_id, error)
                self.failed.append({"id": media_id, "reason": str(error)[:200]})
                self._dump_progress()
                continue

            # Only a film that was actually written is done
            self.done.append(media_id)
            self._dump_progress()
            self._flush()

        return imported

    def _skip_rate_limited(self, subject, media_id):
        """Record a film that was limited on every exit IP and carry on.

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
        """Return whether the user already has this film from Douban."""
        return media_id in self.existing_media[MediaTypes.MOVIE.value][
            Sources.DOUBAN.value
        ]

    def _settled(self, interest, media_id, watched):
        """Return whether a film a previous run handled needs nothing more.

        A film that was imported before the watch date was written is in the
        progress file and in the library, but has no ``end_date``. Such a film
        is looked at again so its date lands, which is also how the entries
        imported before this feature are filled in: one more ``mode=new`` run
        over the collection, with no extra request to Douban unless a date is
        actually missing.
        """
        if not self._is_tracked(media_id):
            return False

        if watched is None:
            # Douban gave no date to begin with, nothing to backfill
            return True

        existing = self._existing(media_id)
        return existing is not None and existing.end_date is not None

    def _process_movie(self, interest, media_id):
        """Store one film and its tracking entry, return whether it was new."""
        existing = self._existing(media_id)

        note = rating_note(interest)
        watched = watched_at(interest)

        if existing and self.mode == "new":
            # The film is already tracked. Only what Douban knows is filled in,
            # and ``sync_existing_media`` leaves anything the user typed alone,
            # so a hand written note, a rating or a watch date survives.
            if self._sync_existing(existing, note, watched):
                self.to_update[MediaTypes.MOVIE.value][existing.pk] = existing
            else:
                logger.debug("Existing Douban film %s is up to date", media_id)
            # The details page reads the provider cache, so it is filled even
            # when nothing is written
            self._cache_metadata(media_id)
            return False

        if existing and self.mode == "overwrite":
            self.to_delete[MediaTypes.MOVIE.value][Sources.DOUBAN.value].add(media_id)

        metadata = self._metadata(media_id)
        item = self._store_item(metadata)

        model = apps.get_model(app_label="app", model_name=MediaTypes.MOVIE.value)
        instance = model(
            item=item,
            user=self.user,
            score=metadata["score"],
            status=Status.COMPLETED.value,
            progress=MOVIE_PROGRESS,
            notes=self._notes(note),
            runtime=metadata.get("runtime_minutes"),
            # When Douban says the film was marked as watched. A film has no
            # start, so ``start_date`` stays empty by design.
            end_date=watched,
        )
        # The history row of an imported film is stamped with its watch date,
        # not the moment of the import, so the timeline reads correctly
        instance._history_date = watched or timezone.now()
        self.bulk_media[MediaTypes.MOVIE.value].append(instance)
        return True

    def _existing(self, media_id):
        """Return the entry of a film the user already tracks, or None."""
        return self.existing_media[MediaTypes.MOVIE.value][
            Sources.DOUBAN.value
        ].get(media_id)

    def _metadata(self, media_id):
        """Return the metadata of a film, from the cache or from Douban.

        A rate limit is not waited out any more: the exit IP is switched through
        the local pool and the same film is asked again, up to three times. A
        film that is limited on every exit IP is skipped by the caller, and only
        a long run of such skips stops the batch. See ``douban_ip_switch``.
        """
        return douban_ip_switch.fetch_metadata(MediaTypes.MOVIE.value, media_id)

    def _store_item(self, metadata):
        """Create or update the item of a film and return it."""
        item, _ = Item.objects.update_or_create(
            media_id=metadata["media_id"],
            source=Sources.DOUBAN.value,
            media_type=MediaTypes.MOVIE.value,
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
        """Return the release date of a film as a date, or None."""
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

    def _sync_existing(self, existing, note, watched):
        """Fill in what an already tracked film is missing.

        ``sync_existing_media`` only fills the score and the notes the entry is
        missing and moves the status forward, so a rating, a note or a watch
        date typed by hand is never overwritten. That guard is what keeps
        ``mode=new`` safe to re-run.

        ``end_date`` is handled here rather than left to that helper because of
        the case that matters: an entry imported before this field was written
        is already completed, and a completed entry gets no date from the
        helper, so its watch date would stay missing forever. It is filled
        under the same rule: only when the entry has none and is completed (or
        is moved to completed now), so a planned or paused film the user set by
        hand keeps its own state and its empty date.
        """
        changed = helpers.sync_existing_media(
            existing,
            {
                "notes": note,
                "status": Status.COMPLETED.value,
                "progress": MOVIE_PROGRESS,
            },
        )

        if watched and existing.end_date is None:
            became_completed = helpers.advance_status(
                existing,
                Status.COMPLETED.value,
            )
            if became_completed or existing.status == Status.COMPLETED.value:
                existing.end_date = watched
                existing.progressed_at = timezone.now()
                changed = True

        return changed

    def _cache_metadata(self, media_id):
        """Fill the provider cache of a film that is already tracked.

        Opening the details page of a Douban film calls the provider, which
        asks Douban for the metadata again. Filling the cache with what has just
        been read saves one of the slow, rate limited calls per film.

        The cached entry is left as it is when it is already there: re-reading
        it would cost a call to a rate limited site and change nothing. A cache
        that cannot be reached (Redis restarted, service stopped) is treated as
        empty, and a failure to fill it is only a warning: the film itself is
        already in the database and must not fail over a cache.
        """
        if self._is_cached(media_id):
            return

        try:
            self._metadata(media_id)
        except Exception as error:  # noqa: BLE001
            # A cache that will not fill only costs one call later on
            logger.warning("Could not refresh Douban film %s: %s", media_id, error)

    def _is_cached(self, media_id):
        """Return whether the provider already has this film in its cache."""
        return douban_movie.cache_get(self._cache_key(media_id)) is not None

    def _cache_key(self, media_id):
        """Return the key ``app.providers.douban_movie`` stores a film under."""
        return f"{Sources.DOUBAN.value}_{MediaTypes.MOVIE.value}_{media_id}"

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
        return [item for item in failed if isinstance(item, dict)] if isinstance(
            failed,
            list,
        ) else []

    def _load_done(self):
        """Return the film ids a previous run already imported.

        Only films that were actually written land in ``done``, so a film that
        failed (a rate limit, a cache that was down) is never skipped: the next
        run simply tries it again.
        """
        if self.mode != "new":
            # A full overwrite re-reads everything on purpose
            return set()

        done = self._read_progress().get("done")
        return {str(media_id) for media_id in done} if isinstance(done, list) else set()

    def _dump_progress(self):
        """Write the running state of the import to the progress file.

        The failed films of previous runs are carried over, so a film that keeps
        failing stays visible instead of vanishing from the report as soon as
        the next run starts. A film that succeeds on a later run is dropped
        from that list.
        """
        done_set = set(self.done)
        failed = [
            item for item in self._previous_failures if item.get("id") not in done_set
        ]
        failed.extend(self.failed)
        # One line per film, the newest reason winning
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
                        "skipped_shows": self.skipped_shows,
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError as error:
            logger.warning("Could not write the Douban movie progress file: %s", error)

    # ---- writing -------------------------------------------------------

    def _flush(self):
        """Write the gathered films to the database and clear the queues."""
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
        if self.skipped_shows:
            self.warnings.append(
                f"跳过了 {self.skipped_shows} 部剧集（第二期做），这次只导入了电影。",
            )

        if self.failed:
            ids = "、".join(item["id"] for item in self.failed[:10])
            more = " 等" if len(self.failed) > 10 else ""
            self.warnings.append(
                f"{len(self.failed)} 部电影没能导入（{ids}{more}），"
                "下次导入会重试，已经导入的不受影响。",
            )
