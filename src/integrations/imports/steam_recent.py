"""Keep the library in step with what was actually played lately.

The daily Steam import reads the whole library, which is far too much work to
do more than once a day. What changes between two runs is small: the handful of
games played during the last two weeks. This module looks only at those.

Two rules drive it:

* a game that is not tracked yet is imported like the full import would;
* a game the operator already edited by hand (rated, annotated, dropped or
  paused) only ever gets its playtime raised and its achievements refreshed,
  never anything else.

The achievements are refreshed for exactly the games of that run, so the cost of
a daily pass stays a handful of requests instead of thousands.
"""

import logging
import time

from django.conf import settings

from app.models import Game, MediaTypes, Sources, Status
from integrations.imports import helpers, steam, steam_achievements

logger = logging.getLogger(__name__)

# Notes the Steam import writes on its own. A game carrying one of those was not
# annotated by the operator, so it is not a sign of a hand edited entry.
AUTO_NOTE_PREFIX = "Imported from Steam"

# Steam answers a burst of achievement requests with 429s. The games are synced
# in batches with a pause in between, which keeps a full backfill under the
# limit without making it crawl.
BACKFILL_BATCH_SIZE = 200
BACKFILL_BATCH_PAUSE = 2

# How many times a game may be asked for its achievements before it is given
# up on. Steam answers 403 for a private profile and 400 for a title without
# an achievement system, and both are stable: the tenth attempt costs the same
# request as the first and is just as empty. Measured on this library, roughly
# a third of the games never answer, so retrying them forever is what turns a
# 90 minute run into a nightly waste.
MAX_ACHIEVEMENT_FAILURES = 10

# Every batch job below selects games by "has a numeric App ID" rather than by
# "was imported from Steam". The App ID is what a cache lookup needs, and the
# Steam client cache is a perfectly good source for a game the operator added by
# hand while owning it on Steam. Measured on this library, the six hand entered
# games all carry a uuid media_id, so nothing changes today; the number is the
# honest condition, and it keeps a future hand entered App ID from being
# invisible to the daily passes.
NUMERIC_APPID_REGEX = r"^\d{1,12}$"


def _has_user_note(game):
    """Return whether the notes of a game were written by the operator."""
    notes = (game.notes or "").strip()
    return bool(notes) and not notes.startswith(AUTO_NOTE_PREFIX)


def _is_user_curated(game):
    """Return whether the operator edited the game by hand.

    A rating, a note of their own, or the game being dropped or paused all mean
    the entry reflects a decision, so a sync must not overrule it.
    """
    return (
        game.score is not None
        or _has_user_note(game)
        or game.status in (Status.DROPPED.value, Status.PAUSED.value)
    )


class RecentGameImporter(steam.SteamImporter):
    """Steam importer restricted to the games played in the last two weeks."""

    def __init__(self, steam_id, user, played, verify_covers=steam.VERIFY_COVERS):
        """Initialise the importer with the games of that run.

        Args:
            steam_id (str): the 64-bit Steam ID the library is read from
            user: the Django user the games belong to
            played (list): the recently played entries of the Steam API, as
                returned by ``steam.fetch_recently_played_games``
            verify_covers (bool): check the covers exist on the Steam CDN
        """
        self.played = list(played)
        super().__init__(steam_id, user, "new", verify_covers=verify_covers)
        self.created = 0
        self.updated = 0
        self.curated = 0
        self.skipped_blocked = 0

    def import_data(self):
        """Import the new games of the run and sync the existing ones.

        Returns:
            dict: how many games were seen, created, updated and left alone
        """
        # Read once per run, like the full import: a rebuilt blocklist is picked
        # up by the next pass
        self.blocklist = steam.read_blocklist()
        # The blocklist exemption compares integers, the same type the playtime
        # API hands out
        self.recently_played = {game["appid"] for game in self.played}
        self.revived = set()

        games = [game for game in self.played if not self._is_blocked(game)]

        for game_data in games:
            self._process_recent_game(game_data)

        helpers.cleanup_existing_media(self.to_delete, self.user)
        helpers.bulk_create_media(self.bulk_media, self.user)
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

        logger.info(
            "Recent Steam sync for user %s: %s played, %s created, "
            "%s updated, %s untouched by hand, %s blocked",
            self.user.username,
            len(self.played),
            self.created,
            self.updated,
            self.curated,
            self.skipped_blocked,
        )

        return {
            "recent": len(self.played),
            "created": self.created,
            "updated": self.updated,
            "curated": self.curated,
            "blocked": self.skipped_blocked,
        }

    def _process_recent_game(self, game_data):
        """Create a missing game, or sync the one already tracked."""
        appid = str(game_data["appid"])
        playtime_forever = int(game_data.get("playtime_forever") or 0)
        playtime_2weeks = int(game_data.get("playtime_2weeks") or 0)

        existing = self.existing_media[MediaTypes.GAME.value][Sources.STEAM.value].get(
            appid
        )

        if existing is None:
            # A game never seen before goes through the regular import path,
            # which builds the item, the cover and the starting status
            created_before = len(self.bulk_media[MediaTypes.GAME.value])
            self._process_game(game_data)
            if len(self.bulk_media[MediaTypes.GAME.value]) > created_before:
                self.created += 1
            return

        if _is_user_curated(existing):
            self._sync_curated_game(existing, playtime_forever)
            self.curated += 1
            return

        self._sync_existing_game(
            existing,
            playtime_forever,
            playtime_2weeks,
            last_played=int(game_data.get("rtime_last_played") or 0) or None,
        )
        self.updated += 1

    def _sync_curated_game(self, game, playtime_forever):
        """Raise the playtime of a hand curated game, without touching the rest.

        Only the playtime is passed along: the score, notes, status and dates of
        the entry stay exactly as the operator left them.
        """
        changed = helpers.sync_existing_media(game, {"progress": playtime_forever})
        if not changed:
            return

        queued = self.bulk_media_updates[MediaTypes.GAME.value]
        if game not in queued:
            queued.append(game)
        logger.debug("Raised the playtime of the curated game %s", game)


