"""WeRead (微信读书) book provider.

Only the public, keyless part of the WeRead web API is used here. Everything
below was checked by hand against the live site from this machine, and the
notes record what actually came back, because several obvious approaches turn
out not to work without a logged in account.

Search: ``GET /web/search/global`` needs no credentials. It answers with a
JSON body holding a ``books`` list plus ``totalCount``/``hasMore``. Pagination
is genuinely server side: ``maxIdx`` is honoured, so page 1 and page 2 return
disjoint ``bookId`` sets (three pages of ``count=24`` gave 72 distinct IDs).
The server caps ``count`` at 50, so ``settings.PER_PAGE`` (24) is used as-is
and ``maxIdx`` is ``(page - 1) * count``. Only ``totalCount`` is unreliable:
it reported 57 for the first two pages of ``三体`` and then jumped to 10329,
so it is treated as a hint rather than a fact.

Detail: ``GET /web/book/info?bookId=...`` does **not** work anonymously. It
answers HTTP 200 with ``{"errCode": -2010, "errMsg": "用户不存在"}`` ("user does
not exist") for every book. That was reproduced with a fresh session cookie,
with guest ``wr_vid``/``wr_skey`` cookies, with a ``Referer`` pointing at the
real book page, and on the ``x.weread.qq.com`` (errCode ``-2012``) and
``i.weread.qq.com`` (HTTP 401) mirrors. Mining the site's JS bundles found no
public per-book endpoint, and the other candidate paths (``/web/book/detail``,
``/web/book/infos``, ``/web/book/store``, ``/web/book/readdata``) are all 404.

The one public page that does carry full metadata is the deep link
``/book-detail?type=1&v=<hash>``, which renders ``bookInfo`` server side. Its
``v`` hash embeds the book ID in a scrambled, non-hex form that cannot be
derived from the ID (it is not a plain hash of the digits, and the layout is
different for 10-digit IDs), so it cannot be reached from a bare ``bookId``.

Because of that, a bare book ID cannot be resolved from scratch. What makes
``book()`` work in practice is that ``search()`` already receives the complete
``bookInfo`` of every result, so those payloads are kept in the cache and
reused: opening a book from a search result never needs a second lookup.

Personal data is out of scope on purpose: the shelf, the reading progress and
the notes need the same login and always answer ``-2010`` anonymously, so
``/web/shelf/sync`` and ``/web/book/readinfo`` are never called.
"""

import logging

import requests
from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.models import Item, MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

SEARCH_URL = "https://weread.qq.com/web/search/global"
BOOK_INFO_URL = "https://weread.qq.com/web/book/info"
BOOK_DETAIL_URL = "https://weread.qq.com/web/bookDetail/{}"

# The keyless endpoints answer a bare python-requests agent too, but the site
# sits behind a front end that expects a browser, so every call identifies as
# a normal desktop client.
headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://weread.qq.com/",
}

# The server ignores anything above this and always returns 50 books.
MAX_RESULTS_PER_PAGE = 50

# How many results are asked for when a book is looked up by its ID.
LOOKUP_RESULTS = 10


def handle_error(error):
    """Handle WeRead API errors."""
    raise services.ProviderAPIError(
        Sources.WEREAD.value,
        error,
    )


def request_json(url, params):
    """Return the parsed JSON of a WeRead call.

    ``services.api_request`` is used instead of a bare ``requests.get`` so the
    call goes through the project wide rate limited session, inherits the
    shared ``settings.REQUEST_TIMEOUT``, and reports failures under the same
    provider label as every other source instead of a naked requests error.
    """
    try:
        return services.api_request(
            Sources.WEREAD.value,
            "GET",
            url,
            params=params,
            headers=headers,
        )
    except requests.RequestException as error:
        handle_error(error)


def get_book_infos(response):
    """Return the ``bookInfo`` payloads of a search response.

    Entries without a usable ``bookInfo`` are dropped: the endpoint mixes book
    results with other shapes and a missing payload would break every field
    access below.
    """
    books = response.get("books") or []

    return [
        book["bookInfo"]
        for book in books
        if isinstance(book, dict) and isinstance(book.get("bookInfo"), dict)
    ]


