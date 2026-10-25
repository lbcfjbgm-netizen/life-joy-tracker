import json
import logging
import os
import socket
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from django.conf import settings

import app
from app.models import MediaTypes, Sources, Status
from app.providers import services
from app.providers import steam as steam_provider
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError

logger = logging.getLogger(__name__)

STEAM_API_BASE_URL = "https://api.steampowered.com"

# Only this Steam asset host answered reliably from this machine: the
# Cloudflare and Akamai variants time out for a large share of app IDs. It is
# asked first and the others only serve as a fallback when it is unreachable.
CDN_HOSTS = (
    "https://media.steampowered.com/steam/apps",
    "https://cdn.cloudflare.steamstatic.com/steam/apps",
)
STEAM_CDN_BASE_URL = CDN_HOSTS[0]

# Games released after the storefront migration have an App ID of three million
# or more and were never mirrored to the old path, so their covers are served
# from the new asset host instead. Older games keep the old host, which still
# holds their art.
NEW_APPID_MIN = 3_000_000
NEW_CDN_BASE_URL = (
    "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps"
)

# Steam builds its cover art from the app ID alone, so no API call is needed.
# The portrait library capsule is used first, as the cards and the details page
# show portrait images; the wide assets are the fallback for games that never
# got a library capsule (usually delisted or region locked titles).
COVER_FILES = ("library_600x900.jpg", "header.jpg", "capsule_616x353.jpg")

# Checking whether a cover exists on the CDN costs one request per game, so it
# is on by default: a broken card is far more expensive than a few cheap HEAD
# requests. The checks run concurrently and only the items that need a fallback
# are written back.
VERIFY_COVERS = True
COVER_CHECK_WORKERS = 8
COVER_CHECK_TIMEOUT = 3

# The shared family library holds every game the relatives own, which on this
# account is 3043 titles, 2396 of them never touched once. Importing all of
# them would bury the real library under thousands of "planning" cards, so only
# the games that were actually played (playtime > 0) are imported. The playtime
# the family API reports was verified to be the user's own: for all 575 games
# present in both the owned and the shared response the two durations matched
# exactly.
FAMILY_IMPORT_PLAYED_ONLY = True

# Games the operator decided to keep out of the library live in this file. It is
# rebuilt by ``.tools/rebuild_blocklist.py`` from the review score, the playtime
# and the two week play log, and read on every import so a rebuilt list needs no
# restart. A game on the list is skipped unless it earns a second chance, see
# ``_is_blocked``.
BLOCKLIST_PATH = (
    Path(settings.BASE_DIR).parent / ".tools" / "raw" / "steam_blocklist.json"
)

# Ten hours of total playtime buys a game back even when its review score is
# poor: something played that long was clearly worth it.
BLOCKED_MIN_PLAY_MINUTES = 600

# The Steam endpoints are reachable directly from this machine, but the
# operator's proxy also answers and is tried first when it is listening, as it
# keeps working when the direct route is blocked.
STEAM_PROXY_URL = os.environ.get("STEAM_PROXY_URL", "http://127.0.0.1:7897")
PROXY_PROBE_TIMEOUT = 1.0


def _proxy_alive(proxy_url, timeout=PROXY_PROBE_TIMEOUT):
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


def steam_proxies():
    """Return the proxy mapping to use for Steam calls, empty when unreachable."""
    if not STEAM_PROXY_URL or not _proxy_alive(STEAM_PROXY_URL):
        return {}
    return {"http": STEAM_PROXY_URL, "https": STEAM_PROXY_URL}


def read_blocklist():
    """Return the set of appids that must never be imported again."""
    try:
        with BLOCKLIST_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return {int(entry["appid"]) for entry in data.get("appids", [])}
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return set()


def fetch_recently_played(api_key, steam_id):
    """Return the appids played in the last two weeks.

    Best effort: an answer that can't be read yields an empty set, which only
    means no game gets the "played recently" exemption this run.
    """
    return {game["appid"] for game in fetch_recently_played_games(api_key, steam_id)}


