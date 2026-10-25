from datetime import timedelta

from django.utils import timezone
from django.utils.text import slugify

from app.models import BasicMedia, MediaTypes, Status, home_media_is_hidden

# 首页「在读」的判定：至少读了 30 分钟、两周内有过进展、并且没被标记读完。
# 115 本 In progress 里 99 本累计不到 10 分钟（导入时留下的空壳记录），
# 只按状态列出来会把书架整个淹掉，所以书的门槛比游戏严。
BOOK_ACTIVE_MIN_READING_SECONDS = 1800
BOOK_ACTIVE_DAYS = 14


def build_home_section(key, media_types):
    """Build home section payload."""
    return {
        "key": key,
        "id": slugify(key),
        "media_types": media_types,
        "count": sum(media_list["total"] for media_list in media_types.values()),
    }


def is_active_in_progress_media(media):
    """Return True when media still has released backlog."""
    if not _is_incoming_media(media):
        return True

    return media.max_progress is not None and media.progress < media.max_progress


def get_home_media_types(
    request,
    sort_by,
    section_key,
    items_limit,
    specific_media_type=None,
    *,
    hide_unreleased=False,
):
    """Return media types for a home section, or one type's load-more page.

    ``items_limit`` is always passed down, so the manager can order plain rows
    and build model instances only for the cards that get rendered. The
    hide-unreleased split happens inside that same pass instead of filtering a
    fully materialised shelf here: on this library that shelf is 2000+ games and
    building the objects cost more than everything else on the page combined.

    On the in-progress shelf the books are narrowed to the ones actually being
    read (see ``_active_book_shelf``); every other shelf is the manager's.
    """
    list_by_type = BasicMedia.objects.get_home_status(
        user=request.user,
        status=section_key,
        sort_by=sort_by,
        items_limit=items_limit,
        specific_media_type=specific_media_type,
        hide_unreleased=hide_unreleased,
    )

    if section_key == Status.IN_PROGRESS.value and specific_media_type in (
        None,
        MediaTypes.BOOK.value,
    ):
        _replace_with_active_books(
            list_by_type,
            request.user,
            sort_by,
            items_limit,
            specific_media_type is not None,
            hide_unreleased,
        )

    return list_by_type


def _replace_with_active_books(
    list_by_type,
    user,
    sort_by,
    items_limit,
    load_more,
    hide_unreleased,
):
    """Swap the manager's book shelf for the "actually reading" one.

    The book conditions are applied on the queryset *before* the page slice.
    Filtering the rendered 14 cards afterwards would report a total that
    disagrees with the cards, and would also lose the qualifying rows that fell
    outside the first page.
    """
    books = _active_book_shelf(user, sort_by, hide_unreleased)
    total = len(books)
    if not total:
        list_by_type.pop(MediaTypes.BOOK.value, None)
        return

    page_start = items_limit if load_more else 0
    list_by_type[MediaTypes.BOOK.value] = {
        "items": books[page_start : page_start + items_limit],
        "total": total,
    }


def _active_book_shelf(user, sort_by, hide_unreleased):
    """Books that belong on the home page's in-progress shelf.

    Same annotations and same sort table as every other home shelf, so the
    ordering cannot drift from the manager; only the row set is narrower.
    """
    queryset = BasicMedia.objects.get_media_list(
        user=user,
        media_type=MediaTypes.BOOK.value,
        status_filter=Status.IN_PROGRESS.value,
        sort_filter=None,
    ).filter(
        reading_seconds__gte=BOOK_ACTIVE_MIN_READING_SECONDS,
        progressed_at__gte=timezone.now() - timedelta(days=BOOK_ACTIVE_DAYS),
        end_date__isnull=True,
    )

    books = list(queryset)
    if not books:
        return []

    manager = BasicMedia.objects
    manager.annotate_max_progress(books, MediaTypes.BOOK.value)
    manager._annotate_next_event(books)

    if hide_unreleased:
        books = [
            book
            for book in books
            if not home_media_is_hidden(
                Status.IN_PROGRESS.value,
                bool(book.next_event),
                book.max_progress,
                book.progress,
            )
        ]

    return manager._sort_home_media(books, sort_by)


def _is_incoming_media(media):
    """Return True when media has a real upcoming release."""
    return bool(media.next_event and not media.next_event.is_max_datetime)
