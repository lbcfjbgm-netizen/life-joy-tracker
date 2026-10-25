import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings
from django.core.cache import cache
from django.http import Http404

from app import helpers, models
from app.models import MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
# This is the only Steam asset host that answered reliably from this machine:
# the Cloudflare and Akamai variants time out for a large share of app IDs,
# while this one served every sampled cover.
CDN_BASE_URL = "https://media.steampowered.com/steam/apps"

# Every game released after the storefront migration got an App ID of three
# million or more, and their covers are not mirrored to the old path: that URL
# is a 404 for them. The new asset host serves them under a different prefix,
# so the base is picked from the App ID instead of being global.
NEW_APPID_MIN = 3_000_000
NEW_CDN_BASE_URL = (
    "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps"
)
FASTLY_HOST = "https://shared.fastly.steamstatic.com"
AKAMAI_HOST = "https://shared.akamai.steamstatic.com"
STORE_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
STORE_COVER_TTL = 60 * 60 * 24

FAMILY_GROUP_URL = (
    "https://api.steampowered.com/IFamilyGroupsService/GetFamilyGroupForUser/v1/"
)
SHARED_LIBRARY_URL = (
    "https://api.steampowered.com/IFamilyGroupsService/GetSharedLibraryApps/v1/"
)

# The portrait library capsule matches the aspect ratio of the cards and is the
# cover the import stores, so the search results use it too. It is missing for
# roughly a fifth of the catalog, so a wide header is used for those games: a
# slightly cropped cover beats a broken image.
LIBRARY_COVER = "library_600x900.jpg"
HEADER_COVER = "header.jpg"
COVER_CHECK_TIMEOUT = 4
COVER_CHECK_TTL = 60 * 60 * 24


def cover_base(appid):
    """Return the asset base URL that actually serves the covers of a game.

    Games released after the storefront migration carry an App ID of three
    million or more; their covers were never mirrored to the old path, so that
    URL answers 404. They are served from the new asset host instead, while the
    old games stay on the old host because that is where they still live.
    """
    try:
        is_new = int(appid) >= NEW_APPID_MIN
    except (TypeError, ValueError):
        is_new = False
    return NEW_CDN_BASE_URL if is_new else CDN_BASE_URL


def cover_url(appid, filename=LIBRARY_COVER):
    """Return the CDN URL of a Steam cover file of a game."""
    return f"{cover_base(appid)}/{appid}/{filename}"


