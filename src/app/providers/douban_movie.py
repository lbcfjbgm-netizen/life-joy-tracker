"""Douban Movie provider.

Douban has no supported public API, but the mobile web app talks to one that
still answers without a login: the ``rexxar`` API behind ``m.douban.com``. Two
things are needed to be let in, and both are load bearing:

- an iPhone user agent, a desktop one gets an HTML page instead of JSON
- ``Referer: https://m.douban.com/``, a missing referer is rejected

What was checked by hand from this machine on 2026-10-22:

``GET /rexxar/api/v2/movie/<id>``
    200, 77 fields, no login and no rate limit hit at one call every three
    seconds. ``subtype`` says whether the subject is a film or a show: a show
    id answers 200 on the ``movie`` path too, it just carries
    ``subtype: tv``, so the subtype is what decides, not the path.

``GET /rexxar/api/v2/tv/<id>``
    Same payload for a show. It is only used as a second look to tell a
    mistyped id (404, ``traversal_error``) apart from a show.

``GET /j/subject_suggest?q=...``
    The JSON search behind the Douban search box: ``id``, ``title``, ``img``,
    ``year`` and a ``type`` of ``movie``/``tv``. Shows are dropped here, this
    provider only handles films.

Two field names collide on purpose and must not be mixed up:

- ``interest.rating.value`` is the **user's own** 1-5 star rating
- ``subject.rating.value`` is the **community average**, 0-10

The star rating is not stored as a score: Yamtrack's score of a watched film is
the film's own reputation, so the community average is what lands there, while
the user's stars are kept as a note by the importer.
"""

import logging
import os
import re
import time
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.cache import cache

from app import helpers, image_relay
from app.models import Item, MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

BASE_URL = "https://m.douban.com/rexxar/api/v2"
MOVIE_PATH = "movie"
TV_PATH = "tv"
SEARCH_URL = "https://movie.douban.com/j/subject_suggest"
SUBJECT_URL = "https://movie.douban.com/subject/{media_id}/"

# Without an iPhone agent Douban answers the mobile site instead of the API
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 "
    "Safari/604.1"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://m.douban.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
REQUEST_TIMEOUT = 30

# Douban throttles: the anonymous budget is reported at about 45 calls a minute,
# but that figure is not what this machine actually sees.
#
# History of this number, so it is not tightened again by mistake:
#   3.2s (~19/min) ran 12/12 fine, then a full 476-movie run was cut off with
#                 ``code 1309 subject_ip_rate_limit`` after ~50 detail calls,
#                 so the real ceiling sits below the reported 45/min.
#   8s   (~7.5/min) never tripped the limit, but 410 movies take ~55 minutes.
#   4.0s (15/min)  the agreed speed: under the 19/min that already failed once,
#                 and 410 movies finish in about 27 minutes.
REQUEST_DELAY = 4.0
_last_request = 0.0

# Douban answers a blocked client with 403 and keeps the block for a while
BLOCKED_STATUS = 403
BLOCKED_MESSAGE = (
    "豆瓣暂时拒绝了本机的请求（HTTP 403），通常是短时间内访问太多被限流了。"  # noqa: RUF001
    "等几个小时再试就好，已经导入的条目不会丢。"  # noqa: RUF001
)

# The throttle above is not the only limit: after roughly fifty detail calls in
# a row Douban starts answering HTTP **400** with a body instead of a page:
# ``{"msg": "subject_ip_rate_limit", "code": 1309, ...}``. It is not the 403
# that a hard block gives, so it has to be recognised by the body. Nothing is
# wrong with the request, so retrying later works and the entry is not broken.
RATE_LIMIT_STATUS = 400
RATE_LIMIT_CODE = 1309
RATE_LIMIT_MARKER = "subject_ip_rate_limit"
RATE_LIMIT_MESSAGE = (
    "豆瓣限制了这个网络的访问（IP 请求太频繁），剩下的稍后再导就行，"  # noqa: RUF001
    "已经导入的条目不会丢。"  # noqa: RUF001
)
# How long the importer waits once for the limit to lift before giving up.
RATE_LIMIT_WAIT = 120

# An empty answer is cached briefly instead of for the usual day. Douban does not
# only refuse with a 400; it also answers ``200`` with ``[]`` once the IP has
# asked too often, and that answer looks exactly like "nothing matched". Caching
# it for a day would keep a throttled search empty until tomorrow.
EMPTY_SEARCH_CACHE_TIMEOUT = 120

