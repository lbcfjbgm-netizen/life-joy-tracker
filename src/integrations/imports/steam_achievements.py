import logging
import time

import requests
from django.conf import settings
from django.utils import timezone

from app.models import Game, MediaTypes, Sources, Status
from app.providers import services
from integrations.imports import steam_achievements_local

logger = logging.getLogger(__name__)

PLAYER_ACHIEVEMENTS_URL = (
    "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/"
)

# Steam is strict about this endpoint, a burst of requests gets answered with
# 429s, so the calls are spaced out instead of being fired together.
DEFAULT_DELAY = 0.5
MAX_RETRIES = 3
RETRY_BASE_DELAY = 5

# The "refresh achievements" button is a person sitting in front of the page
# waiting for an answer, so it gets its own, much shorter, network policy: one
# attempt, a read timeout of a few seconds, and a hard ceiling on the whole
# call (see ``refresh_game_achievements``). The batch jobs above keep the wide
# policy, they run overnight and lose nothing by waiting.
# Measured on this machine with the batch policy: a single click spent 37.9
# seconds on the retries before it gave up, all of it with the button stuck on
# "syncing". The same call with the numbers below answers in under two seconds,
# because the host refuses (403) or the read timeout trips.
FOREGROUND_TIMEOUT = (4, 6)
FOREGROUND_BUDGET = 10
FOREGROUND_ATTEMPTS = 1


