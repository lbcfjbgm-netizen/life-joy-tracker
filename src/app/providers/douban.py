"""Douban Books provider.

Douban has no usable public API any more: ``api.douban.com/v2/book/*`` is gone
and the RSS feeds are dead. What still answers is the site itself, so this
provider reads the same pages a browser would:

- ``/j/subject_suggest`` returns JSON suggestions (books, films, music mixed)
- ``/subject/<id>/`` returns the book page, which is parsed with regexes

Every request has to look like a browser: Douban rejects a bare request and,
more importantly, it hands out cover images only when the referer is a Douban
page. The cover read through this site is therefore served by
``app.image_relay`` instead of being linked directly.

Douban throttles aggressively, so requests are made one at a time with a pause
between them. Going faster gets the IP blocked for hours.
"""

import logging
import re
import time
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.cache import cache

from app import helpers, image_relay
from app.models import MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

SEARCH_URL = "https://book.douban.com/j/subject_suggest"
SUBJECT_URL = "https://book.douban.com/subject/{media_id}/"
TITLE_URL = "https://book.douban.com/j/subject/{media_id}"

# Douban wants a browser on the other end and a Douban page as the referer
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://book.douban.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
REQUEST_TIMEOUT = 30
# Seconds to wait between two calls to Douban. Douban blocks clients that ask
# too often, and a block lasts hours, so this is deliberately slow: a search
# costs one request, opening a book one more.
REQUEST_DELAY = 3
# Douban answers a blocked client with 403 and keeps the block for a while
BLOCKED_STATUS = 403
BLOCKED_MESSAGE = (
    "豆瓣暂时拒绝了本机的请求（HTTP 403），通常是短时间内访问太多被限流了。"  # noqa: RUF001
    "等几个小时再试就好，已经导入的书不会丢。"  # noqa: RUF001
)

# Douban serves three image shards; img9 answers with an HTML page, not an image
USABLE_IMAGE_HOSTS = ("img1.doubanio.com", "img2.doubanio.com", "img3.doubanio.com")
SMALL_COVER = "s/public"
LARGE_COVER = "l/public"

BOOK_TYPE = "b"

RE_TITLE = re.compile(r'<span property="v:itemreviewed">([^<]+)</span>')
RE_SCORE = re.compile(r'property="v:average">([^<]+)<')
RE_VOTES = re.compile(r'property="v:votes">([^<]+)<')
RE_COVER = re.compile(r'<img src="(https://img[^"]+)"[^>]*rel="v:photo"')
RE_INTRO = re.compile(r'<div class="intro">\s*<p>([\s\S]{0,2000}?)</p>')
RE_INFO_BLOCK = re.compile(r'<div id="info"[\s\S]{0,4000}?</div>')
# A label line of the info block, e.g. "出版社: 重庆出版社"
RE_LABEL = re.compile(r"^([\u4e00-\u9fffA-Za-z]+)\s*:\s*(.*)$")


def handle_error(error):
    """Raise a provider error the way the other providers do."""
    response = getattr(error, "response", None)
    if getattr(response, "status_code", None) == BLOCKED_STATUS:
        raise services.ProviderAPIError(
            Sources.DOUBAN.value,
            error,
            BLOCKED_MESSAGE,
        ) from error

    raise services.ProviderAPIError(Sources.DOUBAN.value, error) from error


def get(url, **kwargs):
    """Fetch a Douban page, slowing down between calls.

    ``requests`` is used directly because the shared ``api_request`` helper
    always parses the body as JSON, and the book page is HTML.
    """
    time.sleep(REQUEST_DELAY)
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, **kwargs)

    if response.status_code == BLOCKED_STATUS:
        # Being rate limited is a problem the user can act on, so it is called
        # out instead of surfacing as a bare HTTP error
        logger.warning("Douban rate limited this machine: %s", url)

    return response


def large_cover(url):
    """Return the big version of a Douban cover URL."""
    if not url:
        return settings.IMG_NONE

    host = urlparse(url).hostname or ""
    if host not in USABLE_IMAGE_HOSTS:
        return settings.IMG_NONE

    return url.replace(SMALL_COVER, LARGE_COVER)


def extract_id(url):
    """Return the subject id out of a Douban book URL."""
    match = re.search(r"/subject/(\d+)", url or "")
    return match.group(1) if match else None


