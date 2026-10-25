"""Local image relay for providers whose CDN rejects requests from other sites.

Douban serves its cover images only to requests that carry a Douban referer:
a bare request gets ``418 I'm a teapot`` and a request from a different site
gets ``403``. A browser loading the image straight from the item page always
falls into the second case, so every Douban cover would render as a broken
image. The same is true for anything else that hides behind a referer check.

The fix is to ask the image from the server, where the referer can be set, and
serve the bytes from this site instead.
"""

import hashlib
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.cache import cache
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseNotModified,
    HttpResponseRedirect,
)
from django.utils.http import http_date, parse_http_date_safe

logger = logging.getLogger(__name__)

# Host suffix -> the referer that host wants before it serves an image
REFERER_BY_HOST = {
    "doubanio.com": "https://book.douban.com/",
    "douban.com": "https://book.douban.com/",
}

# Steam's CDN hands out covers to anyone: no referer is needed, but the images
# are big and far away, so every list page used to wait on the browser going
# out to Steam for each poster. These hosts therefore go through the same
# local cache as Douban, just without the extra referer header.
#
# ``shared.fastly.steamstatic.com`` is the current asset host (hashed paths);
# ``media.steampowered.com`` still answers the legacy ``/steam/apps/<id>/...``
# paths, which is what the older imports stored.
CACHE_ONLY_HOSTS = frozenset({
    "media.steampowered.com",
    "shared.fastly.steamstatic.com",
    # The asset host the Store API reports for recently published games. It is
    # a proxy only host: a direct request from this machine never completes
    # (a connect timeout every time, measured at 20 s), while the local proxy
    # answers in about half a second. See ``PROXY_ONLY_HOSTS``.
    "shared.akamai.steamstatic.com",
    "cdn.cloudflare.steamstatic.com",
    "steamcdn-a.akamaihd.net",
    # WeRead (微信读书) book covers, which arrive without a referer check but
    # are far away (200~900 ms each, every visit). Each entry is one exact
    # host taken from the 279 ``media_type='book'`` rows in ``app_item``: the
    # WeRead CDN, the two COS buckets its covers live in, and the QQ mail CDN
    # it also serves some covers from. That covers 242 of the 279 books.
    #
    # Exact hosts only, never ``.myqcloud.com`` or ``.qq.com``: those are
    # shared Tencent domains, so a suffix rule would let an unrelated bucket
    # through the relay. A new bucket therefore needs its own line here.
    #
    # Two book hosts are deliberately absent:
    # ``res.weread.qq.com`` (36 books) answers ``302`` to a Tencent Cloud host
    # that returns ``application/octet-stream``, so the relay can never accept
    # its bytes as an image and would only pay ~0.9 s before redirecting; and
    # ``www.themoviedb.org`` (1 book) only holds the grey placeholder glyph.
    "cdn.weread.qq.com",
    "wfqqreader-1252317822.image.myqcloud.com",
    "weread-1258476243.file.myqcloud.com",
    "rescdn.qqmail.com",
})

# Everything the relay endpoint is willing to fetch. Without this the view is an
# open image proxy: anyone could hand it a third party URL and use this site as
# free bandwidth. Derived from the two maps above so a host can never be
# cacheable without also being allowed through the endpoint (Douban included).
ALLOWED_RELAY_HOSTS = frozenset(REFERER_BY_HOST) | CACHE_ONLY_HOSTS

# Hosts that only answer through the operator's local proxy. Asking them
# directly does not fail fast, it hangs until the connect timeout, and every
# list page would wait that out. These skip the direct attempt and go straight
# through the proxy, so a host is only listed here once the direct route was
# measured dead. Every other allowed host keeps the usual order (proxy first on
# this machine, direct as the fallback).
PROXY_ONLY_HOSTS = frozenset({
    "shared.akamai.steamstatic.com",
})

