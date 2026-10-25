from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app.models import Game, Item, MediaTypes, Sources, Status
from integrations.imports import steam


class ImportSteamUpdate(TestCase):
    """Test Steam overwrite behavior for existing games."""

    def setUp(self):
        """Create user and common data for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.item = Item.objects.create(
            media_id="730",
            source=Sources.STEAM.value,
            media_type=MediaTypes.GAME.value,
            title="Counter-Strike 2",
            image="http://example.com/cs2.jpg",
        )

    def _create_game(self, status=Status.PLANNING.value, progress=0, days_old=0):
        """Create a game with a specific status and progress."""
        game = Game.objects.create(
            item=self.item,
            user=self.user,
            status=status,
            progress=progress,
        )
        if days_old:
            # created_at is automatic, an explicit date keeps the ordering stable
            Game.objects.filter(id=game.id).update(
                created_at=timezone.now() - timedelta(days=days_old),
            )
            game.refresh_from_db()

        return game

    def _setup_mock(self, mock_api_request, playtime=1300):
        """Set up the Steam owned games mock."""
        mock_api_request.return_value = {
            "response": {
                "games": [
                    {
                        "appid": 730,
                        "name": "Counter-Strike 2",
                        "playtime_forever": playtime,
                        "playtime_2weeks": 120,
                        "rtime_last_played": 1704067200,
                    },
                ],
            },
        }

    @patch("integrations.imports.steam.services.api_request")
    def test_overwrite_steam_game_updates_existing(self, mock_api_request):
        """Test overwrite mode updates an existing game instead of recreating it."""
        self._setup_mock(mock_api_request)
        game = self._create_game()

        imported_counts, _ = steam.importer(
            "76561198000000000",
            self.user,
            "overwrite",
        )

        game.refresh_from_db()
        self.assertEqual(imported_counts[MediaTypes.GAME.value], 1)
        self.assertEqual(Game.objects.filter(user=self.user).count(), 1)
        self.assertEqual(game.progress, 1300)
        self.assertEqual(game.status, Status.IN_PROGRESS.value)
        self.assertEqual(game.history.count(), 2)

    @patch("integrations.imports.steam.services.api_request")
    def test_overwrite_steam_game_completed_status(self, mock_api_request):
        """Test overwrite mode does not downgrade completed games."""
        self._setup_mock(mock_api_request, playtime=1100)
        game = self._create_game(status=Status.COMPLETED.value, progress=1000)

        imported_counts, _ = steam.importer(
            "76561198000000000",
            self.user,
            "overwrite",
        )

        game.refresh_from_db()
        self.assertEqual(imported_counts[MediaTypes.GAME.value], 1)
        self.assertEqual(game.progress, 1100)
        self.assertEqual(game.status, Status.COMPLETED.value)

    @patch("integrations.imports.steam.services.api_request")
    def test_overwrite_updates_newest_game_instance(self, mock_api_request):
        """Test overwrite mode updates the newest game instance."""
        self._setup_mock(mock_api_request)
        older_game = self._create_game(progress=100, days_old=1)
        newer_game = self._create_game(progress=200)

        imported_counts, _ = steam.importer(
            "76561198000000000",
            self.user,
            "overwrite",
        )

        older_game.refresh_from_db()
        newer_game.refresh_from_db()
        self.assertEqual(imported_counts[MediaTypes.GAME.value], 1)
        self.assertEqual(older_game.progress, 100)
        self.assertEqual(older_game.status, Status.PLANNING.value)
        self.assertEqual(newer_game.progress, 1300)
        self.assertEqual(newer_game.status, Status.IN_PROGRESS.value)

    @patch("integrations.imports.steam.services.api_request")
    def test_new_mode_syncs_existing_game(self, mock_api_request):
        """Test that new mode raises the playtime and advances the status."""
        self._setup_mock(mock_api_request)
        game = self._create_game()

        imported_counts, _ = steam.importer("76561198000000000", self.user, "new")

        game.refresh_from_db()
        self.assertEqual(imported_counts, {MediaTypes.GAME.value: 1})
        self.assertEqual(game.progress, 1300)
        self.assertEqual(game.status, Status.IN_PROGRESS.value)
        self.assertEqual(Game.objects.filter(user=self.user).count(), 1)

    @patch("integrations.imports.steam.services.api_request")
    def test_new_mode_keeps_tracked_game(self, mock_api_request):
        """Test that new mode never lowers playtime or undoes a completed game."""
        self._setup_mock(mock_api_request)
        game = self._create_game(status=Status.COMPLETED.value, progress=2000)

        imported_counts, _ = steam.importer("76561198000000000", self.user, "new")

        game.refresh_from_db()
        self.assertEqual(imported_counts, {})
        self.assertEqual(game.progress, 2000)
        self.assertEqual(game.status, Status.COMPLETED.value)

    @patch("integrations.imports.steam.services.api_request")
    def test_new_mode_pauses_planned_game_played_long_ago(self, mock_api_request):
        """Test that a planned game played but not recently is paused."""
        self._setup_mock(mock_api_request, playtime=600)
        mock_api_request.return_value["response"]["games"][0]["playtime_2weeks"] = 0
        game = self._create_game()

        steam.importer("76561198000000000", self.user, "new")

        game.refresh_from_db()
        self.assertEqual(game.progress, 600)
        self.assertEqual(game.status, Status.PAUSED.value)