# The film poster variant the site itself shows, one step above the search thumb
SMALL_POSTER = "/s_ratio_poster/"
LARGE_POSTER = "/m_ratio_poster/"

# img9 answers with an HTML page instead of an image, referer or not. The very
# same picture is served by img3 and img2 under the same path, checked on ten
# img9 posters (10 of 10 came back as image/jpeg). Rewriting the shard keeps the
# poster instead of dropping it.
BROKEN_IMAGE_HOSTS = {"img9.doubanio.com": "img3.doubanio.com"}

MOVIE_TYPE = "movie"
SERIES_SUBTYPE = "tv"

# Yamtrack scores out of ten, Douban's community average already does
SCORE_MAX = 10

# How many cast members the details page has room for. TMDB caps its own list
# at the same number, the full count still goes to ``total_cast_count``.
CAST_DISPLAY_LIMIT = 30

RE_MINUTES = re.compile(r"(\d+)\s*分钟")
RE_HOURS = re.compile(r"(\d+)\s*小时")
RE_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
RE_YEAR = re.compile(r"^(\d{4})")


def is_rate_limited(response):
    """Return whether a response is Douban's IP rate limit.

    That limit is a ``400`` whose JSON body carries ``code: 1309`` and the text
    ``subject_ip_rate_limit``, which is why the status alone is not enough: a
    plain 400 could also be a malformed request.
    """
    if getattr(response, "status_code", None) != RATE_LIMIT_STATUS:
        return False

    text = getattr(response, "text", "") or ""
    if RATE_LIMIT_MARKER in text:
        return True

    try:
        body = response.json()
    except (ValueError, AttributeError):
        return False

    return isinstance(body, dict) and body.get("code") == RATE_LIMIT_CODE


def handle_error(error):
    """Raise a provider error the way the other providers do."""
    response = getattr(error, "response", None)

    if getattr(response, "status_code", None) == BLOCKED_STATUS:
        raise services.ProviderAPIError(
            Sources.DOUBAN.value,
            error,
            BLOCKED_MESSAGE,
        ) from error

    if is_rate_limited(response):
        raise services.ProviderAPIError(
            Sources.DOUBAN.value,
            error,
            RATE_LIMIT_MESSAGE,
        ) from error

    raise services.ProviderAPIError(Sources.DOUBAN.value, error) from error


def error_text(error):
    """Return what the error and the errors it was raised from say together.

    ``ProviderAPIError`` replaces the original message with a generic
    "There was an error contacting the Douban API", and only the chained cause
    still knows that a proxy refused the connection. The user message is built
    from the whole chain so it can name the real reason.
    """
    parts = []
    seen = set()
    current = error

    while current is not None and id(current) not in seen and len(parts) < 5:
        seen.add(id(current))
        parts.append(str(current))
        current = current.__cause__ or current.__context__

    return " ".join(part for part in parts if part).strip()


def network_error_message(error, media_label):
    """Return a message a user can understand for a failed collection read.

    The import is going to end as FAILED, so this text is what the user reads in
    the task list. It has to say two things: nothing was imported, and whether
    the local network or a proxy is the reason. A proxy that is configured but
    not running is exactly the failure that used to pass silently as
    "nothing to import", so it is named either way: no status code means the
    host was never reached, and the only local thing that can block that is the
    configured proxy.
    """
    detail = error_text(error)
    status_code = getattr(error, "status_code", None)

    if "proxy" in detail.lower():
        reason = (
            "连不上配置的代理（DOUBAN_PROXY）。代理没开就把这个变量清掉，"
            "清掉以后会直接连豆瓣。"
        )
    elif status_code is None:
        reason = (
            "本机网络或豆瓣暂时不通（如果配了 DOUBAN_PROXY，也很可能是那个"
            "代理没开）。"
        )
    else:
        reason = f"豆瓣返回了 HTTP {status_code}，不是限流也不是封禁。"

    return (
        f"豆瓣{media_label}导入没能开始：{reason}"
        f"这一次一条都没导，不是「没有新内容」。原因：{detail}"
    )


def is_blocked(error):
    """Return whether a provider error means "stop asking for a while"."""
    return is_rate_limited_error(error) or (
        getattr(error, "status_code", None) == BLOCKED_STATUS
    )