# Douban's image CDN accepts either Douban referer for a film poster: with
# ``book.douban.com`` and with ``movie.douban.com`` the same 88 KB JPEG comes
# back, a request with no referer at all gets ``418``. The map above is left
# alone rather than widened, because it is what keeps the books working.
#
# What does break is the ``img9`` shard, which answers an HTML page instead of
# the image, referer or not. Its posters are rewritten to ``img3`` before they
# are stored (see ``app.providers.douban_movie``), so no special case is needed
# here.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
# How long a fetched image is kept in the cache and handed to the browser
IMAGE_CACHE_SECONDS = 60 * 60 * 24 * 30
MAX_IMAGE_BYTES = 8 * 1024 * 1024
REQUEST_TIMEOUT = 20


def _repo_root():
    """Return the directory that owns ``.tools``, or ``BASE_DIR`` if none."""
    start = Path(settings.BASE_DIR)
    for candidate in (start, *start.parents):
        if (candidate / ".tools").is_dir() or (candidate / ".git").exists():
            return candidate
    return start


# Fetched images also land on disk, because the Redis on this machine runs with
# ``--save "" --appendonly no``: everything in it is gone after a restart, and
# every Douban cover has to be pulled from the source again. Redis stays as the
# hot layer, the disk is the durable one. ``.tools/raw/`` is gitignored whole.
IMAGE_DIR = _repo_root() / ".tools" / "raw" / "covers"

# Suffix used on disk, and the content type handed back when reading it again.
# Anything not listed is stored as ``.bin``: the bytes are still right.
_CONTENT_TYPE_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".bin": "application/octet-stream",
}
_SUFFIX_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
# Fallback for ``.bin``: the suffix alone no longer says what the bytes are
_MAGIC_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _sniff_content_type(content):
    """Guess a content type from the first bytes, or return the binary type."""
    for magic, content_type in _MAGIC_SIGNATURES:
        if content.startswith(magic):
            return content_type
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    return "application/octet-stream"


def _disk_paths(digest):
    """Yield every path an image with this digest could live at."""
    bucket = IMAGE_DIR / digest[:2]
    for suffix in _CONTENT_TYPE_BY_SUFFIX:
        yield bucket / f"{digest}{suffix}"


def image_digest(url):
    """Return the sha256 hex digest that names this image on disk and in cache."""
    return hashlib.sha256((url or "").encode()).hexdigest()


def etag_for(url):
    """Return the quoted validator for an image URL.

    The bytes follow the URL, so the first 16 hex characters of its sha256 are
    a stable, unique enough validator: same URL, same ETag, across restarts.
    """
    return '"' + image_digest(url)[:16] + '"'


def _disk_mtime(digest):
    """Return the mtime of the stored image, or None when it is not on disk."""
    for path in _disk_paths(digest):
        try:
            if path.is_file():
                return path.stat().st_mtime
        except OSError:
            continue
    return None


def _etag_matches(header_value, etag):
    """Return True if an ``If-None-Match`` value matches ``etag``.

    Handles ``*``, the weak prefix (``W/"..."``) and comma separated lists.
    """
    if not header_value:
        return False
    for candidate in header_value.split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        if candidate == etag:
            return True
    return False


def _not_modified(request, etag, mtime):
    """Decide whether the browser's copy of this image is still current."""
    if_none_match = request.headers.get("If-None-Match")
    if if_none_match is not None:
        return _etag_matches(if_none_match, etag)

    if_modified_since = request.headers.get("If-Modified-Since")
    if if_modified_since and mtime is not None:
        since = parse_http_date_safe(if_modified_since)
        if since is not None and int(mtime) <= since:
            return True
    return False


def _read_from_disk(digest):
    """Return ``(content, content_type)`` from disk, or None if not stored."""
    for path in _disk_paths(digest):
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                continue
            content = path.read_bytes()
        except OSError as error:
            # Unreadable or truncated file: pretend it is not there and go on
            # to the network instead of failing the request.
            logger.warning("Image relay could not read %s: %s", path, error)
            continue
        if path.suffix == ".bin":
            return content, _sniff_content_type(content)
        return content, _CONTENT_TYPE_BY_SUFFIX[path.suffix]
    return None


