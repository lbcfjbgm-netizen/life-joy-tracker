"""Import a WeRead (微信读书) account into Yamtrack.

WeRead has no export file. Everything comes from its agent gateway: one POST
endpoint that takes the ``api_name`` of the call plus that call's business
parameters **flat at the top level of the body**. Wrapping them in ``params`` is
silently dropped by the gateway, which then answers as if nothing was asked.

The shape of every response below was read off real calls on 2026-10-22, not
off the documentation:

``/shelf/sync``
    ``books[]`` holds the whole shelf, 278 books for this account. Each entry
    carries ``bookId``, ``title``, ``author``, ``cover``, ``category``,
    ``finishReading`` (1 once the book was read to the end), ``secret``,
    ``readUpdateTime`` and ``updateTime``. ``albums`` is the audio book shelf
    and is empty for this account. ``archive`` is not extra books: it holds two
    named groups, 读完 with 131 ids and 待读 with 39, and both are subsets of
    ``books``, so they only label books the shelf lists anyway.

``/book/getprogress``
    ``book.readingTime`` is the number of seconds actually read and is the only
    usable time value: ``recordReadingTime`` is 0 on every book, as the known
    quirks of this API say. ``book.progress`` is a 0-100 percentage, not a page
    number. ``book.finishTime`` is the unix time the book was finished.

``/book/info``
    Title, author, cover, ``intro``, ``newRating`` (0-1000, so 815 is 8.15 out
    of 10), ``newRatingCount``, ``publisher``, ``isbn``. There is **no page
    count**: ``wordCount`` is advertised by the API catalogue but is not sent,
    and neither is any other page-ish field on the six books that were checked.
    ``total_pages`` therefore stays empty instead of being invented.

``/user/notebooks``
    The books that carry notes, 28 of them, ``totalNoteCount`` 179. Cursor
    paginated through ``count`` + ``lastSort`` (the ``sort`` of the last book of
    the page); there is no offset and no limit. ``noteCount`` is the number of
    highlights and ``reviewCount`` the number of thoughts, both checked against
    the two calls below on all 28 books and matching every time.
    ``bookmarkCount`` is 0 on all of them even when there are 32 highlights, so
    it is the wrong field to gate on.

``/book/bookmarklist``
    ``updated[]`` holds the highlights: ``markText``, ``range``, ``chapterUid``,
    ``createTime``. ``chapters[]`` maps a ``chapterUid`` to its title.

``/review/list/mine``
    ``reviews[].review`` holds ``content``, ``abstract`` (the passage the note
    was written on), ``chapterTitle`` and ``createTime``. The parameter here is
    the lowercase ``bookid``; every other call uses ``bookId``.
"""

import json
import logging
import time
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import requests
from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from app.models import Highlight, Item, MediaTypes, Note, Sources, Status
from app.providers import weread as weread_provider
from integrations.imports import helpers

logger = logging.getLogger(__name__)

GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"
SKILL_VERSION = "1.0.5"
REQUEST_TIMEOUT = 20

# WeRead closes the door on a client that asks too fast, and a whole shelf is a
# few hundred calls, so they are spaced out. Two calls per book at this pace
# keeps a 278 book shelf around fifteen minutes, which is fine for a background
# task and kind to the API.
REQUEST_DELAY = 1.5

# Every thousandth of a point on the WeRead scale is a hundredth of a point
# here: 815 out of 1000 is 8.15 out of 10, which Yamtrack stores as 8.2 because
# a score keeps one decimal.
SCORE_MAX = 10
WEREAD_RATING_MAX = 1000

# ``count`` on /user/notebooks and /review/list/mine. The API hands back 28 of
# 28 notebook books for a count of 50 and caps a review page on its own.
PAGE_SIZE = 50
MAX_NOTEBOOK_PAGES = 20
MAX_REVIEW_PAGES = 20

# A key that expired, was revoked or never existed. The gateway answers HTTP 401
# for all of them and puts the real reason in ``errcode``, so both are checked:
# -2013 is a failed authentication, -2010 an account that does not exist.
AUTH_HTTP_STATUS = requests.codes.unauthorized
AUTH_ERROR_CODES = {-2010, -2013}
AUTH_WARNING = "微信读书 Key 失效或无权限，请检查 .env 里的 WEREAD_API_KEY"