def sync_recent_games(user, steam_id):
    """Sync the games played during the last two weeks, achievements included.

    Args:
        user: the Django user the games belong to
        steam_id (str): the 64-bit Steam ID the library is read from

    Returns:
        dict: the import counts and the achievement counts of the run
    """
    empty = {
        "recent": 0,
        "created": 0,
        "updated": 0,
        "curated": 0,
        "blocked": 0,
        "achievements": {"synced": 0, "skipped": 0},
    }

    api_key = settings.STEAM_API_KEY
    if not api_key:
        logger.warning("Steam API key not configured, recent games not synced")
        return empty

    played = steam.fetch_recently_played_games(api_key, steam_id)
    if not played:
        # An empty answer is normal: nobody played anything for two weeks, or
        # the endpoint could not be read. Neither is worth an exception, the
        # next run tries again.
        logger.info(
            "No Steam game played in the last two weeks for %s, nothing to sync",
            steam_id,
        )
        return empty

    counts = RecentGameImporter(steam_id, user, played).import_data()

    # Only this run's games are asked for, never the whole library
    counts["achievements"] = steam_achievements.sync_achievements(
        user,
        steam_id,
        appids=[game["appid"] for game in played],
    )
    return counts


def backfill_achievements(
    user,
    steam_id,
    batch_size=BACKFILL_BATCH_SIZE,
    delay=steam_achievements.DEFAULT_DELAY,
):
    """Sync the achievements of the games that never had them.

    A one-time job for the entries imported before the achievement sync was
    scheduled. The work is done in batches so a rate limit costs one batch
    instead of the whole run.

    Args:
        user: the Django user the games belong to
        steam_id (str): the 64-bit Steam ID the achievements are read from
        batch_size (int): how many games are synced per batch
        delay (float): seconds to wait between two achievement calls

    Returns:
        dict: how many games were pending and how they ended
    """
    if not settings.STEAM_API_KEY:
        logger.warning("Steam API key not configured, achievements not backfilled")
        return {"total": 0, "synced": 0, "skipped": 0, "batches": 0}

    # Read once, before the loop: the query would otherwise shrink under the
    # cursor as the synced games leave the "never synced" filter, and the
    # offset of every batch would skip as many games as the previous batch
    # filled in.
    appids = list(
        Game.objects.filter(
            user=user,
            item__media_type=MediaTypes.GAME.value,
            item__media_id__regex=NUMERIC_APPID_REGEX,
            achievements_synced_at__isnull=True,
            achievement_sync_failures__lt=MAX_ACHIEVEMENT_FAILURES,
        ).values_list("item__media_id", flat=True),
    )

    total = len(appids)
    if not total:
        logger.info("No Steam game is missing its achievements for %s", user.username)
        return {"total": 0, "synced": 0, "skipped": 0, "batches": 0}

    logger.info(
        "Backfilling the achievements of %s Steam games for %s",
        total,
        user.username,
    )

    synced = 0
    skipped = 0
    batches = 0
    for start in range(0, total, batch_size):
        chunk = appids[start : start + batch_size]
        result = steam_achievements.sync_achievements(
            user,
            steam_id,
            appids=chunk,
            delay=delay,
        )
        synced += result["synced"]
        skipped += result["skipped"]
        batches += 1

        done = min(start + batch_size, total)
        logger.info(
            "Backfilled %s/%s Steam achievements (%s synced, %s skipped so far)",
            done,
            total,
            synced,
            skipped,
        )

        if done < total:
            time.sleep(BACKFILL_BATCH_PAUSE)

    logger.info(
        "Achievement backfill done for %s: %s synced, %s skipped out of %s",
        user.username,
        synced,
        skipped,
        total,
    )
    return {"total": total, "synced": synced, "skipped": skipped, "batches": batches}