def _write_to_disk(digest, content, content_type):
    """Persist the image so a Redis restart does not lose it. Never raises."""
    clean_type = (content_type or "").split(";")[0].strip().lower()
    suffix = _SUFFIX_BY_CONTENT_TYPE.get(clean_type, ".bin")
    bucket = IMAGE_DIR / digest[:2]
    path = bucket / f"{digest}{suffix}"
    # Per-process temp name, then an atomic rename: two workers fetching the
    # same cover at once cannot leave a half-written file behind.
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        bucket.mkdir(parents=True, exist_ok=True)
        with temp.open("wb") as handle:
            handle.write(content)
        temp.replace(path)
    except OSError as error:
        logger.warning("Image relay could not persist %s: %s", path, error)
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def _host_matches(host, suffix):
    """Return True if ``host`` is ``suffix`` or one of its subdomains."""
    return host == suffix or host.endswith("." + suffix)


def referer_for(url):
    """Return the referer an image host expects, or None if it needs none."""
    host = (urlparse(url).hostname or "").lower()
    for suffix, referer in REFERER_BY_HOST.items():
        if _host_matches(host, suffix):
            return referer
    return None


def should_cache(url):
    """Return True if this image belongs in the local cache.

    That covers the referer protected hosts (Douban) and the hosts listed in
    :data:`CACHE_ONLY_HOSTS` (Steam), which serve without a referer but are
    still slow enough to be worth caching locally.
    """
    url = url or ""
    if referer_for(url):
        return True
    host = (urlparse(url).hostname or "").lower()
    return any(_host_matches(host, suffix) for suffix in CACHE_ONLY_HOSTS)


def host_allowed(url):
    """Return True if the relay endpoint may fetch this URL at all."""
    host = (urlparse(url or "").hostname or "").lower()
    return bool(host) and any(
        _host_matches(host, suffix) for suffix in ALLOWED_RELAY_HOSTS
    )


def _proxy_only(url):
    """Return True if this host is only ever fetched through the proxy."""
    host = (urlparse(url or "").hostname or "").lower()
    return any(_host_matches(host, suffix) for suffix in PROXY_ONLY_HOSTS)


def _relay_proxies():
    """Return the proxy mapping for an image fetch, or ``{}`` for a direct one.

    The helper is imported from the Steam importer instead of copied, so the
    address stays in one place and an environment override of
    ``STEAM_PROXY_URL`` keeps working here too. The import is done lazily
    because ``integrations.imports.steam`` pulls in the Django models and the
    provider registry, which would be a cycle at this module's import time.

    An empty mapping means "no proxy is listening, go direct", which is exactly
    the fallback this relay wants too.
    """
    from integrations.imports.steam import steam_proxies  # noqa: PLC0415

    return steam_proxies()


def proxy_url(url):
    """Return the local URL that serves an external image, or "" for empty."""
    if not url:
        return ""

    from django.urls import reverse  # noqa: PLC0415

    return f"{reverse('image_relay')}?url={requests.utils.quote(url, safe='')}"


def _request_image(url, headers):
    """Return a live response for the image, or ``None`` when nothing worked.

    Order on this machine is proxy first, direct second, because the local
    proxy is what reaches the Steam asset hosts at all. A proxy only host (see
    :data:`PROXY_ONLY_HOSTS`) skips the direct attempt entirely: asking it
    directly just burns a full connect timeout, and with no proxy listening
    there is nothing left to try, so the fetch gives up at once instead of
    stalling the page for twenty seconds. Every other host keeps the usual
    order, and with no proxy listening the mapping comes back empty, which
    degrades the fetch to a plain direct request.
    """
    proxy = _relay_proxies()
    if proxy:
        attempts = [proxy] if _proxy_only(url) else [proxy, {}]
    else:
        attempts = [] if _proxy_only(url) else [{}]

    if not attempts:
        # Proxy only host and no proxy running: the direct route is known dead,
        # so fail fast rather than wait out a connect timeout that cannot win.
        logger.warning("Image relay has no route to %s (proxy is down)", url)
        return None

    failure = None
    for proxies in attempts:
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                proxies=proxies,
            )
            response.raise_for_status()
        except (requests.RequestException, ValueError) as error:
            failure = error
            continue
        return response

    logger.warning("Image relay failed for %s: %s", url, failure)
    return None