def is_rate_limited_error(error):
    """Return whether a provider error is Douban's IP rate limit.

    The rate limit is not reachable from the status code alone: it arrives as a
    ``400``, and only the body says it is a throttle rather than a bad request.
    ``ProviderAPIError`` keeps that body in its own message, so the marker is
    looked for there.
    """
    message = str(error)
    if RATE_LIMIT_MARKER in message:
        return True

    # The details of the limit were also summarised into the error message
    return RATE_LIMIT_MESSAGE in message


def throttle():
    """Wait long enough that two Douban calls are never closer than the delay.

    The importer uses this same function for its list pages, so the gap holds
    across the whole import instead of per request site.
    """
    global _last_request  # noqa: PLW0603

    elapsed = time.monotonic() - _last_request
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)
    _last_request = time.monotonic()


# When the configured proxy was last found unreachable, on the ``monotonic``
# clock. A proxy that is not running used to end every Douban import, so the
# direct connection is used instead, and that decision is remembered: paying a
# failed connection on every one of 476 films would make the import crawl.
# It is not remembered forever, because the proxy may simply have been started
# a minute after the import began.
_proxy_failed_at = 0.0
# The exact proxy that failed, so setting a different one is honoured at once
_proxy_failed_value = ""
PROXY_RETRY_AFTER = 900


def proxy_unreachable(error):
    """Return whether a request error means the proxy itself was unavailable.

    A proxy that is not listening is reported by ``requests`` as a
    ``ProxyError`` ("Unable to connect to proxy") or, depending on where it
    fails, as a plain connection or connect-timeout error. Only the second case
    needs the text of the error to tell "the proxy is down" apart from "Douban
    is down", and the word "proxy" is what tells them apart.
    """
    if isinstance(error, requests.exceptions.ProxyError):
        return True

    if isinstance(error, requests.exceptions.ConnectTimeout):
        return True

    if isinstance(error, requests.exceptions.ConnectionError):
        return "proxy" in str(error).lower()

    return False


def proxy_fallback_active(proxy):
    """Return whether ``proxy`` is the one that recently failed.

    Only the exact address that failed is skipped: pointing ``DOUBAN_PROXY`` at
    another pool must take effect immediately.
    """
    return (
        _proxy_failed_at > 0
        and proxy == _proxy_failed_value
        and time.monotonic() - _proxy_failed_at < PROXY_RETRY_AFTER
    )


def get(url, **kwargs):
    """Fetch a Douban URL, pausing before the call.

    Douban limits per IP, so a blocked machine can borrow another address: set
    ``DOUBAN_PROXY`` (``http://host:port``) and every call goes through it. It
    is read on each call rather than at import, so the importer can switch
    proxies between films without restarting anything. No proxy configured
    means the plain direct connection, exactly as before.

    A configured proxy that cannot be reached is not allowed to end an import:
    the direct connection is tried instead and the fallback is remembered, so
    only the next import pays for the dead proxy again. Should the direct
    connection fail as well, that error is raised and the run fails honestly.
    """
    global _proxy_failed_at, _proxy_failed_value  # noqa: PLW0603

    throttle()
    proxy = os.getenv("DOUBAN_PROXY", "").strip()

    if proxy and not proxy_fallback_active(proxy) and "proxies" not in kwargs:
        try:
            return _request(url, kwargs, proxy)
        except requests.RequestException as error:
            if not proxy_unreachable(error):
                raise
            _proxy_failed_at = time.monotonic()
            _proxy_failed_value = proxy
            logger.warning(
                "Douban proxy %s is not reachable (%s), falling back to a "
                "direct connection instead",
                proxy,
                error,
            )

    return _request(url, kwargs, None)


def _request(url, kwargs, proxy):
    """Send one Douban request, through ``proxy`` when one is given."""
    if proxy and "proxies" not in kwargs:
        kwargs = {**kwargs, "proxies": {"http": proxy, "https": proxy}}

    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, **kwargs)

    if response.status_code == BLOCKED_STATUS:
        logger.warning("Douban rate limited this machine: %s", url)

    return response


def fixed_poster(url):
    """Return a poster URL that actually renders, or ``IMG_NONE``.

    The img9 shard hands out an HTML page for the same path every other shard
    serves as a JPEG, so links to it are moved to img3 before anything stores
    them. The URL is then run through the image relay, because a browser asking
    Douban for a cover without a Douban referer gets ``418``.
    """
    if not url:
        return settings.IMG_NONE

    host = (urlparse(url).hostname or "").lower()
    replacement = BROKEN_IMAGE_HOSTS.get(host)
    if replacement:
        url = url.replace(f"//{host}/", f"//{replacement}/", 1)

    return image_relay.localize(url)


