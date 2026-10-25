import logging
import time

import requests
from django.conf import settings
from django.utils import timezone

from app.models import Game, MediaTypes, Sources, Status
from app.providers import services

logger = logging.getLogger(__name__)

PLAYER_ACHIEVEMENTS_URL = (
    "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/"
)

# Steam is strict about this endpoint, a burst of requests gets answered with
# 429s, so the calls are spaced out instead of being fired together.
DEFAULT_DELAY = 0.5
MAX_RETRIES = 3
RETRY_BASE_DELAY = 5


def _fetch_achievements(api_key, steam_id, appid, delay):
    """Return the achievement list of a game, None when it can't be fetched."""
    params = {
        "key": api_key,
        "steamid": steam_id,
        "appid": appid,
        "l": "schinese",
    }

    for attempt in range(MAX_RETRIES):
        try:
            response = services.api_request(
                Sources.STEAM.value,
                "GET",
                PLAYER_ACHIEVEMENTS_URL,
                params=params,
            )
            break
        except requests.exceptions.HTTPError as error:
            status_code = getattr(error.response, "status_code", None)
            if (
                status_code != requests.codes.too_many_requests
                or attempt == MAX_RETRIES - 1
            ):
                logger.warning(
                    "Skipped the achievements of appid %s (HTTP %s)",
                    appid,
                    status_code,
                )
                return None

            wait = RETRY_BASE_DELAY * (2**attempt)
            logger.warning(
                "Steam rate limited the achievements of appid %s, "
                "retrying in %s seconds",
                appid,
                wait,
            )
            time.sleep(wait)
        except requests.RequestException:
            logger.warning("Could not reach Steam for appid %s", appid)
            return None

    time.sleep(delay)

    stats = response.get("playerstats") or {}

    # success is false for games without achievements (or private profiles)
    if not stats.get("success") or "achievements" not in stats:
        return None

    return stats["achievements"]


def sync_achievements(user, steam_id, appids=None, delay=DEFAULT_DELAY):
    """Sync the Steam achievement progress of a user's games.

    Args:
        user: the user the games belong to
        steam_id (str): the 64-bit Steam ID the achievements are read from
        appids (list): only sync these app IDs, all Steam games when omitted
        delay (float): seconds to wait between two API calls

    Returns:
        dict: the number of synced and skipped games
    """
    api_key = settings.STEAM_API_KEY
    if not api_key:
        logger.warning("Steam API key not configured, achievements not synced")
        return {"synced": 0, "skipped": 0}

    games = Game.objects.filter(
        user=user,
        item__source=Sources.STEAM.value,
        item__media_type=MediaTypes.GAME.value,
    ).select_related("item")

    if appids:
        games = games.filter(item__media_id__in=[str(appid) for appid in appids])

    games = list(games)
    if not games:
        logger.info("No Steam games to sync achievements for user %s", user.username)
        return {"synced": 0, "skipped": 0}

    synced = 0
    skipped = 0
    to_update = []
    now = timezone.now()

    for game in games:
        appid = game.item.media_id
        achievements = _fetch_achievements(api_key, steam_id, appid, delay)

        if achievements is None:
            skipped += 1
            continue

        unlocked = sum(1 for achievement in achievements if achievement.get("achieved"))
        game.achievements_total = len(achievements)
        game.achievements_unlocked = unlocked
        game.achievements_synced_at = now

        # A fully unlocked achievement list means the game is finished. This
        # runs after the playtime based import, which cannot know about the
        # achievements yet.
        if (
            game.achievements_total > 0
            and unlocked >= game.achievements_total
            and game.status != Status.COMPLETED.value
        ):
            game.status = Status.COMPLETED.value

        to_update.append(game)
        synced += 1

    if to_update:
        Game.objects.bulk_update(
            to_update,
            [
                "achievements_total",
                "achievements_unlocked",
                "achievements_synced_at",
                "status",
            ],
        )

    logger.info(
        "Synced Steam achievements for user %s: %s synced, %s skipped",
        user.username,
        synced,
        skipped,
    )

    return {"synced": synced, "skipped": skipped}
