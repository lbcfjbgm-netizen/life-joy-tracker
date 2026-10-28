"""Resolve the official cover and the Chinese title of a Steam game.

A game is imported from its App ID alone, so the art and the title have to be
asked of the Store. This module holds the one chain of fallbacks the importer
and the backfill script share, so a cover is always picked the same way.

The paths are tried from the best looking asset down to the first one that
exists, and every asset is the Store's own: nothing is cropped or generated
here.

1. ``library_600x900.jpg`` next to the App ID. This is the portrait capsule the
   library already holds and the only path that needs no call at all, so it is
   free for the games that still have it (about two thirds of a recent sample).
2. The ``library_capsule`` the Store reports. Games released after the
   storefront migration have no guessable path, as their art lives under a per
   build hash directory; that hash is only in the Store answer. The 2x variant
   is a true 600x900 portrait.
3. The ``hero_capsule`` of the same answer, a 748x896 portrait, for the games
   whose library capsule was never uploaded.
4. The ``capsule_image`` of the ``appdetails`` endpoint, which is wide. It is
   only used when no portrait asset exists at all.
5. The ``header_image`` of the same endpoint, wider still, the last resort
   before giving up.
6. An empty string, so the caller can tell "no art" from "not checked yet".

The stored URL is the Fastly host, never the Akamai one: the latter times out
for a large share of App IDs from this machine.

The title has its own chain, for a different reason. A delisted game answers
``{"success": false}`` to ``appdetails`` and is gone from the browse endpoint
as well, yet the achievements of many of them are still published, and that
answer carries the name of the game. So the title is looked up as:

1. The browse entry, which already holds the localized name (free, the assets
   of the same answer are needed for the cover anyway).
2. ``appdetails``, for the games missing from the browse answer.
3. ``ISteamUserStats/GetSchemaForGame``, the only remaining source for a game
   that left the Store: about one in six of the delisted titles still has its
   name there. Its ``availableGameStats.achievements`` length is the number of
   achievements the game has, returned alongside.
4. An empty string, which the caller turns into a readable placeholder.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.parse
from pathlib import Path

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

STORE_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
STORE_BROWSE_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
# The game schema endpoint still answers for games that left the Store, and its
# answer holds the name of the game next to its achievements.
STEAM_SCHEMA_URL = "https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/"

# The languages the name is asked in, in order. A game with no Simplified
# Chinese translation simply repeats its English name, so the English pass is
# the one that fills the remaining holes.
TITLE_LANGUAGES = ("schinese", "english")

# The asset hosts. Akamai is what the Store usually reports, Fastly is the
# mirror that answers from this machine, and the old host is the fallback the
# library already uses for the portrait capsule.
AKAMAI_HOST = "https://shared.akamai.steamstatic.com"
FASTLY_HOST = "https://shared.fastly.steamstatic.com"
OLD_CDN_HOST = "https://media.steampowered.com/steam/apps"
NEW_CDN_HOST = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps"

# Games released after the storefront migration have an App ID of three million
# or more and were never mirrored to the old path.
NEW_APPID_MIN = 3_000_000

# The file every game used to have next to its App ID, and the one the library
# already stores for the games imported before this module existed.
GUESS_COVER_FILE = "library_600x900.jpg"

# The two portrait assets of the Store answer, best first.
BROWSE_COVERS = (
    ("library_capsule_2x", "store_library_capsule_2x"),
    ("library_capsule", "store_library_capsule"),
    ("hero_capsule_2x", "store_hero_capsule_2x"),
    ("hero_capsule", "store_hero_capsule"),
)

# The wide assets of the appdetails answer, only used when nothing portrait
# exists: a slightly wrong aspect ratio beats a blank card.
DETAILS_COVERS = (
    ("capsule_image", "store_capsule_image"),
    ("header_image", "store_header_image"),
)

# The same wide art, fetched by path instead of by report. A game pulled from
# the Store publishes no asset table at all, yet the old CDN keeps serving its
# art under the bare App ID, so this is the step that covers most of them.
OLD_CDN_COVERS = (
    ("header.jpg", "old_header"),
    ("capsule_616x353.jpg", "old_capsule_616x353"),
)

REQUEST_TIMEOUT = 20
COVER_CHECK_TIMEOUT = 10
RETRIES = 2
RETRY_DELAY = 2

# A cover smaller than this is an error page, not art.
MIN_COVER_BYTES = 1000

USER_AGENT = "Mozilla/5.0 (compatible; YamtrackSteamStore/1.0)"

# A title is only stored as the Chinese one when it actually holds Chinese:
# the Store answers with the English name for the games it never localised.
CHINESE_START = 0x4E00
CHINESE_END = 0x9FFF


def _proxy_alive(proxy_url, timeout=1.0):
    """Return whether something is listening at the given proxy URL."""
    try:
        parsed = urllib.parse.urlparse(proxy_url)
        host = parsed.hostname
        port = parsed.port or 8080
        if not host:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def store_proxies():
    """Return the proxy mapping to use for Store calls, empty when unreachable.

    The proxy is what keeps the Store reachable when the direct route is
    blocked, but the Store also answers directly on most days, hence the empty
    mapping rather than a failure when nothing listens.
    """
    proxy_url = getattr(settings, "STEAM_PROXY_URL", "") or "http://127.0.0.1:7897"
    if not proxy_url or not _proxy_alive(proxy_url):
        return {}
    return {"http": proxy_url, "https": proxy_url}


def _get(url, params=None, timeout=REQUEST_TIMEOUT):
    """Return the JSON answer of a Store endpoint, None when it can't be read.

    The Store drops answers now and then, so a failure is tried twice more
    before giving up: the caller falls back to the next asset, and one dead
    game must never stop a whole batch.
    """
    proxies = store_proxies()
    headers = {"User-Agent": USER_AGENT}
    last_error = None

    for attempt in range(RETRIES + 1):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=timeout,
                proxies=proxies,
                headers=headers,
            )
            if response.status_code == requests.codes.ok:
                return response.json()
            last_error = f"HTTP {response.status_code}"
        except (requests.RequestException, ValueError) as exc:
            last_error = exc

        if attempt < RETRIES:
            logger.debug("Store attempt %d failed (%s), retrying", attempt + 1, last_error)
            time.sleep(RETRY_DELAY)

    logger.debug("Could not read %s: %s", url, last_error)
    return None


def _get_direct(url, params=None, timeout=REQUEST_TIMEOUT):
    """Return the JSON answer of a Web API endpoint, None when it can't be read.

    ``api.steampowered.com`` answers on the direct route, unlike the storefront,
    so the proxy is offered when one is running and skipped when it is not.
    A read timeout happens often enough to be worth the same two retries as the
    Store calls.
    """
    proxies = store_proxies()
    headers = {"User-Agent": USER_AGENT}
    last_error = None

    for attempt in range(RETRIES + 1):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=timeout,
                proxies=proxies,
                headers=headers,
            )
            if response.status_code == requests.codes.ok:
                return response.json()
            last_error = f"HTTP {response.status_code}"
        except (requests.RequestException, ValueError) as exc:
            last_error = exc

        if attempt < RETRIES:
            logger.debug(
                "Web API attempt %d failed (%s), retrying",
                attempt + 1,
                last_error,
            )
            time.sleep(RETRY_DELAY)

    logger.debug("Could not read %s: %s", url, last_error)
    return None


def _head_ok(url):
    """Return whether a cover URL serves an image, not an error page."""
    try:
        response = requests.head(
            url,
            timeout=COVER_CHECK_TIMEOUT,
            allow_redirects=True,
            proxies=store_proxies(),
            headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException:
        return False

    if response.status_code != requests.codes.ok:
        return False

    length = int(response.headers.get("Content-Length") or 0)
    # A chunked answer reports no length; only a reported tiny body is refused
    return length == 0 or length >= MIN_COVER_BYTES


def guess_cover_url(appid):
    """Return the cover path built from the App ID alone."""
    try:
        is_new = int(appid) >= NEW_APPID_MIN
    except (TypeError, ValueError):
        is_new = False
    base = NEW_CDN_HOST if is_new else OLD_CDN_HOST
    return f"{base}/{appid}/{GUESS_COVER_FILE}"


def _asset_url(appid, filename):
    """Return the full URL of an asset the Store reports as a bare file name."""
    if not filename:
        return ""
    if filename.startswith("http"):
        return filename.replace(AKAMAI_HOST, FASTLY_HOST)
    return f"{NEW_CDN_HOST}/{appid}/{filename}"


def get_browse_items(appids):
    """Return the Store browse entries of many games in one call, keyed by App ID.

    ``IStoreBrowseService/GetItems`` is the endpoint the Steam client itself
    uses for a library page, and the only one that reports the per build hash
    directory of the newer games. It accepts a list of App IDs, so a batch only
    costs one request; the caller that needs the assets of a single game is
    just a batch of one.
    """
    wanted = []
    for appid in appids:
        try:
            wanted.append(int(appid))
        except (TypeError, ValueError):
            continue
    if not wanted:
        return {}

    payload = {
        "ids": [{"appid": appid} for appid in wanted],
        "context": {"language": "schinese", "country_code": "CN", "steam_realm": 1},
        "data_request": {"include_assets": True, "include_basic_info": True},
    }
    answer = _get(
        STORE_BROWSE_URL,
        params={"input_json": json.dumps(payload, separators=(",", ":"))},
    )
    if not isinstance(answer, dict):
        return {}

    items = (answer.get("response") or {}).get("store_items") or []
    entries = {}
    for item in items:
        appid = item.get("id")
        if appid is None:
            continue
        entries[str(appid)] = item
    return entries


def store_assets(appid):
    """Return the asset table the Store publishes for a game, empty on failure."""
    entry = get_browse_items([appid]).get(str(appid)) or {}
    assets = entry.get("assets") or {}
    return assets if isinstance(assets, dict) else {}


def store_details(appid, language="schinese"):
    """Return the appdetails block of a game, empty on failure."""
    answer = _get(
        STORE_APPDETAILS_URL,
        params={"appids": appid, "l": language, "filters": "basic"},
    )
    if not isinstance(answer, dict):
        return {}

    entry = answer.get(str(appid)) or {}
    if not entry.get("success"):
        return {}
    data = entry.get("data") or {}
    return data if isinstance(data, dict) else {}


def resolve_store_cover(appid, assets=None, details=None):
    """Return the best official cover URL of a game, empty when none exists.

    The chain above is walked in order and the first URL that serves an image
    wins. A game that has no asset anywhere returns an empty string, which the
    caller stores as is: a blank cover is honest, a guessed dead link is not.

    Args:
        appid (int): Steam App ID
        assets (dict | None): the Store browse assets of the game, fetched
            here when not given, so a batch can share one request
        details (dict | None): the appdetails block of the game, fetched here
            when not given
    """
    guess = guess_cover_url(appid)
    if _head_ok(guess):
        return guess

    if assets is None:
        assets = store_assets(appid)

    for key, _tier in BROWSE_COVERS:
        url = _asset_url(appid, assets.get(key) or "")
        if url and _head_ok(url):
            return url

    if details is None:
        details = store_details(appid)

    for key, _tier in DETAILS_COVERS:
        url = (details.get(key) or "").replace(AKAMAI_HOST, FASTLY_HOST)
        if url.startswith("https://") and _head_ok(url):
            return url

    # Last resort: a delisted game publishes no asset table, yet the old CDN
    # still serves the wide art of most of them under the bare App ID path.
    # Only 478 of the 129 tested had nothing here at all.
    for filename, tier in OLD_CDN_COVERS:
        url = f"{OLD_CDN_HOST}/{appid}/{filename}"
        if _head_ok(url):
            return url

    logger.info("No official cover found for Steam app %s", appid)
    return ""


def cover_tier(appid, url):
    """Return which step of the chain a resolved cover came from, for logging."""
    if not url:
        return "none"

    if url == guess_cover_url(appid):
        return GUESS_COVER_FILE
    # The longest key first: ``library_capsule`` is a prefix of
    # ``library_capsule_2x``, so a plain loop would report the smaller asset
    for key, _tier in sorted(BROWSE_COVERS, key=lambda pair: -len(pair[0])):
        if key in url:
            return key
    for filename, tier in OLD_CDN_COVERS:
        if filename in url:
            return tier
    for _key, tier in DETAILS_COVERS:
        if tier.replace("store_", "") in url:
            return tier
    return "unknown"


def has_chinese(text):
    """Return whether a title holds at least one Chinese character."""
    if not text:
        return False
    return any(CHINESE_START <= ord(char) <= CHINESE_END for char in text)


def store_title(appid):
    """Return the two titles of a game as ``(title, title_zh)``.

    The Store answers in Simplified Chinese when asked to, but a game that was
    never localised comes back with its English name. Storing that name twice
    would fill the Chinese column with Latin text, so the second title is only
    filled when the answer actually holds Chinese; otherwise the English name
    is the only one there is and it goes in the first column.
    """
    data = store_details(appid, "schinese")
    name = (data.get("name") or "").strip()
    if not name:
        return "", ""

    if has_chinese(name):
        # The English name is only worth a second call when the Chinese one
        # exists: without it, the item keeps the Chinese title in both fields
        # and nothing is lost.
        english = (store_details(appid, "english").get("name") or "").strip()
        return (english or name), name

    return name, ""


def read_api_key():
    """Return the Steam Web API key, preferring the freshest copy in ``.env``.

    Same reason as ``steam.read_access_token``: Django reads ``.env`` once at
    startup, so a key rotated on disk would otherwise stay stale until the next
    restart. The file is read first and the setting is only the fallback.
    """
    env_path = Path(settings.BASE_DIR).parent / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "STEAM_API_KEY":
                api_key = value.strip()
                if api_key:
                    return api_key
    except OSError:
        pass

    return getattr(settings, "STEAM_API_KEY", "") or ""


def schema_title(appid):
    """Return the game name the achievement schema still publishes.

    A game pulled from the Store answers ``{"success": false}`` to
    ``appdetails`` and disappears from the browse endpoint, yet its
    achievements are often still served, and that answer carries the name of
    the game. It is the last source of a real title for a delisted game: about
    one in six still has its name here.

    ``gameName`` can come back empty even when the achievements do not, so a
    blank answer moves on to the next language instead of stopping the chain.

    Args:
        appid (int): Steam App ID

    Returns:
        tuple: ``(name, achievements_total)``; ``name`` is an empty string when
        no language returned one, and ``achievements_total`` is 0 in that case
    """
    api_key = read_api_key()
    if not api_key:
        logger.debug("No Steam Web API key, skipping the schema of app %s", appid)
        return "", 0

    best_total = 0
    for language in TITLE_LANGUAGES:
        answer = _get_direct(
            STEAM_SCHEMA_URL,
            params={"key": api_key, "appid": appid, "l": language},
        )
        if not isinstance(answer, dict):
            continue

        game = answer.get("game") or {}
        if not isinstance(game, dict):
            continue

        # The count is worth keeping even for a game whose name is gone, as it
        # is the only public statement left about it
        achievements = (game.get("availableGameStats") or {}).get("achievements") or []
        if len(achievements) > best_total:
            best_total = len(achievements)

        name = (game.get("gameName") or "").strip()
        if name:
            return name, best_total

    if best_total:
        logger.info(
            "Steam app %s has %d achievements but no name in any language",
            appid,
            best_total,
        )
    return "", best_total


def store_info(appid):
    """Return the title and the cover of a game, fetched with as few calls as possible.

    The Store browse entry carries both the localized name and the assets, so
    one request answers everything for the games it knows. The appdetails
    endpoint is only asked for the ones the browse answer skipped, and the
    achievement schema only for the ones both of those have forgotten.

    Returns:
        dict: ``{"title": str, "title_zh": str, "image": str, "achievements": int}``;
        every text field is an empty string when the Store knows nothing about
        the App ID
    """
    entry = get_browse_items([appid]).get(str(appid)) or {}
    name = (entry.get("name") or "").strip()

    if name and has_chinese(name):
        english = (store_details(appid, "english").get("name") or "").strip()
        title, title_zh = (english or name), name
    elif name:
        title, title_zh = name, ""
    else:
        title, title_zh = store_title(appid)

    achievements = 0
    if not title:
        # Both Store sources came back empty: the game most likely left the
        # Store for good, and the schema is the last place its name can be
        schema_name, achievements = schema_title(appid)
        if schema_name:
            # The schema has no translations, it answers with the original name
            title = schema_name
            title_zh = schema_name if has_chinese(schema_name) else ""

    image = resolve_store_cover(appid, assets=entry.get("assets") or None)
    if not title and not image:
        logger.info("Steam app %s has neither a name nor a cover", appid)
    return {
        "title": title,
        "title_zh": title_zh,
        "image": image,
        "achievements": achievements,
    }

