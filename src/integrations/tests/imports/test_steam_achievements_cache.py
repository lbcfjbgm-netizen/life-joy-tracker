from unittest.mock import patch

from django.test import TestCase

from app.models import Game, Item, MediaTypes, Sources, Status
from integrations.imports import steam_achievements


def _game(appid, total=0, unlocked=0, source=None):
    """Return an unsaved Game row carrying the achievement counters given."""
    item = Item(
        media_id=appid,
        source=source or Sources.STEAM.value,
        media_type=MediaTypes.GAME.value,
        title=f"Game {appid}",
        image="http://example.com/cover.jpg",
    )
    return Game(
        item=item,
        achievements_total=total,
        achievements_unlocked=unlocked,
        status=Status.IN_PROGRESS.value,
    )


@patch("integrations.imports.steam_achievements._fetch_achievements", return_value=None)
class LocalCacheCountersTest(TestCase):
    """Test what the Steam client cache contributes to the counters.

    The API never answers on this machine (403 for a private profile), so the
    cache is the only source. These tests pin the two rules of that fallback:
    a cache hit is a success even when nothing moved, and the cache can only
    ever raise a number, never shrink the total the database holds.
    """

    @patch(
        "integrations.imports.steam_achievements.steam_achievements_local.read_achievements",
        return_value=(60, 18),
    )
    def test_identical_numbers_are_a_success(self, _mock_read, _mock_fetch):
        """A cache that repeats the database is a sync, not a failure."""
        game = _game("2967950", total=60, unlocked=18)

        result = steam_achievements._counters_for_game(
            game,
            "key",
            "76561190000000000",
            "2967950",
            0,
        )

        self.assertEqual(result, (60, 18, True))

    @patch(
        "integrations.imports.steam_achievements.steam_achievements_local.read_achievements",
        return_value=(60, 18),
    )
    def test_empty_database_is_filled_from_the_cache(self, _mock_read, _mock_fetch):
        """A game that never answered once is filled in from the cache."""
        game = _game("2967950")

        result = steam_achievements._counters_for_game(
            game,
            "key",
            "76561190000000000",
            "2967950",
            0,
        )

        self.assertEqual(result, (60, 18, True))

    @patch(
        "integrations.imports.steam_achievements.steam_achievements_local.read_achievements",
        return_value=(500, 120),
    )
    def test_cache_never_shrinks_the_total(self, _mock_read, _mock_fetch):
        """A stale cache total does not overwrite the official one."""
        game = _game("2218750", total=666, unlocked=100)

        result = steam_achievements._counters_for_game(
            game,
            "key",
            "76561190000000000",
            "2218750",
            0,
        )

        self.assertEqual(result, (666, 120, True))

    @patch(
        "integrations.imports.steam_achievements.steam_achievements_local.read_achievements",
        return_value=(60, 5),
    )
    def test_cache_never_loses_unlocked(self, _mock_read, _mock_fetch):
        """An older cache with fewer unlocked achievements writes nothing back."""
        game = _game("2967950", total=60, unlocked=18)

        result = steam_achievements._counters_for_game(
            game,
            "key",
            "76561190000000000",
            "2967950",
            0,
        )

        self.assertEqual(result, (60, 18, True))

    @patch(
        "integrations.imports.steam_achievements.steam_achievements_local.read_achievements",
        return_value=(60, 99),
    )
    def test_unlocked_is_capped_by_the_total(self, _mock_read, _mock_fetch):
        """A cache written mid update cannot report more than 100 percent."""
        game = _game("2967950", total=60, unlocked=18)

        result = steam_achievements._counters_for_game(
            game,
            "key",
            "76561190000000000",
            "2967950",
            0,
        )

        self.assertEqual(result, (60, 60, True))

    @patch(
        "integrations.imports.steam_achievements.steam_achievements_local.read_achievements",
        return_value=None,
    )
    def test_no_api_and_no_cache_is_a_failure(self, _mock_read, _mock_fetch):
        """With neither source there is nothing to write."""
        game = _game("2967950", total=60, unlocked=18)

        result = steam_achievements._counters_for_game(
            game,
            "key",
            "76561190000000000",
            "2967950",
            0,
        )

        self.assertIsNone(result)

    @patch(
        "integrations.imports.steam_achievements.steam_achievements_local.read_achievements",
        return_value=(60, 18),
    )
    def test_api_answer_wins_over_the_cache(self, mock_read, mock_fetch):
        """A live API answer is used as is, the cache is not consulted."""
        mock_fetch.return_value = [
            {"apiname": "A", "achieved": 1},
            {"apiname": "B", "achieved": 0},
        ]
        game = _game("2967950", total=60, unlocked=18)

        result = steam_achievements._counters_for_game(
            game,
            "key",
            "76561190000000000",
            "2967950",
            0,
        )

        self.assertEqual(result, (2, 1, False))
        self.assertFalse(mock_read.called)


