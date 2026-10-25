from unittest.mock import patch

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from requests import Response
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import HTTPError

from app.models import Game, MediaTypes, Sources, Status
from integrations.imports import helpers, steam


class ImportSteam(TestCase):
    """Test importing media from Steam."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        # Never hit the cover CDN from a unit test: the checks are enabled by
        # default, so they are silenced here and asserted on where it matters.
        cover_check = patch("integrations.imports.steam._check_cover")
        self.mock_cover_check = cover_check.start()
        self.mock_cover_check.return_value = True
        self.addCleanup(cover_check.stop)

    @patch("integrations.imports.steam.services.api_request")
    def test_import_steam_games(self, mock_api_request):
        """Test importing games from Steam without any IGDB lookup."""
        mock_api_request.return_value = {
            "response": {
                "games": [
                    {
                        "appid": 730,
                        "name": "Counter-Strike 2",
                        "playtime_forever": 1250,
                        "playtime_2weeks": 120,  # Recent activity
                        "rtime_last_played": 1704067200,  # Recent timestamp
                    },
                    {
                        "appid": 570,
                        "name": "Dota 2",
                        "playtime_forever": 0,  # Never played
                        "playtime_2weeks": 0,  # No recent activity
                    },
                    {
                        "appid": 440,
                        "name": "Team Fortress 2",
                        "playtime_forever": 500,
                        "playtime_2weeks": 0,  # No recent activity
                        "rtime_last_played": 1672531200,  # Old timestamp (over 14 days)
                    },
                ],
            },
        }

        imported_counts, _ = steam.importer(
            "76561198000000000",
            self.user,
            "new",
        )

        self.assertEqual(imported_counts[MediaTypes.GAME.value], 3)

        games = Game.objects.filter(user=self.user)
        self.assertEqual(games.count(), 3)

        cs2_game = games.get(item__media_id="730")
        self.assertEqual(cs2_game.item.source, Sources.STEAM.value)
        self.assertEqual(cs2_game.status, Status.IN_PROGRESS.value)
        self.assertEqual(cs2_game.progress, 1250)
        self.assertEqual(cs2_game.notes, "Imported from Steam")

        dota_game = games.get(item__media_id="570")
        self.assertEqual(dota_game.status, Status.PLANNING.value)
        self.assertEqual(dota_game.progress, 0)

        tf2_game = games.get(item__media_id="440")
        self.assertEqual(tf2_game.status, Status.PAUSED.value)
        self.assertEqual(tf2_game.progress, 500)

    @patch("integrations.imports.steam.services.api_request")
    def test_import_steam_private_profile(self, mock_api_request):
        """Test handling of private Steam profile."""
        response = Response()
        response.status_code = 403
        mock_api_request.side_effect = HTTPError(response=response)

        with self.assertRaises(helpers.MediaImportError) as context:
            steam.importer("76561198000000000", self.user, "new")

        self.assertIn("private or invalid", str(context.exception))

    @patch("integrations.imports.steam.services.api_request")
    def test_import_steam_cold_appid_still_created(self, mock_api_request):
        """Test that a game unknown to the metadata providers is still imported."""
        mock_api_request.return_value = {
            "response": {
                "games": [
                    {
                        "appid": 999999999,
                        "name": "Some Obscure Indie Game",
                        "playtime_forever": 100,
                        "playtime_2weeks": 0,
                    },
                ],
            },
        }

        imported_counts, warnings = steam.importer(
            "76561198000000000",
            self.user,
            "new",
        )

        self.assertEqual(imported_counts.get(MediaTypes.GAME.value), 1)
        self.assertEqual(warnings, "")

        game = Game.objects.get(user=self.user)
        self.assertEqual(game.item.media_id, "999999999")
        self.assertEqual(game.item.title, "Some Obscure Indie Game")
        self.assertEqual(
            game.item.image,
            "https://media.steampowered.com/steam/apps/"
            "999999999/library_600x900.jpg",
        )

    @patch("integrations.imports.steam.services.api_request")
    def test_import_steam_empty_name(self, mock_api_request):
        """Test a game without a name gets a readable placeholder title."""
        mock_api_request.return_value = {
            "response": {"games": [{"appid": 42, "playtime_forever": 0}]},
        }

        steam.importer("76561198000000000", self.user, "new")

        self.assertEqual(
            Game.objects.get(user=self.user).item.title,
            "Unknown Game 42",
        )

    @patch("integrations.imports.steam.services.api_request")
    def test_import_steam_no_games(self, mock_api_request):
        """Test a private library without games imports nothing."""
        mock_api_request.return_value = {"response": {}}

        imported_counts, warnings = steam.importer(
            "76561198000000000",
            self.user,
            "new",
        )

        self.assertEqual(imported_counts, {})
        self.assertEqual(warnings, "")

    def test_steam_import_does_not_use_igdb(self):
        """Test the Steam import module has no IGDB dependency left."""
        self.assertFalse(hasattr(steam, "external_game"))
        self.assertFalse(hasattr(steam, "_match_with_igdb"))

    def test_determine_game_status_logic(self):
        """Test the status determination logic."""
        importer_instance = steam.SteamImporter("76561198000000000", self.user, "new")

        status = importer_instance._determine_game_status(0, 0)
        self.assertEqual(status, Status.PLANNING.value)

        status = importer_instance._determine_game_status(100, 50)
        self.assertEqual(status, Status.IN_PROGRESS.value)

        status = importer_instance._determine_game_status(100, 0)
        self.assertEqual(status, Status.PAUSED.value)

    def test_cover_url_helpers(self):
        """Test the cover URLs are built from the app ID alone."""
        self.assertEqual(
            steam.primary_cover_url(730),
            "https://media.steampowered.com/steam/apps/730/header.jpg",
        )
        self.assertEqual(
            steam.cover_url(730, "header.jpg"),
            "https://media.steampowered.com/steam/apps/730/header.jpg",
        )
        self.assertEqual(
            steam.cover_url(
                730,
                "header.jpg",
                "https://cdn.cloudflare.steamstatic.com/steam/apps",
            ),
            "https://cdn.cloudflare.steamstatic.com/steam/apps/730/header.jpg",
        )

    @patch("integrations.imports.steam._check_cover")
    def test_resolve_cover_keeps_the_portrait_capsule(self, mock_check):
        """Test the portrait capsule wins when the CDN serves it."""
        mock_check.return_value = True

        self.assertEqual(
            steam.resolve_cover_url(999),
            "https://media.steampowered.com/steam/apps/999/library_600x900.jpg",
        )

    @patch("integrations.imports.steam._check_cover")
    def test_resolve_cover_falls_back_to_wide_cover(self, mock_check):
        """Test a game without a library capsule falls back to the header."""
        # The portrait capsule misses on both hosts, the first header answers
        mock_check.side_effect = [False, False, True]

        self.assertEqual(
            steam.resolve_cover_url(999),
            "https://media.steampowered.com/steam/apps/999/header.jpg",
        )

    @patch("integrations.imports.steam._check_cover")
    def test_resolve_cover_falls_back_to_small_capsule(self, mock_check):
        """Test a game without a header falls back to the small capsule."""
        mock_check.side_effect = [False, False, False, False, True]

        self.assertEqual(
            steam.resolve_cover_url(999),
            "https://media.steampowered.com/steam/apps/999/capsule_616x353.jpg",
        )

    @patch("integrations.imports.steam._check_cover")
    def test_resolve_cover_keeps_the_cover_on_a_network_error(self, mock_check):
        """Test a network error never downgrades a cover."""
        mock_check.return_value = None

        self.assertEqual(
            steam.resolve_cover_url(999),
            steam.primary_cover_url(999),
        )

    @patch("integrations.imports.steam._check_cover")
    def test_resolve_cover_uses_placeholder_when_missing(self, mock_check):
        """Test a delisted game falls back to the placeholder image."""
        mock_check.return_value = False

        self.assertEqual(steam.resolve_cover_url(999), settings.IMG_NONE)

    @patch("integrations.imports.steam.services.api_request")
    @patch("integrations.imports.steam._check_cover")
    def test_import_steam_verifies_covers(
        self,
        mock_check,
        mock_api_request,
    ):
        """Test the imported covers are checked against the CDN."""
        mock_api_request.return_value = {
            "response": {
                "games": [{"appid": 999, "name": "Delisted", "playtime_forever": 10}],
            },
        }
        mock_check.return_value = False

        steam.importer("76561198000000000", self.user, "new")

        self.assertEqual(
            Game.objects.get(user=self.user).item.image,
            settings.IMG_NONE,
        )

    @patch("integrations.imports.steam.services.api_request")
    def test_import_steam_can_skip_cover_checks(self, mock_api_request):
        """Test the cover check can be turned off for a fast import."""
        mock_api_request.return_value = {
            "response": {
                "games": [{"appid": 999, "name": "Delisted", "playtime_forever": 10}],
            },
        }

        with patch("integrations.imports.steam._check_cover") as mock_check:
            steam.importer("76561198000000000", self.user, "new", verify_covers=False)

        mock_check.assert_not_called()

    @patch("integrations.imports.steam.services.api_request")
    def test_import_steam_no_api_key(self, _mock_api_request):
        """Test handling when Steam API key is not configured."""
        with patch.object(settings, "STEAM_API_KEY", ""):
            with self.assertRaises(helpers.MediaImportError) as context:
                steam.importer("76561198000000000", self.user, "new")

            self.assertIn("Steam API key not configured", str(context.exception))


class GetOwnedGamesRetry(TestCase):
    """Test how the owned games call retries a broken connection.

    A network failure is the reason the scheduled Steam import used to be lost
    for the whole day: it was raised on the first attempt instead of being
    retried like the 429 rate limit already was.
    """

    def setUp(self):
        """Build an importer without reading the database of the user's media."""
        self.user = get_user_model().objects.create_user(
            username="test",
            password="12345",
        )
        self.importer = steam.SteamImporter("76561198000000000", self.user, "new")

        # The real backoff would hold the suite for 15 + 30 seconds
        sleep_patch = patch("integrations.imports.steam.time.sleep")
        self.mock_sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    @patch("integrations.imports.steam.services.api_request")
    def test_connection_error_is_retried_then_raised(self, mock_api_request):
        """Test a connection error that never clears is retried and then raised."""
        mock_api_request.side_effect = RequestsConnectionError("connection reset")

        with self.assertRaises(helpers.MediaImportUnexpectedError) as context:
            self.importer._get_owned_games()

        self.assertIn("Could not reach the Steam API", str(context.exception))
        # Three attempts in total: the first, then two retries
        self.assertEqual(mock_api_request.call_count, 3)
        self.assertEqual(self.mock_sleep.call_count, 2)

    @patch("integrations.imports.steam.services.api_request")
    def test_connection_error_is_retried_then_succeeds(self, mock_api_request):
        """Test a single dropped connection does not lose the whole import."""
        mock_api_request.side_effect = [
            RequestsConnectionError("connection reset"),
            {
                "response": {
                    "games": [{"appid": 730, "name": "Counter-Strike 2"}],
                },
            },
        ]

        games = self.importer._get_owned_games()

        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["appid"], 730)
        self.assertEqual(mock_api_request.call_count, 2)
        self.assertEqual(self.mock_sleep.call_count, 1)

    @patch("integrations.imports.steam.services.api_request")
    def test_timeout_is_retried_then_succeeds(self, mock_api_request):
        """Test a connect timeout is retried like any other network failure."""
        mock_api_request.side_effect = [
            requests.exceptions.ConnectTimeout("connect timeout"),
            {"response": {"games": [{"appid": 570, "name": "Dota 2"}]}},
        ]

        games = self.importer._get_owned_games()

        self.assertEqual(len(games), 1)
        self.assertEqual(mock_api_request.call_count, 2)

    def _rate_limited(self):
        response = Response()
        response.status_code = requests.codes.too_many_requests
        return HTTPError(response=response)

    @patch("integrations.imports.steam.services.api_request")
    def test_rate_limit_still_retries_and_keeps_its_message(self, mock_api_request):
        """Test the 429 path keeps retrying and keeps its own error message."""
        mock_api_request.side_effect = [self._rate_limited() for _ in range(3)]

        with self.assertRaises(helpers.MediaImportError) as context:
            self.importer._get_owned_games()

        self.assertEqual(mock_api_request.call_count, 3)
        self.assertEqual(self.mock_sleep.call_count, 2)
        self.assertIn("rate limit exceeded", str(context.exception))

    @patch("integrations.imports.steam.services.api_request")
    def test_rate_limit_still_recovers_on_the_next_attempt(self, mock_api_request):
        """Test a 429 that clears still returns the games."""
        mock_api_request.side_effect = [
            self._rate_limited(),
            {"response": {"games": [{"appid": 440, "name": "Team Fortress 2"}]}},
        ]

        games = self.importer._get_owned_games()

        self.assertEqual(len(games), 1)
        self.assertEqual(mock_api_request.call_count, 2)

    @patch("integrations.imports.steam.services.api_request")
    def test_forbidden_is_not_retried(self, mock_api_request):
        """Test a real answer like 403 is still raised at once, with no retry."""
        response = Response()
        response.status_code = requests.codes.forbidden
        mock_api_request.side_effect = HTTPError(response=response)

        with self.assertRaises(helpers.MediaImportError):
            self.importer._get_owned_games()

        self.assertEqual(mock_api_request.call_count, 1)
        self.mock_sleep.assert_not_called()