def get_image(book_info):
    """Return the cover URL of a book.

    The URL is passed through untouched. The file name carries a version and
    timestamp suffix (``.../s_yuewen_6952331784884457.jpg``) that is part of
    the path, so rebuilding or trimming the URL gives a 404. The CDN serves it
    to anonymous clients without any ``Referer``, as a JPEG of a few KB.
    """
    return book_info.get("cover") or settings.IMG_NONE


def get_synopsis(book_info):
    """Return the introduction of a book."""
    intro = book_info.get("intro")

    if not intro:
        return "No synopsis available."

    # The intro is plain text with hard line breaks while the details page
    # renders it as one paragraph, so the runs of whitespace are collapsed.
    return " ".join(str(intro).split())


def get_score(book_info):
    """Return the rating of a book on the ten point scale of Yamtrack.

    WeRead sends ``newRating`` on a hundred point scale, so ``930`` is ``9.3``.
    Books without enough ratings leave the field out entirely, which means an
    unknown score rather than a zero one.
    """
    rating = book_info.get("newRating")

    if not isinstance(rating, (int, float)) or rating <= 0:
        return None

    return round(rating / 100, 1)


def get_score_count(book_info):
    """Return the number of ratings of a book, or None when it has none."""
    count = book_info.get("newRatingCount")

    if not isinstance(count, int) or count <= 0:
        return None

    return count


def get_source_url(book_info, media_id):
    """Return the reading link of a book, falling back to its detail page."""
    return (
        book_info.get("readerLink")
        or book_info.get("deepLink")
        or BOOK_DETAIL_URL.format(media_id)
    )


def get_details(book_info):
    """Return the extra metadata rows of a book."""
    publisher = book_info.get("publisher")

    return {
        "author": book_info.get("author"),
        "publishers": [publisher] if publisher else [],
    }


def get_total_results(response, page, count, seen):
    """Return a result count the pagination controls can trust.

    ``totalCount`` is not dependable: for one query it reported 57 on the
    first two pages and 10329 from the third on. It is only accepted when it
    is at least the number of books actually handed back, and when the server
    says there is nothing more the count is clamped to what was seen so the
    pager does not advertise empty pages.
    """
    total = response.get("totalCount")
    seen_so_far = (page - 1) * count + seen

    # A count that is missing or too small to cover this page says nothing
    # useful, and a count past the last page would advertise empty pages.
    if not isinstance(total, int) or total < seen:
        return seen_so_far

    if not response.get("hasMore") and total > seen_so_far:
        return seen_so_far

    return total


def search(query, page):
    """Search WeRead books with the public web search endpoint.

    The response holds the books under ``books``, each one wrapping the real
    payload in a ``bookInfo`` object. The server honours the ``maxIdx`` offset,
    so a page is a real slice of the result set and nothing is sliced locally.
    """
    cache_key = f"search_{Sources.WEREAD.value}_{MediaTypes.BOOK.value}_{query}_{page}"
    data = cache.get(cache_key)
    if data is not None:
        return data

    count = min(settings.PER_PAGE, MAX_RESULTS_PER_PAGE)
    params = {
        "keyword": query,
        "maxIdx": (page - 1) * count,
        "count": count,
    }

    response = request_json(SEARCH_URL, params)

    book_infos = [
        book_info
        for book_info in get_book_infos(response)
        if book_info.get("bookId") and book_info.get("title")
    ]

    results = [
        {
            "media_id": str(book_info["bookId"]),
            "source": Sources.WEREAD.value,
            "media_type": MediaTypes.BOOK.value,
            "title": book_info["title"],
            "image": get_image(book_info),
        }
        for book_info in book_infos
    ]

    # The complete payload of every result is kept so that opening one of them
    # needs no further request, see the module docstring for why that matters.
    cache.set_many(
        {
            book_info_cache_key(book_info["bookId"]): book_info
            for book_info in book_infos
        },
    )

    data = helpers.format_search_response(
        page,
        count,
        get_total_results(response, page, count, len(results)),
        results,
    )

    cache.set(cache_key, data)
    return data


def book_info_cache_key(media_id):
    """Return the cache key holding the raw ``bookInfo`` of a book."""
    return f"{Sources.WEREAD.value}_book_info_{media_id}"