# ``/book/getprogress`` reports how far the book was read as a percent.
PROGRESS_PERCENT = 100

# WeRead's own flag for a book that was read to the end.
FINISHED = 1

# Where a long import stopped, so the next run picks up the books that are still
# missing instead of starting over. It sits next to the database, outside of the
# source tree.
PROGRESS_FILE = "_tmp/weread_progress.json"
IMPORT_NOTE = "Imported from WeRead"


def read_api_key():
    """Return the WeRead API key, preferring the freshest copy in ``.env``.

    Django reads ``.env`` once at startup, so a key rotated on disk would not be
    seen until the next restart. The file is read first for that reason, and the
    key already loaded into the settings is the fallback.
    """
    env_path = Path(settings.BASE_DIR).parent / ".env"
    try:
        with env_path.open(encoding="utf-8") as handle:
            for line in handle:
                key, sep, value = line.partition("=")
                if sep and key.strip() == "WEREAD_API_KEY":
                    token = value.strip().strip('"').strip("'")
                    if token:
                        return token
    except OSError:
        pass

    return getattr(settings, "WEREAD_API_KEY", "") or ""


def score_from_rating(raw):
    """Turn a WeRead rating into a Yamtrack score.

    WeRead rates a book out of a thousand, so 815 means "8.15 out of 10" and is
    stored as 8.2, the one decimal Yamtrack keeps. A book that nobody rated has
    no ``newRating`` at all: that returns ``None``, and the score stays empty,
    because a zero would claim the book was rated zero, which is a different
    thing from never having been rated.

    The division is done in decimal rather than in binary floating point so that
    8.15 rounds up the way a person would read it: plain floats carry 8.15 as
    8.149999... and would round it down to 8.1.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return None

    score = (Decimal(str(raw)) / Decimal(WEREAD_RATING_MAX) * Decimal(SCORE_MAX))
    score = score.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

    return max(Decimal(0), min(score, Decimal(SCORE_MAX)))


def importer(api_key, user, mode):
    """Import every book of a WeRead account for one user."""
    return WeReadImporter(api_key, user, mode).import_data()


class WeReadError(Exception):
    """A WeRead call failed in a way the importer reports to the user."""


class WeReadAuthError(WeReadError):
    """The API key is missing, expired or not allowed to read this account."""


class WeReadNetworkError(WeReadError, helpers.MediaImportNetworkError):
    """The gateway could not be reached at all: no answer, or a timeout.

    It carries both parents on purpose, and each one buys one behaviour:

    - ``WeReadError`` keeps every "this is a bonus, carry on without it" handler
      working. A blip while reading one book's details, progress, highlights or
      notes still degrades to the shelf data instead of failing the book, which
      is how this importer already behaves.
    - ``MediaImportNetworkError`` (and through it
      ``MediaImportUnexpectedError``) is in the Celery retry policy, so a run
      that could not even read the shelf is retried an hour later instead of
      ending as a success that imported nothing.

    The two limits the user can act on are deliberately *not* this type: a bad
    key stays ``WeReadAuthError`` and a gateway that answers with an error code
    stays ``WeReadError``, because asking again an hour later would only repeat
    the same answer.
    """


class WeReadImporter:
    """Import the WeRead shelf, its reading time, notes and highlights."""

    def __init__(self, api_key, user, mode):
        """Initialize the importer with the API key, user and mode."""
        self.api_key = (api_key or "").strip()
        self.user = user
        self.mode = mode
        self.warnings = []

        self.existing_media = helpers.get_existing_media(user)
        # Existing media changed in "new" mode, by primary key
        self.to_update = defaultdict(dict)
        # Reading time of existing media, which is machine data rather than
        # something the user edits, so it is written even in "new" mode
        self.to_update_reading_time = []
        # Media ids to delete in overwrite mode
        self.to_delete = defaultdict(lambda: defaultdict(set))
        # Bulk creation lists for each media type
        self.bulk_media = defaultdict(list)

        # Book ids handled by this run, kept on disk so a second run resumes
        self.done = []
        self.failed = []
        self._progress_path = Path(settings.BASE_DIR).parent / PROGRESS_FILE

        self._last_call = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

        logger.info(
            "Initialized WeRead importer for user %s with mode %s",
            user.username,
            mode,
        )

    # ---- gateway -------------------------------------------------------

    def _call(self, api_name, **params):
        """POST one call to the gateway and return the decoded body.

        The business parameters go flat at the top level of the body, and calls
        are spaced out by ``REQUEST_DELAY`` seconds.
        """
        if not self.api_key:
            raise WeReadAuthError(AUTH_WARNING)

        self._throttle()

        payload = {"api_name": api_name, "skill_version": SKILL_VERSION}
        payload.update(params)

        try:
            response = self.session.post(
                GATEWAY_URL,
                data=json.dumps(payload),
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as error:
            # The host never answered: refused, timed out, proxy down. This is
            # the one WeRead failure worth another attempt an hour later, so it
            # is raised as the retryable type. A bad key and a gateway that
            # answers with an error code are handled below and stay
            # non-retryable: the user has to fix those, not wait.
            msg = f"WeRead did not answer {api_name}: {error}"
            raise WeReadNetworkError(msg) from error

        if response.status_code == AUTH_HTTP_STATUS:
            raise WeReadAuthError(self._auth_message(response))

        if response.status_code >= requests.codes.bad_request:
            msg = f"WeRead refused {api_name} with HTTP {response.status_code}"
            raise WeReadError(msg)

        try:
            data = response.json()
        except ValueError as error:
            msg = f"WeRead sent a body that is not JSON for {api_name}"
            raise WeReadError(msg) from error

        errcode = data.get("errcode")
        if errcode in AUTH_ERROR_CODES:
            raise WeReadAuthError(self._auth_message(response, data))
        if errcode not in (None, 0):
            msg = f"WeRead refused {api_name}: {data.get('errmsg') or errcode}"
            raise WeReadError(msg)

        return data

    def _auth_message(self, response, data=None):
        """Return the one line warning a bad key deserves."""
        code = data.get("errcode") if isinstance(data, dict) else None

        if code is None:
            try:
                code = response.json().get("errcode")
            except ValueError:
                code = None

        if code is None:
            return AUTH_WARNING

        return f"{AUTH_WARNING}（接口返回 {code}）"

    def _throttle(self):
        """Wait long enough that two calls are never closer than the delay."""
        elapsed = time.monotonic() - self._last_call
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        self._last_call = time.monotonic()

    # ---- progress file -------------------------------------------------

    def _load_done(self):
        """Return the book ids a previous run already finished."""
        if self.mode != "new":
            # A full overwrite re-reads everything on purpose
            return set()

        try:
            data = json.loads(self._progress_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()

        done = data.get("done")
        return {str(book_id) for book_id in done} if isinstance(done, list) else set()

    def _dump_progress(self):
        """Write the running state of the import to the progress file.

        The ids are de-duplicated without reordering: a book that was carried
        over from an earlier run and is imported again must not end up listed
        twice.
        """
        try:
            self._progress_path.parent.mkdir(parents=True, exist_ok=True)
            self._progress_path.write_text(
                json.dumps(
                    {
                        "done": list(dict.fromkeys(self.done)),
                        "failed": self.failed,
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError as error:
            logger.warning("Could not write the WeRead progress file: %s", error)

    # ---- import --------------------------------------------------------

    def import_data(self):
        """Import the shelf, then the notes and highlights behind it."""
        try:
            books = self._fetch_shelf()
            counts = self._fetch_notebook_counts()
        except WeReadNetworkError:
            # The gateway never answered, so nothing was read: this is not an
            # empty shelf and must not be reported as a finished run that found
            # no books. Raising hands it to the Celery retry policy, which asks
            # again an hour later.
            raise
        except WeReadError as error:
            # A bad key is not a crash, it is something the user has to fix, and
            # so is a gateway that answers with an error code of its own
            logger.warning("WeRead import could not start: %s", error)
            return {MediaTypes.BOOK.value: 0}, str(error)

        logger.info("WeRead shelf holds %s books", len(books))

        imported, stopped_on = self._import_books(books, counts)
        self._flush()

        if stopped_on:
            # The key died halfway, so the rest of the shelf is reported as
            # missing work rather than attempted again on a dead key
            return {MediaTypes.BOOK.value: imported}, stopped_on

        if self.failed:
            self.warnings.append(
                f"{len(self.failed)} 本没能导入（原因见导入日志），"
                "下次导入会重试，已经导入的书不受影响。",
            )
        self._dump_progress()

        deduplicated = "\n".join(dict.fromkeys(str(w) for w in self.warnings))
        return {MediaTypes.BOOK.value: imported}, deduplicated

    def _import_books(self, books, counts):
        """Import every book of the shelf, return ``(count, stop warning)``.

        A book that fails on its own lands in ``failed`` and the batch carries
        on. A dead key stops the batch and comes back as a warning, because the
        remaining books would fail in exactly the same way.
        """
        done_before = self._load_done()
        if done_before:
            logger.info(
                "Resuming the WeRead import, %s books were done before",
                len(done_before),
            )
            # Books that are already in the library stay in the progress file.
            # Keeping only what this run wrote would empty it on a run where
            # everything is already imported, and the next run would then fetch
            # every detail page of the shelf again.
            self.done = sorted(done_before)

        imported = 0

        for book in books:
            media_id = str(book.get("bookId") or "")
            if not media_id:
                self.warnings.append("书架上有一条记录没有 bookId，已跳过。")
                continue

            if media_id in done_before and self._is_tracked(media_id):
                # The book itself is imported already. Its highlights and notes
                # are still worth another look: the stored ones are only ever
                # added to, and a book can gain notes after it was imported.
                existing = self.existing_media[MediaTypes.BOOK.value][
                    Sources.WEREAD.value
                ].get(media_id)
                if existing is not None:
                    self._import_notes(
                        existing.item,
                        counts.get(media_id, {}),
                        media_id,
                    )
                logger.debug("WeRead book %s was already imported", media_id)
                continue

            try:
                if self._process_book(book, media_id, counts.get(media_id, {})):
                    imported += 1
            except WeReadAuthError as error:
                logger.warning("WeRead import stopped at %s: %s", media_id, error)
                return imported, str(error)
            except Exception as error:  # noqa: BLE001
                # One odd book must not end the whole batch
                logger.warning("WeRead book %s failed: %s", media_id, error)
                self.failed.append({"id": media_id, "reason": str(error)[:200]})

            self.done.append(media_id)
            self._dump_progress()

        return imported, None

    def _is_tracked(self, media_id):
        """Return whether the user already has this book from WeRead."""
        return media_id in self.existing_media[MediaTypes.BOOK.value][
            Sources.WEREAD.value
        ]

    def _flush(self):
        """Write the gathered books, notes and highlights to the database."""
        helpers.cleanup_existing_media(self.to_delete, self.user)
        helpers.bulk_create_media(self.bulk_media, self.user)
        helpers.bulk_update_media(
            self.to_update,
            helpers.EXISTING_MEDIA_UPDATE_FIELDS,
            self.user,
        )
        # Reading time is not in EXISTING_MEDIA_UPDATE_FIELDS, so the books that
        # are already tracked get theirs through a narrower update
        if self.to_update_reading_time:
            helpers.bulk_update_media(
                {MediaTypes.BOOK.value: self.to_update_reading_time},
                {MediaTypes.BOOK.value: ["reading_seconds"]},
                self.user,
            )

    def _fetch_shelf(self):
        """Return every book of the shelf."""
        data = self._call("/shelf/sync")
        books = data.get("books")

        if not isinstance(books, list):
            # No error and no books: an empty shelf, not a broken answer
            return []

        return [book for book in books if book.get("bookId")]

    def _fetch_notebook_counts(self):
        """Return ``{bookId: counts}`` for the books that carry notes.

        One extra call learns which books have highlights or notes, so a plain
        shelf import does not spend two more calls on every book to find out
        that most of them have neither.
        """
        counts = {}
        last_sort = None

        for _ in range(MAX_NOTEBOOK_PAGES):
            params = {"count": PAGE_SIZE}
            if last_sort is not None:
                params["lastSort"] = last_sort

            try:
                data = self._call("/user/notebooks", **params)
            except WeReadAuthError:
                raise
            except WeReadError as error:
                # Notes are a bonus, the shelf itself still imports
                logger.warning("WeRead notebook overview unavailable: %s", error)
                return counts

            books = data.get("books") or []
            for entry in books:
                if isinstance(entry, dict) and entry.get("bookId"):
                    counts[str(entry["bookId"])] = {
                        "bookmarkCount": entry.get("bookmarkCount") or 0,
                        "reviewCount": entry.get("reviewCount") or 0,
                        "noteCount": entry.get("noteCount") or 0,
                    }

            if not data.get("hasMore") or not books:
                break

            last_sort = books[-1].get("sort")
            if last_sort is None:
                break

        return counts

    # ---- one book ------------------------------------------------------

    def _process_book(self, book, media_id, counts):
        """Store one book and its tracking entry, return whether it was new."""
        existing = self.existing_media[MediaTypes.BOOK.value][
            Sources.WEREAD.value
        ].get(media_id)

        details = self._fetch_details(media_id, book)
        progress = self._fetch_progress(media_id)
        status = self._status(book, progress)

        if existing and self.mode == "new":
            self._sync_existing(existing, details, progress, status)
            # The book is already tracked, but its details page still needs the
            # provider cache filled, or opening it answers 500
            self._cache_book_info(media_id, details)
            # Notes are new data either way, so they still land
            self._import_notes(existing.item, counts, media_id)
            return False

        if existing and self.mode == "overwrite":
            self.to_delete[MediaTypes.BOOK.value][Sources.WEREAD.value].add(media_id)

        item = self._store_item(media_id, details)
        self._import_notes(item, counts, media_id)

        model = apps.get_model(app_label="app", model_name=MediaTypes.BOOK.value)
        instance = model(
            item=item,
            user=self.user,
            score=details["score"],
            status=status,
            progress=self._page_progress(item, progress, status),
            notes=IMPORT_NOTE,
            reading_seconds=progress["reading_seconds"],
            end_date=self._finished_at(progress, status),
        )
        instance._history_date = self._history_date(progress)
        self.bulk_media[MediaTypes.BOOK.value].append(instance)
        return True

    def _fetch_details(self, media_id, shelf_book):
        """Return the detail fields of a book, falling back to the shelf entry.

        The shelf already carries the title, author and cover. The detail call
        adds the rating and the synopsis. When it fails the book is still
        imported on the shelf data: a tracked book without a rating beats a book
        missing from the library.
        """
        details = {
            "title": shelf_book.get("title") or media_id,
            "author": shelf_book.get("author") or "",
            "cover": shelf_book.get("cover") or settings.IMG_NONE,
            "synopsis": "",
            "score": None,
            "score_count": None,
            "publisher": "",
            "source_url": shelf_book.get("deepLink")
            or f"https://weread.qq.com/web/bookDetail/{media_id}",
        }

        try:
            info = self._call("/book/info", bookId=media_id)
        except WeReadAuthError:
            raise
        except WeReadError as error:
            logger.warning("WeRead detail for %s unavailable: %s", media_id, error)
            return details

        details["title"] = info.get("title") or details["title"]
        details["author"] = info.get("author") or details["author"]
        details["cover"] = info.get("cover") or details["cover"]
        details["synopsis"] = " ".join(str(info.get("intro") or "").split())
        details["score"] = score_from_rating(info.get("newRating"))
        details["score_count"] = info.get("newRatingCount") or None
        details["publisher"] = info.get("publisher") or ""
        details["source_url"] = (
            info.get("readerLink") or info.get("deepLink") or details["source_url"]
        )
        return details

    def _fetch_progress(self, media_id):
        """Return the seconds read, the percent read and the finish time.

        ``readingTime`` is the number of seconds read. ``recordReadingTime`` is
        zero on every book and is ignored on purpose. ``progress`` is a 0-100
        percent.
        """
        empty = {"reading_seconds": 0, "percent": None, "finished_on": None}

        try:
            data = self._call("/book/getprogress", bookId=media_id)
        except WeReadAuthError:
            raise
        except WeReadError as error:
            logger.warning("WeRead progress for %s unavailable: %s", media_id, error)
            return empty

        book = data.get("book")
        if not isinstance(book, dict):
            return empty

        return {
            "reading_seconds": self._whole(book.get("readingTime")),
            "percent": self._whole(book.get("progress")),
            "finished_on": self._whole(book.get("finishTime")) or None,
        }

    def _whole(self, value):
        """Return a non negative int for a number, zero when it is not one."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return max(0, int(value))

    def _status(self, book, progress):
        """Return the Yamtrack status of a shelf book."""
        if book.get("finishReading") == FINISHED:
            return Status.COMPLETED.value

        percent = progress["percent"]
        if percent is not None and percent >= PROGRESS_PERCENT:
            return Status.COMPLETED.value

        if progress["reading_seconds"] > 0 or (percent or 0) > 0:
            return Status.IN_PROGRESS.value

        # On the shelf but never opened
        return Status.PLANNING.value

    def _page_progress(self, item, progress, status=None):
        """Return how far the book was read, as a page or as a percent.

        WeRead reports how far a book was read as a 0-100 percent and never
        sends a page count (see the module docstring). Yamtrack keeps that
        number in ``progress``, an integer, and the unit comes from the item:

        - when ``total_pages`` is known the percent is turned into the page the
          reader stopped on, so the page count and the progress agree;
        - when it is not known, which is the normal case for a WeRead book, the
          percent itself is stored and the details page shows "已读 45%" instead
          of a page count. Storing nothing would throw away the only thing
          WeRead tells about the reading position.

        A finished book is read cover to cover, so a book counted in percent is
        written as 100 whatever percent the shelf reports: WeRead keeps the
        reading position of a finished book below 100 often enough (a book read
        to the last page still shows 99), and the tracker treats "finished" as
        the stronger statement. The import writes through a bulk call that
        skips the model, so this is the only place the rule can be applied for
        books imported for the first time.
        """
        percent = progress["percent"]
        if percent is None:
            return 0

        percent = max(0, min(int(percent), PROGRESS_PERCENT))

        total = item.total_pages
        if not total:
            if status == Status.COMPLETED.value:
                return PROGRESS_PERCENT
            return percent

        pages = round(total * percent / PROGRESS_PERCENT)
        return max(0, min(int(pages), int(total)))

    def _finished_at(self, progress, status):
        """Return the finish date of a completed book, or None."""
        if status != Status.COMPLETED.value:
            return None

        finished = progress.get("finished_on")
        if not finished:
            return None

        return timezone.datetime.fromtimestamp(
            finished,
            tz=timezone.get_current_timezone(),
        )

    def _history_date(self, progress):
        """Return the moment the reading last moved, for the history row."""
        finished = progress.get("finished_on")
        if finished:
            return timezone.datetime.fromtimestamp(
                finished,
                tz=timezone.get_current_timezone(),
            )
        return timezone.now()

    def _store_item(self, media_id, details):
        """Create or update the item of a book and return it.

        The book metadata is also handed to the provider cache, keyed the way
        ``app.providers.weread`` looks it up. That cache is where the provider
        keeps the ``bookInfo`` of every book it saw, because the public detail
        endpoint refuses an anonymous caller with ``用户不存在`` (-2010). Without
        this the provider raises a 404 while opening any book the importer
        created, and the details page answers 500. Filling it in here is what
        makes an imported book openable: the importer is the one component that
        holds a key, so it is the only one that can do it.
        """
        item, _ = Item.objects.update_or_create(
            media_id=media_id,
            source=Sources.WEREAD.value,
            media_type=MediaTypes.BOOK.value,
            defaults={
                "title": details["title"],
                "image": details["cover"],
                "synopsis": details["synopsis"],
            },
        )
        self._cache_book_info(media_id, details)
        return item

    def _cache_book_info(self, media_id, details):
        """Hand the book metadata to the provider cache.

        ``providers.weread.book()`` returns the cached entry as is, so the
        payload has to carry every key the details page reads. The raw
        ``bookInfo`` is cached too, as the provider's own ``book()`` rebuilds
        from it when the ready made entry is missing.
        """
        book_info = {
            "bookId": str(media_id),
            "title": details["title"],
            "author": details["author"],
            "cover": details["cover"],
            "intro": details["synopsis"],
        }

        metadata = {
            "media_id": str(media_id),
            "source": Sources.WEREAD.value,
            "source_url": details["source_url"],
            "media_type": MediaTypes.BOOK.value,
            "title": details["title"],
            "max_progress": None,
            "image": details["cover"] or settings.IMG_NONE,
            "synopsis": details["synopsis"] or "No synopsis available.",
            "genres": [],
            "score": details["score"],
            "score_count": details["score_count"],
            "details": {
                "author": details["author"],
                "publishers": [details["publisher"]] if details["publisher"] else [],
            },
            "related": {},
        }

        try:
            cache.set(weread_provider.book_info_cache_key(media_id), book_info)
            cache.set(
                f"{Sources.WEREAD.value}_{MediaTypes.BOOK.value}_{media_id}",
                metadata,
            )
        except Exception as error:  # noqa: BLE001
            # A cache that is down must not fail the import: the book is still
            # stored, only its details page stays unavailable
            logger.warning("Could not cache WeRead book %s: %s", media_id, error)

    def _sync_existing(self, existing, details, progress, status):
        """Write what WeRead knows onto a book the user already tracks.

        Only the fields of ``helpers.EXISTING_MEDIA_UPDATE_FIELDS`` are touched,
        and ``sync_existing_media`` only fills a score the entry is missing, so a
        rating typed by hand is never overwritten, and it only ever raises the
        progress, so a position tracked by hand is never pulled back. Reading
        time is machine data and is refreshed either way.
        """
        changed = helpers.sync_existing_media(
            existing,
            {
                "score": details["score"],
                "status": status,
                "progress": self._page_progress(existing.item, progress, status),
            },
        )
        if changed:
            self.to_update[MediaTypes.BOOK.value][existing.pk] = existing

        seconds = progress["reading_seconds"]
        if seconds and seconds != existing.reading_seconds:
            existing.reading_seconds = seconds
            self.to_update_reading_time.append(existing)

    # ---- notes and highlights ------------------------------------------

    def _import_notes(self, item, counts, media_id):
        """Store the highlights and notes of one book, skipping what is there."""
        if not counts:
            return

        # ``noteCount`` is the number of highlights and ``reviewCount`` the
        # number of thoughts, checked against both calls on all 28 notebook
        # books and matching every time. ``bookmarkCount`` stays 0 on all of
        # them and would never open the highlight call.
        if counts.get("noteCount"):
            self._import_highlights(item, media_id)

        if counts.get("reviewCount"):
            self._import_notes_for_book(item, media_id)

    def _import_highlights(self, item, media_id):
        """Store the highlights of one book."""
        try:
            data = self._call("/book/bookmarklist", bookId=media_id)
        except WeReadAuthError:
            raise
        except WeReadError as error:
            logger.warning("WeRead highlights for %s unavailable: %s", media_id, error)
            return

        chapters = {}
        for chapter in data.get("chapters") or []:
            if isinstance(chapter, dict) and chapter.get("chapterUid") is not None:
                chapters[chapter["chapterUid"]] = chapter.get("title") or ""

        stored = 0
        for entry in data.get("updated") or []:
            if not isinstance(entry, dict):
                continue

            text = (entry.get("markText") or "").strip()
            range_ = (entry.get("range") or "").strip()

            # Without a text and without a range there is nothing to keep
            if not text and not range_:
                continue

            chapter = chapters.get(entry.get("chapterUid"), "") or ""
            _, created = Highlight.objects.get_or_create(
                item=item,
                chapter=chapter[:255],
                range=range_[:255],
                text=text,
                page=None,
            )
            stored += int(created)

        logger.info("WeRead stored %s new highlights for %s", stored, media_id)

    def _import_notes_for_book(self, item, media_id):
        """Store the notes of one book, walking the cursor pages."""
        stored = 0

        try:
            data = self._call("/review/list/mine", bookid=media_id, count=PAGE_SIZE)
        except WeReadAuthError:
            raise
        except WeReadError as error:
            logger.warning("WeRead notes for %s unavailable: %s", media_id, error)
            return

        for _ in range(MAX_REVIEW_PAGES):
            reviews = data.get("reviews") or []

            for entry in reviews:
                review = entry.get("review") if isinstance(entry, dict) else None
                if not isinstance(review, dict):
                    continue

                content = (review.get("content") or "").strip()
                abstract = (review.get("abstract") or "").strip()

                if not content and not abstract:
                    continue

                chapter = review.get("chapterTitle") or review.get("chapterName") or ""
                _, created = Note.objects.get_or_create(
                    item=item,
                    content=content,
                    abstract=abstract,
                    chapter=chapter[:255],
                )
                stored += int(created)

            if not data.get("hasMore") or not reviews:
                break

            try:
                data = self._call(
                    "/review/list/mine",
                    bookid=media_id,
                    count=PAGE_SIZE,
                    synckey=data.get("synckey"),
                )
            except WeReadAuthError:
                raise
            except WeReadError as error:
                logger.warning(
                    "WeRead notes for %s stopped early: %s",
                    media_id,
                    error,
                )
                break

        logger.info("WeRead stored %s new notes for %s", stored, media_id)