class LocalOnlySyncTest(TestCase):
    """Test the cache only pass used to rescue the given up games."""

    def setUp(self):
        """Create a user with one given up Steam game."""
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(
            username="rescued",
            password="12345",  # noqa: S106
        )
        item = Item.objects.create(
            media_id="2967950",
            source=Sources.STEAM.value,
            media_type=MediaTypes.GAME.value,
            title="The Witch's Bakery",
            image="http://example.com/cover.jpg",
        )
        self.game = Game.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            achievements_total=60,
            achievements_unlocked=18,
            achievement_sync_failures=10,
        )

    @patch("app.providers.services.session.get")
    @patch(
        "integrations.imports.steam_achievements.steam_achievements_local.read_achievements",
        return_value=(60, 18),
    )
    def test_local_only_never_calls_the_api(self, _mock_read, mock_get):
        """The rescue pass reads the cache and sends no request."""
        result = steam_achievements.sync_achievements(
            self.user,
            "76561190000000000",
            appids=["2967950"],
            delay=0,
            local_only=True,
        )

        self.assertFalse(mock_get.called)
        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["from_local"], 1)

        self.game.refresh_from_db()
        self.assertEqual(self.game.achievement_sync_failures, 0)
        self.assertIsNotNone(self.game.achievements_synced_at)

    @patch("app.providers.services.session.get")
    @patch(
        "integrations.imports.steam_achievements.steam_achievements_local.read_achievements",
        return_value=None,
    )
    def test_local_only_miss_keeps_the_streak(self, _mock_read, mock_get):
        """A cache miss neither writes nor counts another failure."""
        result = steam_achievements.sync_achievements(
            self.user,
            "76561190000000000",
            appids=["2967950"],
            delay=0,
            local_only=True,
        )

        self.assertFalse(mock_get.called)
        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["skipped"], 1)

        self.game.refresh_from_db()
        self.assertEqual(self.game.achievement_sync_failures, 10)


class NumericAppIdFilterTest(TestCase):
    """Test that the batch jobs select by App ID, not by the item source."""

    def setUp(self):
        """Create one Steam game and one hand entered game with an App ID."""
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(
            username="numeric",
            password="12345",  # noqa: S106
        )
        for appid, source in (("620", Sources.STEAM.value), ("730", Sources.MANUAL.value)):
            item = Item.objects.create(
                media_id=appid,
                source=source,
                media_type=MediaTypes.GAME.value,
                title=f"Game {appid}",
                image="http://example.com/cover.jpg",
            )
            Game.objects.create(
                item=item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
            )

    @patch(
        "integrations.imports.steam_achievements._counters_for_game",
        return_value=None,
    )
    def test_hand_entered_app_id_is_synced(self, mock_counters):
        """A hand entered game with a plain App ID is not left out."""
        steam_achievements.sync_achievements(
            self.user,
            "76561190000000000",
            delay=0,
        )

        appids = {call.args[3] for call in mock_counters.call_args_list}
        self.assertEqual(appids, {"620", "730"})

    def test_uuid_media_id_is_left_out(self):
        """A hand entered entry with a uuid is not a Steam game."""
        item = Item.objects.create(
            media_id="7f9c2f3a-1b2c-4d5e-8f90-1234567890ab",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.GAME.value,
            title="Hand typed",
            image="http://example.com/cover.jpg",
        )
        Game.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        appids = list(
            Game.objects.filter(
                user=self.user,
                item__media_type=MediaTypes.GAME.value,
                item__media_id__regex=r"^\d{1,12}$",
            ).values_list("item__media_id", flat=True),
        )

        self.assertNotIn("7f9c2f3a-1b2c-4d5e-8f90-1234567890ab", appids)
        self.assertEqual(sorted(appids), ["620", "730"])