def large_poster(url):
    """Return the big variant of a search thumbnail, or ``IMG_NONE``."""
    if not url:
        return settings.IMG_NONE

    return fixed_poster(url.replace(SMALL_POSTER, LARGE_POSTER))


def parse_runtime(durations):
    """Return the running time of a film in whole minutes, or None.

    ``durations`` is a list of strings such as ``["145分钟"]``. A space in the
    unit (``"122 分钟"``) is seen, and a few films list one cut per region
    (``["106分钟(香港)", "98分钟(中国大陆)"]``); the first entry is the length
    Douban itself shows, so that one is used. The hour form is handled as well,
    although it was not seen in fifteen samples.
    """
    if not durations:
        return None

    text = str(durations[0])
    hours = RE_HOURS.search(text)
    minutes = RE_MINUTES.search(text)

    total = 0
    if hours:
        total += int(hours.group(1)) * 60
    if minutes:
        total += int(minutes.group(1))

    return total or None


def readable_duration(minutes):
    """Return a running time in minutes as ``2h 25m``."""
    if not minutes:
        return None

    hours, rest = divmod(int(minutes), 60)
    return f"{hours}h {rest}m" if hours > 0 else f"{rest}m"


def parse_release_date(value):
    """Return ``YYYY-MM-DD`` out of a Douban date string, or None.

    ``pubdate`` holds strings like ``"2026-07-29(中国大陆)"``. Some old films
    only carry a year (``"2005(中国大陆)"``), or a year and a month, and those
    give no real day, so the day is left out instead of being invented.
    """
    if not value:
        return None

    match = RE_ISO_DATE.match(str(value).strip())
    return match.group(0) if match else None


def release_date(subject):
    """Return the release date of a subject as ``YYYY-MM-DD``, or None.

    ``pubdate`` is the only date Douban sends: ``release_date`` is null on
    every one of the 475 subjects checked that carried no such key at all, so
    it is never read.
    """
    pubdates = subject.get("pubdate") or []
    if isinstance(pubdates, str):
        pubdates = [pubdates]

    for value in pubdates:
        parsed = parse_release_date(value)
        if parsed:
            return parsed

    return None


def get_rating(subject):
    """Return ``(score, count)`` of the community average of a film.

    This is ``subject.rating``, the 0-10 average, never the 1-5 stars of the
    caller's own interest. A film nobody rated has a value of 0, which is "not
    rated" rather than "rated zero", so it comes back as None.
    """
    rating = subject.get("rating") or {}
    value = rating.get("value")

    try:
        score = float(value)
    except (TypeError, ValueError):
        return None, None

    if score <= 0:
        return None, None

    return round(min(score, SCORE_MAX), 1), rating.get("count") or None