def search(query, page):
    """Search Douban books and return a page of results."""
    cache_key = f"search_{Sources.DOUBAN.value}_{MediaTypes.BOOK.value}_{query}_{page}"
    data = cache.get(cache_key)
    if data is not None:
        return data

    try:
        response = get(SEARCH_URL, params={"q": query})
        response.raise_for_status()
    except requests.RequestException as error:
        handle_error(error)

    # The response has no charset in its headers, decoding it as text gives
    # mojibake, the body is UTF-8
    try:
        entries = response.json()
    except ValueError:
        entries = []

    # Films and music come back mixed with the books
    books = [
        entry
        for entry in entries
        if entry.get("type") == BOOK_TYPE and entry.get("id")
    ]

    results = [
        {
            "media_id": str(entry["id"]),
            "source": Sources.DOUBAN.value,
            "media_type": MediaTypes.BOOK.value,
            "title": entry.get("title") or "",
            "image": image_relay.localize(large_cover(entry.get("pic"))),
        }
        for entry in books
    ]

    per_page = settings.PER_PAGE
    start = (page - 1) * per_page
    data = helpers.format_search_response(
        page,
        per_page,
        len(results),
        results[start : start + per_page],
    )

    cache.set(cache_key, data)
    return data


def clean(text):
    """Strip the markup and whitespace Douban wraps values in."""
    if text is None:
        return None
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def parse_info_block(html):
    """Return the ``label: value`` pairs of the book info block.

    Douban is inconsistent about where the value sits: ``出版年`` has it on the
    same line, while ``作者`` and ``出版社`` put it on the line below. The block
    is therefore turned into plain text and walked line by line, borrowing the
    next line whenever a label has nothing after its colon.
    """
    block_match = RE_INFO_BLOCK.search(html)
    if not block_match:
        return {}

    text = re.sub(r"<br\s*/?>", "\n", block_match.group(0))
    text = re.sub(r"<[^>]+>", "", text).replace("&nbsp;", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    info = {}
    index = 0
    while index < len(lines):
        match = RE_LABEL.match(lines[index])
        if not match:
            index += 1
            continue

        label, value = match.group(1), match.group(2).strip()
        if not value and index + 1 < len(lines):
            value = lines[index + 1]
            index += 1
        info[label] = value or None
        index += 1
    return info


def first_int(text):
    """Return the first integer in a string, or None."""
    match = re.search(r"\d+", text or "")
    return int(match.group(0)) if match else None


def book(media_id):
    """Return the metadata of a Douban book."""
    cache_key = f"{Sources.DOUBAN.value}_{MediaTypes.BOOK.value}_{media_id}"
    data = cache.get(cache_key)
    if data is not None:
        return data

    url = SUBJECT_URL.format(media_id=media_id)
    try:
        response = get(url)
        response.raise_for_status()
    except requests.RequestException as error:
        handle_error(error)

    html = response.content.decode("utf-8", errors="replace")

    title_match = RE_TITLE.search(html)
    if not title_match:
        # A private or removed book answers with a login page, which has no
        # title in it. That is a missing book, not an empty result.
        services.raise_not_found_error(Sources.DOUBAN.value, media_id, "book")

    info = parse_info_block(html)

    score_match = RE_SCORE.search(html)
    votes_match = RE_VOTES.search(html)
    cover_match = RE_COVER.search(html)
    intro_match = RE_INTRO.search(html)

    score = None
    if score_match and score_match.group(1).strip():
        try:
            score = float(score_match.group(1))
        except ValueError:
            score = None

    pages = first_int(info.get("页数"))
    author = clean(info.get("作者"))
    publisher = clean(info.get("出版社"))
    isbn = clean(info.get("ISBN"))
    publish_date = clean(info.get("出版年"))

    data = {
        "media_id": str(media_id),
        "source": Sources.DOUBAN.value,
        "source_url": url,
        "media_type": MediaTypes.BOOK.value,
        "title": title_match.group(1).strip(),
        "max_progress": pages,
        "image": image_relay.localize(
            large_cover(cover_match.group(1) if cover_match else None),
        ),
        "synopsis": (
            clean(intro_match.group(1)) if intro_match else "No synopsis available."
        ),
        "genres": [],
        "score": score,
        "score_count": int(votes_match.group(1)) if votes_match else None,
        "details": {
            "author": author,
            "publishers": [publisher] if publisher else [],
            "isbn": isbn,
            "number_of_pages": pages,
            "publish_date": publish_date,
        },
        "related": {},
    }

    cache.set(cache_key, data)
    return data