def fetch_recently_played_games(api_key, steam_id):
    """Return the games played in the last two weeks, with their playtime.

    The answer already carries the name and both playtime counters of every
    game, so the daily recent sync never has to read the whole library. It is
    best effort: an answer that can't be read yields an empty list.
    """
    if not api_key or not steam_id:
        return []

    url = f"{STEAM_API_BASE_URL}/IPlayerService/GetRecentlyPlayedGames/v1/"
    params = {"key": api_key, "steamid": steam_id, "format": "json"}
    # The proxy is tried first when it is up, then the direct route
    proxies = steam_proxies()
    attempts = [proxies, {}] if proxies else [{}]

    payload = None
    last_error = None
    for attempt, proxy in enumerate(attempts):
        try:
            response = requests.get(url, params=params, timeout=30, proxies=proxy)
            response.raise_for_status()
            payload = response.json()
            break
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            logger.debug("Recently played attempt %d failed: %s", attempt + 1, exc)

    if payload is None:
        logger.warning("Could not read the recently played games: %s", last_error)
        return []

    games = (payload.get("response") or {}).get("games") or []
    recent = []
    for game in games:
        try:
            appid = int(game.get("appid"))
            two_weeks = int(game.get("playtime_2weeks") or 0)
        except (TypeError, ValueError):
            continue
        # The endpoint lists the games played during the two weeks, but a zero
        # counter still shows up on some accounts
        if two_weeks <= 0:
            continue
        recent.append(
            {
                "appid": appid,
                "name": game.get("name") or f"Unknown Game {appid}",
                "playtime_forever": int(game.get("playtime_forever") or 0),
                "playtime_2weeks": two_weeks,
                "rtime_last_played": game.get("rtime_last_played"),
            },
        )

    logger.info("Got %d games played in the last two weeks", len(recent))
    return recent


# Status codes the Steam CDN returns for a cover that was never uploaded
MISSING_COVER_CODES = (
    requests.codes.forbidden,
    requests.codes.not_found,
    requests.codes.gone,
)


def cover_base(appid):
    """Return the asset base URL that actually serves the covers of a game."""
    try:
        is_new = int(appid) >= NEW_APPID_MIN
    except (TypeError, ValueError):
        is_new = False
    return NEW_CDN_BASE_URL if is_new else STEAM_CDN_BASE_URL


def cover_url(appid, filename, host=None):
    """Return the Steam CDN URL of a cover file of a game."""
    return f"{host or cover_base(appid)}/{appid}/{filename}"


def primary_cover_url(appid):
    """Return the cover URL stored when a game is imported.

    A stored URL is a promise that the card has an image, so it is never the
    portrait capsule straight away: that asset is missing for a large part of
    the catalog. The wide header is used instead, and callers that can afford
    a check (see ``resolve_cover_url``) upgrade it to the portrait one.

    A new game (App ID of three million or more) has no cover at a guessable
    path at all, so the Store is asked for the one it actually publishes.
    """
    try:
        is_new = int(appid) >= NEW_APPID_MIN
    except (TypeError, ValueError):
        is_new = False

    if is_new:
        url = steam_provider.store_cover(appid)
        if url:
            return url

    return cover_url(appid, COVER_FILES[1])


def _cover_candidates(appid, filename):
    """Return the URLs of one cover file, most reliable host first."""
    return [cover_url(appid, filename, host) for host in CDN_HOSTS]