def book(media_id):
    """Get the metadata of a WeRead book.

    The detail endpoint cannot be used anonymously and no public route turns a
    bare book ID back into a book, so the lookup goes through every source
    that can still answer, in order of cost:

    1. the ``bookInfo`` that ``search()`` already fetched for this book,
    2. the documented detail endpoint, which is kept for the day WeRead opens
       it or a logged in cookie is supplied,
    3. a search for the ID, matched exactly.

    When none of them knows the book a 404 is raised rather than returning a
    half empty entry built from guesses.
    """
    cache_key = f"{Sources.WEREAD.value}_{MediaTypes.BOOK.value}_{media_id}"
    data = cache.get(cache_key)
    if data is not None:
        return data

    # The cache is a Redis instance: a restart, an eviction or the 24h TTL can
    # empty it, so it must never be the only place a book can be described
    # from. The importer already stored the title, cover and synopsis on the
    # item row, which survives any restart, so read those before giving up.
    from_db = book_from_db(media_id)
    if from_db is not None:
        cache.set(cache_key, from_db)
        return from_db

    book_info = lookup_book_info(media_id)

    if not book_info or not book_info.get("title"):
        services.raise_not_found_error(
            Sources.WEREAD.value,
            media_id,
            "book",
        )

    data = {
        "media_id": str(media_id),
        "source": Sources.WEREAD.value,
        "source_url": get_source_url(book_info, media_id),
        "media_type": MediaTypes.BOOK.value,
        "title": book_info["title"],
        "max_progress": None,
        "image": get_image(book_info),
        "synopsis": get_synopsis(book_info),
        "genres": [],
        "score": get_score(book_info),
        "score_count": get_score_count(book_info),
        "details": get_details(book_info),
        "related": {},
    }

    cache.set(cache_key, data)
    return data


def book_from_db(media_id):
    """Describe a book from the row the importer already stored.

    The public endpoints cannot turn a bare book ID back into a book, and the
    only component holding an API key is the importer, so once its cache entry
    is gone there is nothing left to look the book up with. The item row does
    keep the title, the cover and the synopsis though, and that is enough for
    the details page to render instead of answering 500.

    Returns None when no such item exists, so the caller can fall through to
    the network paths.
    """
    item = Item.objects.filter(
        media_id=str(media_id),
        source=Sources.WEREAD.value,
        media_type=MediaTypes.BOOK.value,
    ).first()

    if item is None or not item.title:
        return None

    synopsis = item.synopsis or "No synopsis available."

    return {
        "media_id": str(media_id),
        "source": Sources.WEREAD.value,
        "source_url": f"https://weread.qq.com/web/bookDetail/{media_id}",
        "media_type": MediaTypes.BOOK.value,
        "title": item.title,
        "max_progress": None,
        "image": item.image or settings.IMG_NONE,
        "synopsis": synopsis,
        "genres": [],
        "score": None,
        "score_count": None,
        "details": {},
        "related": {},
    }


def lookup_book_info(media_id):
    """Return the ``bookInfo`` of a book, or None when it cannot be found."""
    cached_info = cache.get(book_info_cache_key(media_id))
    if cached_info:
        return cached_info

    book_info = fetch_book_info(media_id)
    if book_info and book_info.get("title"):
        cache.set(book_info_cache_key(media_id), book_info)
        return book_info

    return search_book_info(media_id)


def fetch_book_info(media_id):
    """Return the payload of the detail endpoint, or None when it is gated.

    The endpoint answers a logged out caller with ``errCode`` set and no book
    at all, so that response is reported as "no data" instead of an error, and
    so is a failed request: this endpoint is a bonus path, and a book that the
    search cache and the search endpoint can still describe must not be lost
    just because the gated call misbehaved.
    """
    try:
        response = request_json(BOOK_INFO_URL, {"bookId": str(media_id)})
    except services.ProviderAPIError as error:
        logger.warning("WeRead book info unavailable for %s: %s", media_id, error)
        return None

    if not isinstance(response, dict):
        return None

    if response.get("errCode"):
        logger.warning(
            "WeRead book info refused %s: %s (errCode %s)",
            media_id,
            response.get("errMsg"),
            response.get("errCode"),
        )
        return None

    return response


def search_book_info(media_id):
    """Return the ``bookInfo`` of a book found through the search endpoint."""
    response = request_json(
        SEARCH_URL,
        {
            "keyword": str(media_id),
            "maxIdx": 0,
            "count": LOOKUP_RESULTS,
        },
    )

    return next(
        (
            book_info
            for book_info in get_book_infos(response)
            if str(book_info.get("bookId")) == str(media_id)
        ),
        None,
    )