def pick_store_cover(payload):
    """Return the Store header image of a game, on the reachable asset host.

    New games only publish their art under a per-build hash directory, so the
    plain ``header.jpg`` path cannot be guessed; the Store API is the only
    place that knows it. The Store reports the Akamai host, which times out
    from this machine, hence the host is swapped for the Fastly mirror.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") or {}
    for key in ("header_image", "capsule_image"):
        url = data.get(key)
        if url:
            return url.replace(AKAMAI_HOST, FASTLY_HOST)
    return None


def store_cover(appid):
    """Return the Store-reported cover URL of a game, cached for a day."""
    cache_key = f"steam_store_cover_{appid}"
    known = cache.get(cache_key)
    if known is not None:
        return known or None

    try:
        payload = services.api_request(
            Sources.STEAM.value,
            "GET",
            STORE_APPDETAILS_URL,
            params={"appids": appid, "l": "schinese"},
        )
    except requests.RequestException:
        # A network hiccup says nothing about the cover, leave it undecided
        return None

    entry = payload.get(str(appid)) if isinstance(payload, dict) else None
    url = pick_store_cover(entry) if isinstance(entry, dict) else None
    if url:
        cache.set(cache_key, url, STORE_COVER_TTL)
    return url


def _cover_exists(appid, filename):
    """Return whether a Steam cover file exists, cached for a day."""
    cache_key = f"steam_cover_{appid}_{filename}"
    known = cache.get(cache_key)
    if known is not None:
        return known

    # This is a bare ``requests.head`` rather than ``services.api_request``, so
    # the interactive budget is honoured here by hand: a cover lookup never
    # waits past the deadline of the search it belongs to. Only the time left
    # is used, so the lookup gives up rather than overrunning the search.
    remaining = services.interactive_budget_remaining()
    timeout = COVER_CHECK_TIMEOUT
    if remaining is not None:
        if remaining <= 0:
            # Out of time: assume the cover is there, exactly as the network
            # failure branch below does, so no game is dropped.
            return True
        timeout = min(timeout, remaining)

    try:
        response = requests.head(
            cover_url(appid, filename),
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException:
        # A network hiccup says nothing about the cover, assume it is there
        return True

    exists = response.status_code == requests.codes.ok
    cache.set(cache_key, exists, COVER_CHECK_TTL)
    return exists


def resolve_cover(appid):
    """Return the best available cover URL of a game."""
    try:
        is_new = int(appid) >= NEW_APPID_MIN
    except (TypeError, ValueError):
        is_new = False

    if is_new:
        # The plain paths of a new game mostly miss, and the art that does
        # exist lives under a build hash only the Store API reports.
        if _cover_exists(appid, LIBRARY_COVER):
            return cover_url(appid, LIBRARY_COVER)
        url = store_cover(appid)
        if url:
            return url
        return cover_url(appid, HEADER_COVER)
    if _cover_exists(appid, LIBRARY_COVER):
        return cover_url(appid, LIBRARY_COVER)
    return cover_url(appid, HEADER_COVER)


def metadata(media_id, media_type):
    """Return the metadata of a Steam game from the stored item.

    Steam games are imported from the app ID alone, so everything shown here
    comes from the item itself, without any external API call. That includes
    the description, the genres and the release date: they are seeded from the
    local library cache, never fetched at read time.

    An app ID that was never imported has no local item to describe, so the
    details page answers 404 instead of failing on the missing row.
    """
    try:
        item = models.Item.objects.get(
            media_id=media_id,
            media_type=media_type,
            source=Sources.STEAM.value,
        )
    except models.Item.DoesNotExist as exc:
        msg = f"Game with ID {media_id} not found"
        raise Http404(msg) from exc

    return {
        "media_id": item.media_id,
        "source": Sources.STEAM.value,
        "source_url": f"https://store.steampowered.com/app/{item.media_id}",
        "media_type": item.media_type,
        "title": item.title,
        "max_progress": None,
        "image": item.image,
        "synopsis": item.synopsis or "No synopsis available.",
        # Stored on the item by the metadata backfill, so the details page can
        # show the genres and the release date without asking the Store API.
        "genres": item.genres or [],
        "release_date": item.release_date.isoformat() if item.release_date else None,
        # Official names as reported by the store. They are aliases next to
        # the display title, so they stay separate keys instead of replacing it.
        "title_zh": item.title_zh,
        "title_en": item.title_en,
        "score": None,
        "score_count": None,
        "details": {},
        "related": {},
        # Filled in by the details view from the tracked game of the user
        "achievements": {},
    }


def _resolve_cover_interactive(appid, deadline=None):
    """Resolve the cover of a game on the short budget of a search.

    ``resolve_cover`` may ask the Store API (``store.steampowered.com``), the
    very host that accepts the connection and then stays silent from this
    machine, which is why the lookup runs on the interactive budget.

    ``deadline`` is passed in because a ``ThreadPoolExecutor`` worker does not
    inherit the context variable holding it: without it every worker would
    start a fresh budget of its own, and a page of games would still take
    several rounds of the full read timeout. Once the deadline is spent the
    lookup is skipped and the cheap no-request rule is used instead: that is
    the same fallback ``resolve_cover`` takes when the network says nothing,
    so the cover may be the wide one but a game is never dropped.
    """
    remaining = services.interactive_budget_remaining(deadline)
    if remaining is None:
        # No search budget in force (a caller outside the interactive block):
        # keep the plain behaviour instead of inventing a short one.
        return resolve_cover(appid)
    if remaining <= 0:
        return _cover_url_without_requests(appid)
    with services.interactive_requests(deadline=deadline):
        return resolve_cover(appid)


def _cover_url_without_requests(appid):
    """Return the cover URL a game has by its App ID alone, without asking."""
    try:
        is_new = int(appid) >= NEW_APPID_MIN
    except (TypeError, ValueError):
        is_new = False
    return cover_url(appid, LIBRARY_COVER if is_new else HEADER_COVER)


def search(query, page):
    """Search Steam games, on the short budget of a person waiting.

    The Store host accepts the connection and then stays silent from this
    machine, so both the search call and the cover lookups are marked
    interactive: a search answers or gives up within seconds instead of
    inheriting the wide read timeout of the background jobs.
    """
    with services.interactive_requests():
        return _search(query, page)


def _search(query, page):
    """Search Steam games with the public (keyless) store search endpoint.

    The endpoint returns games, bundles and DLC mixed together, only the games
    are kept. It has no pagination, the whole result set comes back in one
    call, so the local slice is used to page through it.
    """
    cache_key = f"search_{Sources.STEAM.value}_{MediaTypes.GAME.value}_{query}_{page}"
    data = cache.get(cache_key)
    if data is not None:
        return data

    params = {"term": query, "l": "schinese", "cc": "cn"}

    try:
        response = services.api_request(
            Sources.STEAM.value,
            "GET",
            STORE_SEARCH_URL,
            params=params,
        )
    except services.ProviderUnavailableError:
        # Already the right kind: the host is unreachable (or short-circuited),
        # not answering with an error status.
        raise
    except requests.exceptions.HTTPError as error:
        raise services.ProviderAPIError(Sources.STEAM.value, error) from error
    except requests.exceptions.RequestException as error:
        raise services.ProviderUnavailableError(Sources.STEAM.value, error) from error

    items = [
        item
        for item in response.get("items") or []
        if item.get("type") == "app" and item.get("id")
    ]

    # The cover check is one request per game, so it runs concurrently to keep
    # a search snappy even when the page is full of unknown games. The deadline
    # of the search is handed to every worker: it is not inherited across
    # threads, and each worker starting a fresh budget is what made a page of
    # games exceed the target on its own.
    deadline = services.interactive_deadline()
    with ThreadPoolExecutor(max_workers=8) as pool:
        covers = list(
            pool.map(
                lambda appid: _resolve_cover_interactive(appid, deadline),
                [item["id"] for item in items],
            ),
        )

    results = [
        {
            "media_id": str(item["id"]),
            "source": Sources.STEAM.value,
            "media_type": MediaTypes.GAME.value,
            "title": item["name"],
            "image": image,
            "release_year": None,
        }
        for item, image in zip(items, covers, strict=True)
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


def _family_request(url, access_token, params):
    """Return the parsed response of a family groups API call, {} on failure.

    A revoked or expired web token answers with 401/403. The import must keep
    working with the owned games in that case, so these are an empty result
    plus a warning instead of an exception.
    """
    params = {**params, "access_token": access_token}

    try:
        return services.api_request(
            Sources.STEAM.value,
            "GET",
            url,
            params=params,
        )
    except requests.exceptions.HTTPError as error:
        status_code = getattr(error.response, "status_code", None)
        if status_code in (
            requests.codes.unauthorized,
            requests.codes.forbidden,
        ):
            logger.warning(
                "Steam family sharing skipped, the access token was rejected "
                "(HTTP %s). Update STEAM_ACCESS_TOKEN to use it.",
                status_code,
            )
            return {}
        raise services.ProviderAPIError(Sources.STEAM.value, error) from error


def get_family_group_id(access_token, steamid):
    """Return the family group ID of a user, or None when there is no group."""
    response = _family_request(
        FAMILY_GROUP_URL,
        access_token,
        {"steamid": steamid},
    )
    family = response.get("response") or {}

    if family.get("is_not_member_of_any_group"):
        return None

    return family.get("family_groupid") or None


def get_shared_library(access_token, family_group_id):
    """Return the apps a user can play through Steam family sharing."""
    response = _family_request(
        SHARED_LIBRARY_URL,
        access_token,
        {
            "family_groupid": family_group_id,
            "include_own": 0,
            "include_excluded": 0,
            "include_free": 0,
            "include_non_games": 0,
            "language": "schinese",
        },
    )
    return (response.get("response") or {}).get("apps") or []