def _check_cover(url):
    """Return whether a cover URL is reachable, None when it can't be told."""
    try:
        response = requests.head(url, timeout=COVER_CHECK_TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        # A network hiccup says nothing about the cover, keep the current URL
        return None

    if response.status_code == requests.codes.ok:
        return True
    if response.status_code in MISSING_COVER_CODES:
        return False
    return None


def resolve_cover_url(appid):
    """Return the best reachable cover URL of a game.

    The portrait capsule wins when it exists, otherwise the cover degrades to
    the wide header and then to the small capsule. A network error never
    downgrades a cover: the best theoretical URL is kept in that case, so a
    temporary outage doesn't wipe out good images.
    """
    inconclusive = False

    for filename in COVER_FILES:
        for url in _cover_candidates(appid, filename):
            exists = _check_cover(url)
            if exists:
                return url
            if exists is None:
                # Network error, don't downgrade a probably valid image
                inconclusive = True

    try:
        is_new = int(appid) >= NEW_APPID_MIN
    except (TypeError, ValueError):
        is_new = False

    if is_new:
        # Every guessable path of a new game missed, but the Store knows the
        # hashed one, so that is still better than the placeholder.
        url = steam_provider.store_cover(appid)
        if url:
            exists = _check_cover(url)
            if exists is not False:
                return url

    if inconclusive:
        return primary_cover_url(appid)
    return settings.IMG_NONE


def verify_cover_images(items, max_workers=COVER_CHECK_WORKERS):
    """Resolve the covers of imported items concurrently and return the changed.

    Args:
        items (list): tuples of the item and the Steam app ID of the game
        max_workers (int): number of parallel cover checks

    Returns:
        list: items whose image was replaced with a working URL or the placeholder
    """
    if not items:
        return []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        resolved = list(pool.map(_resolve_entry_cover, items))

    changed = []
    for item, image in resolved:
        if image and image != item.image:
            item.image = image
            changed.append(item)

    return changed


def _resolve_entry_cover(entry):
    """Resolve the cover of an imported item, for the thread pool."""
    item, appid = entry
    return item, resolve_cover_url(appid)


def _map_http_error(error):
    """Return the readable import error of a Steam API HTTP error."""
    status_code = error.response.status_code

    messages = {
        requests.codes.forbidden: "Steam profile is private or invalid",
        requests.codes.bad_request: (
            "Bad request to Steam API. Please check the Steam ID."
        ),
        requests.codes.unauthorized: "Invalid Steam API key",
    }
    if status_code in messages:
        return MediaImportError(messages[status_code])

    return MediaImportError(f"Steam API error: {status_code}")


def read_access_token():
    """Return the Steam store access token, preferring the freshest copy.

    The token lives in ``.env`` and expires after roughly a day. The scheduled
    refresher rewrites that file, but Django only reads ``.env`` once at
    startup, so a long running server would keep the stale value in
    ``settings`` forever. Reading the file first means a refreshed token is
    picked up by the very next import, with no restart required.
    """
    env_path = Path(settings.BASE_DIR).parent / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "STEAM_ACCESS_TOKEN":
                token = value.strip()
                if token:
                    return token
    except OSError:
        pass

    return settings.STEAM_ACCESS_TOKEN


def importer(steam_id, user, mode, verify_covers=VERIFY_COVERS):
    """Import the user's games from Steam."""
    steam_importer = SteamImporter(steam_id, user, mode, verify_covers=verify_covers)
    return steam_importer.import_data()


class SteamImporter:
    """Class to handle importing user game data from Steam."""

    def __init__(self, steam_id, user, mode, verify_covers=VERIFY_COVERS):
        """Initialize the importer with user details and mode.

        Args:
            steam_id (str): Steam user ID (64-bit SteamID) to import from
            user: Django user object to import data for
            mode (str): Import mode ("new" or "overwrite")
            verify_covers (bool): check the covers exist on the Steam CDN
        """
        self.steam_id = steam_id
        self.user = user
        self.mode = mode
        self.verify_covers = verify_covers
        self.warnings = []
        self.api_key = settings.STEAM_API_KEY

        if not self.api_key:
            msg = "Steam API key not configured in environment variables"
            raise MediaImportError(msg)

        self.existing_media = helpers.get_existing_media(user)

        self.to_delete = defaultdict(lambda: defaultdict(set))

        self.bulk_media = defaultdict(list)
        self.bulk_media_updates = defaultdict(list)

        # Items created by this import, checked for missing covers at the end
        self.created_items = []

        # Filled at the start of import_data, once per run
        self.blocklist = set()
        self.recently_played = set()
        self.revived = set()
        self.skipped_blocked = 0

        logger.info(
            "Initialized Steam importer for Steam ID %s with mode %s",
            steam_id,
            mode,
        )

    def import_data(self):
        """Import user's Steam game library."""
        owned_games = self._get_owned_games()

        # Games reachable through Steam family sharing come on top of the owned
        # ones, flagged in the notes so both origins stay readable
        games = owned_games + self._get_family_shared_games()

        if not games:
            logger.info("No games found for Steam user %s", self.steam_id)
            return {}, ""

        # Read once per import: a rebuilt list is picked up by the next run
        self.blocklist = read_blocklist()
        self.recently_played = fetch_recently_played(self.api_key, self.steam_id)
        self.revived = set()
        self.skipped_blocked = 0
        games = [game for game in games if not self._is_blocked(game)]

        for game_data in games:
            self._process_game(game_data)

        helpers.cleanup_existing_media(self.to_delete, self.user)
        helpers.bulk_create_media(self.bulk_media, self.user)
        # Reviving a game and moving it forward can queue the same instance
        # twice, and every queued object is written once per entry.
        updates = self.bulk_media_updates[MediaTypes.GAME.value]
        if updates:
            self.bulk_media_updates[MediaTypes.GAME.value] = list(
                {game.pk: game for game in updates}.values(),
            )
        helpers.bulk_update_media(
            self.bulk_media_updates,
            {MediaTypes.GAME.value: ["progress", "progressed_at", "status"]},
            self.user,
        )

        self._verify_covers()

        created_games = len(self.bulk_media[MediaTypes.GAME.value])
        updated_games = len(self.bulk_media_updates[MediaTypes.GAME.value])
        imported_counts = {}
        if created_games or updated_games:
            imported_counts[MediaTypes.GAME.value] = created_games + updated_games

        logger.info(
            "Steam import completed for user %s: %s "
            "(skipped_blocked=%d, revived=%d, blocklist=%d, recently_played=%d)",
            self.user.username,
            imported_counts,
            self.skipped_blocked,
            len(self.revived),
            len(self.blocklist),
            len(self.recently_played),
        )

        return imported_counts, "\n".join(self.warnings) if self.warnings else ""

    def _is_blocked(self, game_data):
        """Return whether a game must stay out of the library.

        A game on the blocklist is skipped unless it earned a second chance:
        played during the last two weeks, or played for ten hours in total.
        Such a game is imported normally and its status is restored by
        ``_revive_blocked``, which lifts the block on its own.
        """
        try:
            appid = int(game_data.get("appid"))
        except (TypeError, ValueError):
            return False

        if appid not in self.blocklist:
            return False

        if appid in self.recently_played:
            self.revived.add(str(appid))
            logger.info("Unblocking %s: played in the last two weeks", appid)
            return False

        if int(game_data.get("playtime_forever") or 0) >= BLOCKED_MIN_PLAY_MINUTES:
            self.revived.add(str(appid))
            logger.info(
                "Unblocking %s: %s minutes played",
                appid,
                game_data.get("playtime_forever"),
            )
            return False

        self.skipped_blocked += 1
        return True

    def _revive_blocked(self, game, playtime_forever, playtime_2weeks):
        """Give a game its status back when it earned an exemption."""
        if game.status != Status.DROPPED.value:
            return
        status = self._determine_game_status(
            playtime_forever,
            playtime_2weeks,
            achievements_unlocked=game.achievements_unlocked,
            achievements_total=game.achievements_total,
        )
        if status == Status.DROPPED.value:
            return
        game.status = status
        queued = self.bulk_media_updates[MediaTypes.GAME.value]
        if game not in queued:
            queued.append(game)
        logger.info("Restored %s to status %s", game.item.media_id, status)

    def _verify_covers(self):
        """Replace the covers of the imported games that are missing."""
        if not self.verify_covers or not self.created_items:
            return

        changed = verify_cover_images(self.created_items)
        if changed:
            app.models.Item.objects.bulk_update(changed, ["image"])
            logger.info(
                "Replaced the cover of %d of %d imported Steam games",
                len(changed),
                len(self.created_items),
            )

    def _get_owned_games(self):
        """Fetch owned games from Steam API, retrying the failures that heal.

        Two failures are worth another attempt: the 429 rate limit and a broken
        connection (reset, refused, timed out). The scheduled import used to
        give up on the network ones straight away, which is the one case an
        hourly retry was made for.
        """
        url = f"{STEAM_API_BASE_URL}/IPlayerService/GetOwnedGames/v0001/"
        params = {
            "key": self.api_key,
            "steamid": self.steam_id,
            "include_appinfo": 1,
            "include_played_free_games": 1,
            "format": "json",
        }

        max_retries = 3
        base_delay = 15

        for attempt in range(max_retries):
            try:
                response = services.api_request("STEAM", "GET", url, params=params)

                if "response" not in response:
                    msg = "Invalid response from Steam API"
                    raise MediaImportError(msg)

                if "games" not in response["response"]:
                    # User might have private profile or no games
                    logger.warning(
                        "No games found in Steam response for user %s",
                        self.steam_id,
                    )
                    return []

                games = response["response"]["games"]
                logger.info(
                    "Found %d games for Steam user %s",
                    len(games),
                    self.steam_id,
                )
                return games  # noqa: TRY300

            except requests.HTTPError as e:
                if e.response.status_code != requests.codes.too_many_requests:
                    raise _map_http_error(e) from e

                if attempt < max_retries - 1:
                    delay = base_delay * (2**attempt)
                    logger.warning(
                        "Steam API rate limited (429). "
                        "Retrying in %d seconds (attempt %d/%d)",
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    time.sleep(delay)
                    continue

                msg = "Steam API rate limit exceeded. Please try again later."
                raise MediaImportError(msg) from e

            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    # The same backoff as the rate limit above: a host that
                    # dropped the connection is usually back on the next try,
                    # and the task level retry is an hour away.
                    delay = base_delay * (2**attempt)
                    logger.warning(
                        "Could not reach the Steam API (%s). "
                        "Retrying in %d seconds (attempt %d/%d)",
                        e,
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    time.sleep(delay)
                    continue

                msg = "Could not reach the Steam API. Check your connection."
                raise MediaImportUnexpectedError(msg) from e

        msg = "Steam API request failed after all retries"
        raise MediaImportUnexpectedError(msg)

    def _get_family_shared_games(self):
        """Return the games reachable through Steam family sharing, if enabled.

        Family sharing is read from the web API, which accepts the store
        access token instead of an API key. Everything is best effort: a
        missing or expired token only costs the shared games, the import of
        the owned library keeps going.
        """
        access_token = read_access_token()
        if not access_token:
            return []

        try:
            family_group_id = steam_provider.get_family_group_id(
                access_token,
                self.steam_id,
            )
            if not family_group_id:
                logger.info(
                    "Steam user %s is not in a family group",
                    self.steam_id,
                )
                return []

            apps = steam_provider.get_shared_library(access_token, family_group_id)
        except Exception:
            # Any unexpected answer from a third party API must not break the
            # import of the games the user actually owns
            logger.warning(
                "Could not read the Steam family library of %s",
                self.steam_id,
                exc_info=True,
            )
            return []

        shared = []
        skipped_unplayed = 0
        for app_data in apps:
            appid = app_data.get("appid")
            if not appid:
                continue
            # exclude_reason is 0 for the games that can actually be shared
            if app_data.get("exclude_reason"):
                continue

            playtime = int(app_data.get("rt_playtime") or 0)

            # Family libraries are mostly games that were never launched, so
            # they are dropped unless they were actually played. See the note
            # on FAMILY_IMPORT_PLAYED_ONLY.
            if FAMILY_IMPORT_PLAYED_ONLY and playtime <= 0:
                skipped_unplayed += 1
                continue

            shared.append(
                {
                    "appid": appid,
                    "name": app_data.get("name") or f"Unknown Game {appid}",
                    "playtime_forever": playtime,
                    "playtime_2weeks": 0,
                    "family_shared": True,
                },
            )

        logger.info(
            "Found %d family shared games for Steam user %s (%d unplayed skipped)",
            len(shared),
            self.steam_id,
            skipped_unplayed,
        )
        return shared

    def _process_game(self, game_data):
        """Process a single game from Steam API response."""
        appid = str(game_data["appid"])
        name = game_data.get("name") or f"Unknown Game {appid}"
        # Playtime is in minutes, the same unit as the progress of a game
        playtime_forever = int(game_data.get("playtime_forever") or 0)
        playtime_2weeks = int(game_data.get("playtime_2weeks") or 0)
        notes = "Imported from Steam"
        if game_data.get("family_shared"):
            notes += "（家庭共享）"  # noqa: RUF001

        try:
            existing_game = self.existing_media[MediaTypes.GAME.value][
                Sources.STEAM.value
            ].get(appid)

            # A game that was blocked before and earned an exemption since
            # gets its status back, whatever the import mode is
            if existing_game and appid in self.revived:
                self._revive_blocked(existing_game, playtime_forever, playtime_2weeks)

            if existing_game and self.mode == "overwrite":
                self._queue_existing_game_update(
                    existing_game,
                    playtime_forever,
                    playtime_2weeks,
                )
                return

            # Existing games only get the playtime they are missing
            if existing_game and self.mode == "new":
                self._sync_existing_game(
                    existing_game,
                    playtime_forever,
                    playtime_2weeks,
                )
                return

            if not helpers.should_process_media(
                self.existing_media,
                self.to_delete,
                MediaTypes.GAME.value,
                Sources.STEAM.value,
                appid,
                self.mode,
            ):
                return

            item = self._get_or_create_item(appid, name)

            # Determine status from playtime here; the achievement sync will
            # upgrade it to "completed" once every achievement is unlocked.
            status = self._determine_game_status(playtime_forever, playtime_2weeks)

            # Create game object
            game = app.models.Game(
                item=item,
                user=self.user,
                status=status,
                score=None,
                progress=playtime_forever,
                notes=notes,
                start_date=None,
                end_date=None,
            )

            self.bulk_media[MediaTypes.GAME.value].append(game)

        except (ValueError, KeyError, TypeError) as e:
            logger.warning("Failed to process Steam game %s (%s): %s", name, appid, e)
            self.warnings.append(f"{name} ({appid}): {e!s}")

    def _get_or_create_item(self, appid, name):
        """Return the Steam item of a game, creating it from the app ID alone."""
        item, created = app.models.Item.objects.get_or_create(
            media_id=appid,
            source=Sources.STEAM.value,
            media_type=MediaTypes.GAME.value,
            defaults={
                "title": name,
                "image": primary_cover_url(appid),
            },
        )

        if created:
            self.created_items.append((item, appid))

        return item

    def _queue_existing_game_update(self, game, playtime_forever, playtime_2weeks):
        """Queue updates for an existing game when Steam overwrite is used."""
        changed = False

        if game.progress != playtime_forever:
            game.progress = playtime_forever
            changed = True

        new_status = self._determine_game_status(
            playtime_forever,
            playtime_2weeks,
            achievements_unlocked=game.achievements_unlocked,
            achievements_total=game.achievements_total,
        )
        if (
            game.status
            in [
                Status.PLANNING.value,
                Status.IN_PROGRESS.value,
                Status.PAUSED.value,
            ]
            and game.status != new_status
        ):
            game.status = new_status
            changed = True

        if changed:
            self.bulk_media_updates[MediaTypes.GAME.value].append(game)
            logger.debug("Queued Steam update for existing game %s", game)

    def _sync_existing_game(self, game, playtime_forever, playtime_2weeks):
        """Raise the playtime of an existing game and move its status forward.

        The playtime tracked in Yamtrack can include time played outside Steam,
        so it is never lowered.
        """
        status = self._determine_game_status(
            playtime_forever,
            playtime_2weeks,
            achievements_unlocked=game.achievements_unlocked,
            achievements_total=game.achievements_total,
        )
        changed = helpers.sync_existing_media(
            game,
            {"progress": playtime_forever, "status": status},
        )

        # Planned games that were played but not recently are paused, like in a
        # fresh import, as paused isn't a status an import moves forward to
        if game.status == Status.PLANNING.value and status == Status.PAUSED.value:
            game.status = status
            changed = True

        if changed:
            self.bulk_media_updates[MediaTypes.GAME.value].append(game)
            logger.debug("Queued Steam sync for existing game %s", game)

    def _determine_game_status(
        self,
        playtime_forever,
        playtime_2weeks,
        achievements_unlocked=0,
        achievements_total=0,
    ):
        """Determine game status based on achievements and Steam playtime.

        Args:
            playtime_forever (int): Total playtime in minutes
            playtime_2weeks (int): Playtime in last 2 weeks in minutes
            achievements_unlocked (int): Number of unlocked achievements
            achievements_total (int): Number of achievements the game has

        Returns:
            str: Status value from Status choices

        A fully completed achievement list wins over everything else: the game
        is finished even if it is still being launched. Otherwise the playtime
        decides, from most recent to oldest activity.
        """
        # Every achievement unlocked means the game is finished.
        if achievements_total > 0 and achievements_unlocked >= achievements_total:
            return Status.COMPLETED.value

        # Games with no playtime are considered "Planning"
        if playtime_forever == 0:
            return Status.PLANNING.value

        # Games played in the last 2 weeks are "In Progress"
        if playtime_2weeks > 0:
            return Status.IN_PROGRESS.value

        # Games with total playtime but no recent activity are "On Hold"
        return Status.PAUSED.value