def get_names(people):
    """Return the names of a list of ``{"name": ...}`` entries.

    The list length changes per film (16 to 81 actors in this collection), so
    it is walked as a whole rather than by fixed positions.
    """
    names = []
    for person in people or []:
        if not isinstance(person, dict):
            continue
        name = (person.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def get_cast(actors):
    """Return the cast list shaped the way the details page renders it.

    Each card reads ``name``, ``character``, ``image`` and ``id``. Douban sends
    only the name and never a character name, so that key is left out instead
    of being filled with an empty string.
    """
    cast = []
    for name in get_names(actors):
        cast.append(
            {
                "id": None,
                "name": name,
                "image": settings.IMG_NONE,
            },
        )
    return cast


def get_genres(subject):
    """Return the genres of a film."""
    return [
        genre.strip()
        for genre in subject.get("genres") or []
        if isinstance(genre, str) and genre.strip()
    ]


def clean_text(text):
    """Strip the markup and the extra whitespace Douban wraps text in."""
    if not text:
        return None

    text = re.sub(r"<[^>]+>", " ", str(text))
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def get_synopsis(subject):
    """Return the synopsis of a film."""
    return clean_text(subject.get("intro")) or "No synopsis available."


def get_country(subject):
    """Return the countries of a film as one readable line."""
    countries = [
        country.strip()
        for country in subject.get("countries") or []
        if isinstance(country, str) and country.strip()
    ]
    return " / ".join(countries) or None


def get_languages(subject):
    """Return the languages of a film."""
    languages = [
        language.strip()
        for language in subject.get("languages") or []
        if isinstance(language, str) and language.strip()
    ]
    return languages or None


def process_movie(response):
    """Turn one subject payload into the metadata other providers return."""
    subject = response
    media_id = str(subject.get("id") or "")
    score, score_count = get_rating(subject)
    actors = subject.get("actors") or []
    runtime = parse_runtime(subject.get("durations"))

    date = release_date(subject)

    return {
        "media_id": media_id,
        "source": Sources.DOUBAN.value,
        "source_url": SUBJECT_URL.format(media_id=media_id),
        "media_type": MediaTypes.MOVIE.value,
        "title": (subject.get("title") or "").strip() or media_id,
        "max_progress": 1,
        "image": fixed_poster(subject.get("cover_url")),
        "synopsis": get_synopsis(subject),
        "genres": get_genres(subject),
        # Directors are a list of ``{"name": ...}`` on the item, walked whole
        # because a film can credit two or seven of them
        "directors": [{"name": name} for name in get_names(subject.get("directors"))],
        "score": score,
        "score_count": score_count,
        "release_date": date,
        # Minutes, for the importer to store on the movie row. ``details`` keeps
        # the human readable form shown on the page.
        "runtime_minutes": runtime,
        "details": {
            "format": "Movie",
            "release_date": date,
            "runtime": readable_duration(runtime),
            "country": get_country(subject),
            "languages": get_languages(subject),
            "status": "Released" if subject.get("is_released") else None,
        },
        "cast": get_cast(actors)[:CAST_DISPLAY_LIMIT],
        "total_cast_count": len(actors),
        "related": {},
        "external_links": {"Douban": SUBJECT_URL.format(media_id=media_id)},
    }


def request_subject(media_id):
    """Return the detail payload of a subject, film or show.

    The film path is tried first. A show id answers 200 there as well, it just
    carries ``subtype: tv``, so the subtype is what decides. The show path is
    only a second look, used to tell a show apart from an id that does not
    exist (a plain 404 with ``traversal_error``); both end in the same 404, the
    second call only makes the log say which of the two it was.
    """
    try:
        response = get(f"{BASE_URL}/{MOVIE_PATH}/{media_id}")
        response.raise_for_status()
    except requests.HTTPError as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status != requests.codes.not_found:
            handle_error(error)

        if _is_show(media_id):
            logger.info("Douban %s is a show, skipped by the film provider", media_id)
        services.raise_not_found_error(Sources.DOUBAN.value, media_id, "movie")
        raise  # unreachable, keeps the type checker honest

    payload = _json_or_none(response)
    if payload is None:
        services.raise_not_found_error(Sources.DOUBAN.value, media_id, "movie")

    if payload.get("subtype") == SERIES_SUBTYPE:
        # A show, which this provider does not serve. Opening it as a film would
        # show a show's metadata on a film row.
        services.raise_not_found_error(Sources.DOUBAN.value, media_id, "movie")

    return payload


def _is_show(media_id):
    """Return whether an id is a show, used only to explain a 404."""
    try:
        response = get(f"{BASE_URL}/{TV_PATH}/{media_id}")
    except requests.RequestException:
        return False

    if response.status_code != requests.codes.ok:
        return False

    payload = _json_or_none(response)
    return bool(payload) and payload.get("subtype") == SERIES_SUBTYPE


def _json_or_none(response):
    """Return the JSON body of a response, or None when it is not JSON."""
    try:
        return response.json()
    except ValueError:
        return None


def cache_get(key):
    """Return a cached value, or None when the cache cannot be reached.

    Redis is a cache, not storage: when it is down (a restart of the site, a
    stopped service) reading it raises ``ConnectionError``. Letting that out
    would turn one film into an import failure for a reason that has nothing to
    do with the film, so an unreachable cache is treated as an empty one.
    """
    try:
        return cache.get(key)
    except Exception as error:  # noqa: BLE001
        logger.warning("Douban cache read failed for %s: %s", key, error)
        return None


def cache_set(key, value, timeout=None):
    """Store a value in the cache, ignoring a cache that cannot be reached.

    ``timeout`` is the number of seconds to keep it; ``None`` keeps the entry
    until it is deleted, which is how the project writes search results.
    """
    try:
        cache.set(key, value, timeout)
    except Exception as error:  # noqa: BLE001
        # A lost cache entry only costs one more call to Douban later on
        logger.warning("Douban cache write failed for %s: %s", key, error)


def movie(media_id):
    """Return the metadata of a Douban film.

    Everything the details page shows is already stored on the item by the
    importer, so a Douban that will not answer right now must not turn an
    imported film into a 500: the stored copy is served instead, and the cache
    is left unwritten so the next open asks Douban again.
    """
    cache_key = f"{Sources.DOUBAN.value}_{MediaTypes.MOVIE.value}_{media_id}"
    data = cache_get(cache_key)
    if data is not None:
        return data

    try:
        data = process_movie(request_subject(media_id))
    except services.ProviderAPIError as error:
        fallback = stored_movie(media_id)
        if fallback is None:
            raise
        logger.warning(
            "Douban unavailable for %s (%s), serving the stored copy",
            media_id,
            error,
        )
        return fallback

    cache_set(cache_key, data)
    return data


def stored_movie(media_id):
    """Return the metadata of a film from the item the importer stored.

    The stored item holds the title, the poster, the synopsis, the genres, the
    directors, the cast and the release date, which is everything the page
    needs to render. The community score and the running time live on the movie
    row of the user, not here, so they are left out: the page reads the running
    time from that row by itself.
    """
    item = Item.objects.filter(
        media_id=str(media_id),
        source=Sources.DOUBAN.value,
        media_type=MediaTypes.MOVIE.value,
    ).first()

    if item is None:
        return None

    cast = item.cast or []
    return {
        "media_id": item.media_id,
        "source": Sources.DOUBAN.value,
        "source_url": SUBJECT_URL.format(media_id=item.media_id),
        "media_type": MediaTypes.MOVIE.value,
        "title": item.title,
        "max_progress": 1,
        "image": item.image,
        "synopsis": item.synopsis or "No synopsis available.",
        "genres": item.genres or [],
        "directors": item.directors or [],
        "score": None,
        "score_count": None,
        "release_date": (
            item.release_date.isoformat() if item.release_date else None
        ),
        "details": {
            "format": "Movie",
            "release_date": (
                item.release_date.isoformat() if item.release_date else None
            ),
        },
        "cast": cast[:CAST_DISPLAY_LIMIT],
        "total_cast_count": len(cast),
        "related": {},
        "external_links": {"Douban": SUBJECT_URL.format(media_id=item.media_id)},
    }


def search(query, page):
    """Search Douban films and return a page of results.

    A blocked or throttled Douban must not turn the search page into a 500: the
    page renders an empty result set, which is what a user with no hit sees
    anyway, and nothing is cached so the next search asks again. This mirrors
    ``douban_tv.search``.
    """
    cache_key = (
        f"search_{Sources.DOUBAN.value}_{MediaTypes.MOVIE.value}_{query}_{page}"
    )
    data = cache_get(cache_key)
    if data is not None:
        return data

    try:
        response = get(SEARCH_URL, params={"q": query})
        response.raise_for_status()
    except requests.RequestException as error:
        logger.warning("Douban film search failed for %r: %s", query, error)
        per_page = settings.PER_PAGE
        return helpers.format_search_response(page, per_page, 0, [])

    try:
        entries = response.json()
    except ValueError:
        entries = []

    if not isinstance(entries, list):
        entries = []

    # Shows and books come back from the same suggestion endpoint
    movies = [
        entry
        for entry in entries
        if entry.get("type") == MOVIE_TYPE and entry.get("id")
    ]

    results = [
        {
            "media_id": str(entry["id"]),
            "source": Sources.DOUBAN.value,
            "media_type": MediaTypes.MOVIE.value,
            "title": entry.get("title") or "",
            "image": large_poster(entry.get("img")),
            "release_year": _year(entry.get("year")),
        }
        for entry in movies
    ]

    per_page = settings.PER_PAGE
    start = (page - 1) * per_page
    data = helpers.format_search_response(
        page,
        per_page,
        len(results),
        results[start : start + per_page],
    )

    cache_set(cache_key, data, EMPTY_SEARCH_CACHE_TIMEOUT if not results else None)
    return data


def _year(raw):
    """Return a year as an int, or None."""
    match = RE_YEAR.match(str(raw or "").strip())
    return int(match.group(1)) if match else None