def _fetch_achievements(api_key, steam_id, appid, delay, timeout=None, attempts=None):
    """Return the achievement list of a game, None when it can't be fetched.

    ``timeout`` and ``attempts`` are the two knobs the foreground refresh
    shortens; left as None they keep the batch policy above.
    """
    if attempts is None:
        attempts = MAX_RETRIES

    params = {
        "key": api_key,
        "steamid": steam_id,
        "appid": appid,
        "l": "schinese",
    }

    for attempt in range(attempts):
        try:
            response = services.api_request(
                Sources.STEAM.value,
                "GET",
                PLAYER_ACHIEVEMENTS_URL,
                params=params,
                timeout=timeout,
            )
            break
        except requests.exceptions.HTTPError as error:
            status_code = getattr(error.response, "status_code", None)
            if (
                status_code != requests.codes.too_many_requests
                or attempt == attempts - 1
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


def _local_counters(game, appid):
    """Return the counters the Steam client cache holds for a game, None if any.

    The cache may be older than the database, so it is only ever allowed to
    raise a number: a total the database already holds stays (the cache's
    ``nTotal`` can lag behind Steam's real one, measured 500 against 666 on
    ``2218750``), and an unlocked count never goes backwards.

    Args:
        game: the Game row being synced
        appid (str): the Steam App ID of the game

    Returns:
        tuple | None: ``(total, unlocked)``
    """
    cached = steam_achievements_local.read_achievements(appid)
    if cached is None:
        return None

    cached_total, cached_unlocked = cached
    total = max(cached_total, game.achievements_total)
    unlocked = max(cached_unlocked, game.achievements_unlocked)
    if total:
        unlocked = min(unlocked, total)

    return total, unlocked


def _counters_for_game(game, api_key, steam_id, appid, delay, foreground=False):
    """Return the counters to store for one game, None when none can be read.

    The API is asked first. Only when it answers nothing (403 for a private
    profile, no achievement system, or a network failure) is the cache the
    Steam client keeps on this machine read instead.

    The cache never overwrites a total the database already holds: its
    ``nTotal`` is what the client last saw, and it can be lower than Steam's
    real one (measured: ``2218750`` 500 here against 666 official,
    ``1829470`` 12 against 14), so filling it in twice would shrink the number.
    The total returned is therefore never below the one the database holds.

    A cache that reports the same numbers as the database is still a success:
    it is the only source this machine has for many games (the profile is
    private, so the API answers 403 forever), and "the counters did not move"
    means "nothing new was unlocked", not "the sync failed". Failures are
    reserved for the case where neither source has anything to say.

    Args:
        game: the Game row being synced
        api_key (str): the Steam Web API key
        steam_id (str): the 64-bit Steam ID
        appid (str): the Steam App ID of the game
        delay (float): seconds to wait after the API call
        foreground (bool): True for the single game refresh a person waits on

    Returns:
        tuple | None: ``(total, unlocked, from_local)``
    """
    if foreground:
        with services.interactive_requests(budget=FOREGROUND_BUDGET):
            achievements = _fetch_achievements(
                api_key,
                steam_id,
                appid,
                delay,
                timeout=FOREGROUND_TIMEOUT,
                attempts=FOREGROUND_ATTEMPTS,
            )
    else:
        achievements = _fetch_achievements(api_key, steam_id, appid, delay)

    if achievements is not None:
        unlocked = sum(1 for achievement in achievements if achievement.get("achieved"))
        return len(achievements), unlocked, False

    local = _local_counters(game, appid)
    if local is None:
        return None

    return local[0], local[1], True



def refresh_game_achievements(game, steam_id, delay=0):
    """Refresh the achievements of a single tracked game, right now.

    ``sync_achievements`` above walks every Steam game of a user, so it filters
    on the item source and cannot reach a hand entered game whose ``media_id``
    happens to be a Steam App ID. This one takes the game object directly and
    therefore works for both.

    Args:
        game: the Game row to refresh
        steam_id (str): the 64-bit Steam ID the achievements are read from
        delay (float): seconds to wait after the API call, 0 for a single sync
            a person is waiting on

    Returns:
        tuple: ``(ok, from_local)`` — ``ok`` is False only when neither the API
        nor the Steam client cache had anything to say (no API key, a game
        without an achievement system, an empty cache), in which case the
        stored counters are left untouched. A cache that simply repeats what
        the database already holds is a success: nothing new was unlocked is
        not the same as the refresh failing.
    """
    api_key = settings.STEAM_API_KEY
    if not api_key:
        logger.warning("Steam API key not configured, achievements not synced")
        return False, False

    appid = game.item.media_id

    try:
        # foreground=True: a person is waiting on the button, so the short
        # timeout and the single attempt of FOREGROUND_* apply here and only
        # here. The batch jobs keep the tolerant policy.
        counters = _counters_for_game(
            game,
            api_key,
            steam_id,
            appid,
            delay,
            foreground=True,
        )
    except services.ProviderAPIError:
        # A provider that is flagged down raises before a request goes out; the
        # page must say "Steam gave nothing" instead of crashing on it.
        logger.warning("Achievements of appid %s unavailable", appid)
        return False, False

    if counters is None:
        logger.info("Skipped the achievements of appid %s", appid)
        return False, False

    total, unlocked, from_local = counters
    game.achievements_total = total
    game.achievements_unlocked = unlocked
    game.achievements_synced_at = timezone.now()
    # A read that worked clears the failure streak, otherwise a game Steam
    # briefly refused would stay blacklisted after it started answering.
    game.achievement_sync_failures = 0

    update_fields = [
        "achievements_total",
        "achievements_unlocked",
        "achievements_synced_at",
        "achievement_sync_failures",
    ]

    # A fully unlocked achievement list means the game is finished, same rule
    # the batch sync applies.
    if (
        game.achievements_total > 0
        and unlocked >= game.achievements_total
        and game.status != Status.COMPLETED.value
    ):
        game.status = Status.COMPLETED.value
        update_fields.append("status")

    # Written with a queryset update, exactly like the batch sync writes its
    # games: ``Game.save()`` runs ``process_status()`` whenever the status
    # changed, and that reaches into the providers for a metadata refresh the
    # refresh button has no use for. Only these columns are meant to change.
    Game.objects.filter(pk=game.pk).update(
        **{field: getattr(game, field) for field in update_fields},
    )

    logger.info(
        "Refreshed the achievements of %s: %s/%s",
        game,
        unlocked,
        total,
    )

    return True, from_local


def sync_achievements(
    user,
    steam_id,
    appids=None,
    delay=DEFAULT_DELAY,
    local_only=False,
):
    """Sync the Steam achievement progress of a user's games.

    Args:
        user: the user the games belong to
        steam_id (str): the 64-bit Steam ID the achievements are read from
        appids (list): only sync these app IDs, all Steam games when omitted
        delay (float): seconds to wait between two API calls
        local_only (bool): read the Steam client cache only, no request goes out

    Returns:
        dict: how many games were synced (``from_local`` of them from the
        Steam client cache, see ``steam_achievements_local``) and skipped
    """
    api_key = settings.STEAM_API_KEY
    if not api_key and not local_only:
        logger.warning("Steam API key not configured, achievements not synced")
        return {"synced": 0, "skipped": 0, "from_local": 0}

    # Both sources are walked on purpose. A hand entered game whose media_id is
    # a plain App ID (the "own it on Steam but added it by hand" case) used to
    # be invisible to every batch job, because they all filtered on
    # item__source=steam. That filter is only a stand-in for "there is a numeric
    # App ID, so a cache file may exist", and it is the wrong stand-in: the
    # number is what makes the cache lookup possible, not the source. Filtering
    # on the number instead keeps hand entered entries with a uuid media_id out
    # without a per row check, and every Steam game already has a numeric one.
    games = Game.objects.filter(
        user=user,
        item__media_type=MediaTypes.GAME.value,
        item__media_id__regex=r"^\d{1,12}$",
    ).select_related("item")

    if appids:
        games = games.filter(item__media_id__in=[str(appid) for appid in appids])

    games = list(games)
    if not games:
        logger.info("No Steam games to sync achievements for user %s", user.username)
        return {"synced": 0, "skipped": 0, "from_local": 0}

    synced = 0
    skipped = 0
    from_local = 0
    to_update = []
    now = timezone.now()

    # A batch job: nobody is waiting, and the loop is thousands of games long,
    # so the short circuit of the provider must be the tolerant one. Otherwise
    # a single slow answer marks Steam down and the rest of the run is answered
    # from the flag without a request going out (measured: 2011 games "synced"
    # in 34 seconds, none of them touched).
    with services.background_requests():
        for game in games:
            appid = game.item.media_id
            if local_only:
                # No request goes out: this is the rescue pass for the games the
                # failure streak already gave up on (see
                # ``rescue_blacklisted_achievements`` in ``steam_recent``).
                local = _local_counters(game, appid)
                counters = None if local is None else (local[0], local[1], True)
            else:
                counters = _counters_for_game(game, api_key, steam_id, appid, delay)

            if counters is None:
                # Steam gave nothing for this app id (403 for a private
                # profile, no achievement system, network trouble). The streak
                # is counted so the backfill can give up on it, see
                # ``MAX_ACHIEVEMENT_FAILURES`` in ``steam_recent``. The rescue
                # pass leaves the streak alone: those games are already given
                # up on, and counting a cache miss would only grow the number.
                if not local_only:
                    game.achievement_sync_failures += 1
                skipped += 1
                to_update.append(game)
                continue

            total, unlocked, from_cache = counters
            game.achievements_total = total
            game.achievements_unlocked = unlocked
            game.achievements_synced_at = now
            game.achievement_sync_failures = 0

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
            if from_cache:
                from_local += 1

    if to_update:
        Game.objects.bulk_update(
            to_update,
            [
                "achievements_total",
                "achievements_unlocked",
                "achievements_synced_at",
                "achievement_sync_failures",
                "status",
            ],
        )

    logger.info(
        "Synced Steam achievements for user %s: %s synced (%s from the local "
        "cache), %s skipped",
        user.username,
        synced,
        from_local,
        skipped,
    )

    return {"synced": synced, "skipped": skipped, "from_local": from_local}
