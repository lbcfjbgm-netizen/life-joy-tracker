"""Import a public Douban reading list into Yamtrack.

Douban has no export file and its API is gone, so the reading list is read off
the same pages a browser shows:

- ``/people/<user>/collect`` is what the user has read
- ``/people/<user>/do`` is what the user is reading
- ``/people/<user>/wish`` is what the user wants to read

Each page holds 15 books and pages are walked until one comes back empty. A
private (or misspelled) list answers with a login page instead, which has no
book entries at all: that raises a clear error instead of silently importing
nothing.
"""

import logging
import re
import time
from collections import defaultdict

import requests
from django.apps import apps
from django.conf import settings
from django.utils import timezone

import app
from app import image_relay
from app.models import MediaTypes, Sources, Status
from app.providers import douban, services
from app.providers.douban import BLOCKED_STATUS
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError

logger = logging.getLogger(__name__)

SHELF_URL = "https://book.douban.com/people/{user}/{shelf}"
SHELVES = (
    ("collect", Status.COMPLETED),
    ("do", Status.IN_PROGRESS),
    ("wish", Status.PLANNING),
)
BOOKS_PER_PAGE = 15
# Douban blocks clients that hammer it, and the block lasts hours. A whole
# reading list is a few hundred book pages, so the importer is slower than the
# provider default instead of faster.
REQUEST_DELAY = 2.5
MAX_PAGES = 100
IMPORT_NOTE = "Imported from Douban"

RE_SUBJECT_ITEM = re.compile(r'<li class="subject-item"[\s\S]*?</li>')
RE_TITLE_ATTR = re.compile(r'title="([^"]*)"')
RE_SUBJECT_ID = re.compile(r"/subject/(\d+)/")
RE_RATING = re.compile(r'class="rating_nums">([^<]+)<')
RE_PIC = re.compile(r'<img src="([^"]+)"')
# A private list is a login wall, which is much shorter than a real list page
RE_LOGIN_WALL = re.compile(r'action="[^"]*login|id="lzform"|name="form_password"')
LOGIN_WALL_MAX_BYTES = 60_000
# Yamtrack scores on 0-10, Douban's own star rating on 1-5
SCORE_MAX = 10
STARS_MAX = 5


def _score_from_stars(raw):
    """Turn the rating of a list entry into a Yamtrack score.

    Douban rates books with 1 to 5 stars, Yamtrack with 0 to 10, so the stars are
    doubled. A value above 5 was already on the ten point scale (the community
    average is shown that way), it is kept as is instead of being doubled into an
    impossible score.
    """
    if raw is None:
        return None

    score = float(raw)
    if score <= STARS_MAX:
        score *= 2

    return max(0.0, min(score, SCORE_MAX))


def importer(username, user, mode):
    """Import a Douban reading list for a user."""
    return DoubanImporter(username, user, mode).import_data()