def fetch_image(url):
    """Return ``(content, content_type)`` for an image URL, or ``(None, None)``.

    Only hosts in :data:`ALLOWED_RELAY_HOSTS` are fetched here (Douban, which
    needs a referer, and Steam, which does not); everything else is left to the
    browser, so this cannot be abused as a general purpose proxy.
    """
    referer = referer_for(url)
    if not should_cache(url):
        return None, None

    digest = image_digest(url)
    cache_key = "relay_img_" + digest

    # Disk first: it survives a restart, the Redis cache does not.
    cached = _read_from_disk(digest)
    if cached is None:
        cached = cache.get(cache_key)
    if cached is not None:
        return cached

    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer

    response = _request_image(url, headers)
    if response is None:
        return None, None

    content_type = response.headers.get("Content-Type", "")
    if not content_type.startswith("image/"):
        logger.warning(
            "Image relay got %s instead of an image for %s",
            content_type or "an unknown type",
            url,
        )
        return None, None

    if len(response.content) > MAX_IMAGE_BYTES:
        logger.warning("Image relay skipped oversized image %s", url)
        return None, None

    value = (response.content, content_type)
    cache.set(cache_key, value, IMAGE_CACHE_SECONDS)
    _write_to_disk(digest, response.content, content_type)
    return value


def relay(request):
    """Serve an external image through this site, adding the needed referer."""
    url = request.GET.get("url", "").strip()
    if not url:
        return HttpResponseBadRequest("Missing image URL")

    if not url.startswith(("http://", "https://")):
        return HttpResponseBadRequest("Invalid image URL")

    # Whitelist before anything is fetched: this endpoint is part of the site,
    # it must not become a free proxy for arbitrary URLs.
    if not host_allowed(url):
        return HttpResponseBadRequest("Host not allowed")

    content, content_type = fetch_image(url)
    if content is None:
        # Nothing fetched: let the browser try on its own, the CDN may be fine
        return HttpResponseRedirect(url)

    # Only now that there is something to serve is it safe to answer 304:
    # deciding earlier would report a failed fetch as "unchanged".
    digest = image_digest(url)
    etag = etag_for(url)
    mtime = _disk_mtime(digest)
    cache_control = f"public, max-age={IMAGE_CACHE_SECONDS}"

    if _not_modified(request, etag, mtime):
        response = HttpResponseNotModified()
        # The browser needs both back on a 304 to know to keep its copy.
        response["ETag"] = etag
        response["Cache-Control"] = cache_control
        if mtime is not None:
            response["Last-Modified"] = http_date(mtime)
        return response

    response = HttpResponse(content, content_type=content_type)
    response["Cache-Control"] = cache_control
    response["ETag"] = etag
    # No mtime (Redis-only hit) means no Last-Modified, rather than a freshly
    # generated date that would make every refresh look "modified" again.
    if mtime is not None:
        response["Last-Modified"] = http_date(mtime)
    return response


def localize(url):
    """Return a URL that renders an external image from this site."""
    if not url or url == settings.IMG_NONE:
        return url

    if not should_cache(url):
        return url

    return proxy_url(url)


def steam_hero_url(media_id):
    """Return the wide ``library_hero.jpg`` URL for a Steam appid, or ""."""
    appid = str(media_id or "").strip()
    if not appid.isdigit():
        return ""
    return f"https://media.steampowered.com/steam/apps/{appid}/library_hero.jpg"