def refresh_played_achievements(
    user,
    steam_id,
    batch_size=BACKFILL_BATCH_SIZE,
    delay=steam_achievements.DEFAULT_DELAY,
):
    """Re-read the achievements of every game the operator actually played.

    The backfill only ever looks at the games that never answered once, so a
    game that was synced a year ago keeps its year old counters forever. Steam
    does not push anything, and the daily recent-games pass only covers the two
    last weeks, which leaves the long tail stale.

    This one is the opposite filter: every Steam game with a playtime above
    zero, whether it was synced already or not. Unplayed games are left out on
    purpose, there is nothing to unlock in a game nobody launched, and a game
    that kept failing is skipped so one bad app id cannot hold the whole run
    for ten minutes every week.

    It is a weekly job, not a before-bed one: nothing waits for it, and the
    batches are paced exactly like the backfill.

    Args:
        user: the Django user the games belong to
        steam_id (str): the 64-bit Steam ID the achievements are read from
        batch_size (int): how many games are synced per batch
        delay (float): seconds to wait between two achievement calls

    Returns:
        dict: how many games were rescanned and how they ended
    """
    if not settings.STEAM_API_KEY:
        logger.warning("Steam API key not configured, achievements not refreshed")
        return {"total": 0, "synced": 0, "skipped": 0, "batches": 0}

    appids = list(
        Game.objects.filter(
            user=user,
            item__media_type=MediaTypes.GAME.value,
            item__media_id__regex=NUMERIC_APPID_REGEX,
            progress__gt=0,
            achievement_sync_failures__lt=MAX_ACHIEVEMENT_FAILURES,
        ).values_list("item__media_id", flat=True),
    )

    total = len(appids)
    if not total:
        logger.info("No played Steam game to refresh for %s", user.username)
        return {"total": 0, "synced": 0, "skipped": 0, "batches": 0}

    logger.info(
        "Refreshing the achievements of %s played Steam games for %s",
        total,
        user.username,
    )

    synced = 0
    skipped = 0
    batches = 0
    for start in range(0, total, batch_size):
        chunk = appids[start : start + batch_size]
        result = steam_achievements.sync_achievements(
            user,
            steam_id,
            appids=chunk,
            delay=delay,
        )
        synced += result["synced"]
        skipped += result["skipped"]
        batches += 1

        done = min(start + batch_size, total)
        logger.info(
            "Refreshed %s/%s played Steam achievements (%s synced, %s skipped so far)",
            done,
            total,
            synced,
            skipped,
        )

        if done < total:
            time.sleep(BACKFILL_BATCH_PAUSE)

    # The games that were given up on are rescued from the Steam client cache
    # afterwards, never from the API: that is the whole point of giving up on
    # them, and a run of ten refusals must not be replayed every night.
    rescue_blacklisted_achievements(user, steam_id, batch_size)

    logger.info(
        "Played achievement refresh done for %s: %s synced, %s skipped out of %s",
        user.username,
        synced,
        skipped,
        total,
    )
    return {"total": total, "synced": synced, "skipped": skipped, "batches": batches}


def rescue_blacklisted_achievements(user, steam_id, batch_size=BACKFILL_BATCH_SIZE):
    """Refresh the given up games from the Steam client cache alone.

    A game reaches ``MAX_ACHIEVEMENT_FAILURES`` when Steam refused it ten times
    in a row, which is what a private profile looks like from the outside. For
    many of those games the Steam client on this very machine still holds the
    counters, but every batch job skips them on the failure count, so the cache
    is never read and they stay frozen at the counters of the day they were
    given up on.

    This pass ignores the streak and reads the cache. A hit resets the streak,
    so the game is back in the regular passes; a miss leaves the row untouched,
    there is nothing to write and no reason to count another failure.

    Args:
        user: the Django user the games belong to
        steam_id (str): the 64-bit Steam ID the achievements are read from
        batch_size (int): how many games are read per batch

    Returns:
        dict: how many games were given up on, rescued and left alone
    """
    appids = list(
        Game.objects.filter(
            user=user,
            item__media_type=MediaTypes.GAME.value,
            item__media_id__regex=NUMERIC_APPID_REGEX,
            achievement_sync_failures__gte=MAX_ACHIEVEMENT_FAILURES,
        ).values_list("item__media_id", flat=True),
    )

    empty = {"blacklisted": len(appids), "rescued": 0, "batches": 0}
    if not appids:
        return empty

    logger.info(
        "Reading the Steam client cache for %s given up games of %s",
        len(appids),
        user.username,
    )

    rescued = 0
    batches = 0
    for start in range(0, len(appids), batch_size):
        chunk = appids[start : start + batch_size]
        result = steam_achievements.sync_achievements(
            user,
            steam_id,
            appids=chunk,
            local_only=True,
        )
        rescued += result["synced"]
        batches += 1

    logger.info(
        "Rescued %s/%s given up games of %s from the Steam client cache",
        rescued,
        len(appids),
        user.username,
    )
    return {"blacklisted": len(appids), "rescued": rescued, "batches": batches}