class DoubanImporter:
    """Class to handle importing a public Douban reading list."""

    def __init__(self, username, user, mode):
        """Initialize the importer with the Douban username, user and mode."""
        self.username = username
        self.user = user
        self.mode = mode
        self.warnings = []

        self.existing_media = helpers.get_existing_media(user)
        # Track existing media changed in "new" mode, by primary key
        self.to_update = defaultdict(dict)
        # Track media IDs to delete in overwrite mode
        self.to_delete = defaultdict(lambda: defaultdict(set))
        # Track bulk creation lists for each media type
        self.bulk_media = defaultdict(list)

        logger.info(
            "Initialized Douban importer for user %s (douban user %s) with mode %s",
            user.username,
            username,
            mode,
        )

    def import_data(self):
        """Import every book of the three public shelves."""
        imported = 0
        seen_ids = set()

        for shelf, status in SHELVES:
            books = self._fetch_shelf(shelf)
            logger.info("Douban shelf %s has %s books", shelf, len(books))

            for book in books:
                if book["media_id"] in seen_ids:
                    continue
                seen_ids.add(book["media_id"])

                try:
                    if self._process_book(book, status):
                        imported += 1
                except services.ProviderAPIError as error:
                    if getattr(error, "status_code", None) == BLOCKED_STATUS:
                        # Douban rate limited this machine, carrying on would
                        # only extend the block. Store what was gathered so far
                        # and tell the user why the rest is missing.
                        self._flush(imported)
                        msg = (
                            f"Douban stopped answering after {imported} books "
                            "because it rate limited this machine. Wait a few "
                            "hours and import again, the books already imported "
                            "are kept."
                        )
                        raise MediaImportError(msg) from error

                    logger.warning(
                        "Error processing Douban book %s - %s",
                        book.get("title"),
                        error,
                    )
                    self.warnings.append(f"{book.get('title')}: {error}")
                except Exception as error:
                    msg = f"Error processing Douban book: {book}"
                    raise MediaImportUnexpectedError(msg) from error

        self._flush()
        deduplicated = "\n".join(dict.fromkeys(self.warnings))
        return {MediaTypes.BOOK.value: imported}, deduplicated

    def _flush(self, imported=0):  # noqa: ARG002
        """Write the gathered books to the database."""
        helpers.cleanup_existing_media(self.to_delete, self.user)
        helpers.bulk_create_media(self.bulk_media, self.user)
        helpers.bulk_update_media(
            self.to_update,
            helpers.EXISTING_MEDIA_UPDATE_FIELDS,
            self.user,
        )

    def _fetch_shelf(self, shelf):
        """Return every book of one shelf, walking the pages."""
        books = []
        start = 0

        for page_number in range(1, MAX_PAGES + 1):
            page_books, is_logged_out = self._fetch_page(shelf, start)

            if is_logged_out:
                msg = (
                    f"The Douban reading list of '{self.username}' is not public. "
                    "Open the list in a browser while logged out to check it, "
                    "or make the list public in Douban's privacy settings."
                )
                raise MediaImportError(msg)

            if not page_books:
                # The last page of a list simply has no entries, only the first
                # page proves the list exists
                if page_number == 1:
                    logger.info(
                        "Douban shelf %s of %s is empty",
                        shelf,
                        self.username,
                    )
                break

            books.extend(page_books)
            start += BOOKS_PER_PAGE
            time.sleep(REQUEST_DELAY)

        return books

    def _fetch_page(self, shelf, start):
        """Return ``(books, is_logged_out)`` for one page of a shelf."""
        url = SHELF_URL.format(user=self.username, shelf=shelf)
        try:
            response = requests.get(
                url,
                params={"start": start},
                headers=douban.HEADERS,
                timeout=douban.REQUEST_TIMEOUT,
            )
        except requests.RequestException as error:
            raise services.ProviderAPIError(Sources.DOUBAN.value, error) from error

        if response.status_code in (
            requests.codes.not_found,
            requests.codes.forbidden,
        ):
            # A misspelled or hidden account answers like this
            return [], True

        try:
            response.raise_for_status()
        except requests.RequestException as error:
            raise services.ProviderAPIError(Sources.DOUBAN.value, error) from error

        html = response.content.decode("utf-8", errors="replace")
        entries = RE_SUBJECT_ITEM.findall(html)

        if not entries:
            # The nav bar of every Douban page, public or not, links to the
            # login page, so only the absence of entries says anything. A
            # private list answers with a login wall instead of the books.
            return [], bool(RE_LOGIN_WALL.search(html))

        return [self._parse_entry(entry) for entry in entries], False

    def _parse_entry(self, entry):
        """Turn one entry of the list markup into a book dict."""
        title = RE_TITLE_ATTR.search(entry)
        subject = RE_SUBJECT_ID.search(entry)
        rating = RE_RATING.search(entry)
        pic = RE_PIC.search(entry)

        score = None
        if rating and rating.group(1).strip():
            try:
                score = float(rating.group(1))
            except ValueError:
                score = None

        return {
            "media_id": subject.group(1) if subject else None,
            "title": title.group(1).strip() if title else None,
            "score": score,
            "image": pic.group(1) if pic else None,
        }

    def _resolve_details(self, book):
        """Return ``(title, image, pages)`` for a book of the list page.

        The list markup already carries the title and a cover, the detail page
        adds the page count and the large cover. Asking for one page per book is
        what Douban throttles hardest, so a failed lookup falls back to the list
        data: a tracked book without a page count beats a book missing from the
        library.
        """
        fallback_image = image_relay.localize(
            douban.large_cover(book["image"]) if book["image"] else settings.IMG_NONE,
        )

        try:
            metadata = services.get_media_metadata(
                MediaTypes.BOOK.value,
                str(book["media_id"]),
                Sources.DOUBAN.value,
            )
        except services.ProviderAPIError as error:
            if getattr(error, "status_code", None) == BLOCKED_STATUS:
                raise
            logger.warning(
                "Douban detail for %s unavailable, using the list entry: %s",
                book.get("title"),
                error,
            )
            return book["title"], fallback_image, None

        return (
            metadata["title"] or book["title"],
            metadata["image"] or fallback_image,
            metadata.get("max_progress"),
        )

    def _sync_existing(self, existing, book, status):
        """Write the shelf status, rating and progress onto a tracked book.

        Only the fields of ``helpers.EXISTING_MEDIA_UPDATE_FIELDS`` are touched,
        so this never undoes something the user typed by hand: an unrated book
        keeps its rating, and the status only moves forward. The page count is
        read from the item when it is already known: asking Douban for one more
        page per tracked book is what gets this machine blocked.
        """
        values = {
            # Douban's list page rates with stars, Yamtrack with ten points
            "score": _score_from_stars(book["score"]),
            "status": status.value,
            # Only a finished book is read cover to cover. Zero is a no-op here
            # because a sync never lowers the progress already tracked.
            "progress": (existing.item.total_pages or 0)
            if status == Status.COMPLETED
            else 0,
        }
        return helpers.sync_existing_media(existing, values)

    def _process_book(self, book, status):
        """Store one book and its tracking entry, return whether it was new."""
        if not book["media_id"]:
            self.warnings.append(f"{book['title']}: no Douban subject id found.")
            return False

        media_id = str(book["media_id"])

        existing = self.existing_media[MediaTypes.BOOK.value][Sources.DOUBAN.value].get(
            media_id,
        )
        if existing and self.mode == "new":
            # The book is already tracked, so only what the shelf tells about it
            # is written onto the existing entry, then the flush saves it.
            if self._sync_existing(existing, book, status):
                self.to_update[MediaTypes.BOOK.value][existing.pk] = existing
            else:
                logger.debug("Existing Douban book %s is up to date", media_id)

            return False

        if existing and self.mode == "overwrite":
            self.to_delete[MediaTypes.BOOK.value][Sources.DOUBAN.value].add(media_id)

        title, image, pages = self._resolve_details(book)

        item, _ = app.models.Item.objects.update_or_create(
            media_id=media_id,
            source=Sources.DOUBAN.value,
            media_type=MediaTypes.BOOK.value,
            defaults={
                "title": title,
                "image": image,
            },
        )

        pages = pages or 0
        values = {
            # Douban only shows the user's own star rating on a list entry when
            # the user rated the book, so most entries have none. The book score
            # from the detail page is not a user score and is kept out of this
            # field, an absent rating stays absent instead of being invented.
            "score": book["score"],
            "status": status.value,
            # Only a finished book is read cover to cover
            "progress": pages if status == Status.COMPLETED else 0,
            "notes": IMPORT_NOTE,
        }

        model = apps.get_model(app_label="app", model_name=MediaTypes.BOOK.value)
        instance = model(item=item, user=self.user, **values)
        instance._history_date = timezone.now()
        self.bulk_media[MediaTypes.BOOK.value].append(instance)
        return True
