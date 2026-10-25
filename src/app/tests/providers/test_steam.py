from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from app.models import Game, Item, MediaTypes, Sources, Status
from app.providers import services, steam
from integrations.imports import steam_achievements

STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"


def _response(payload):
    """Return a fake requests response carrying ``payload``."""
    response = MagicMock()
    response.json.return_value = payload
    response.status_code = requests.codes.ok
    return response


def _http_error(status_code):
    """Return an HTTPError carrying a response with ``status_code``."""
    response = MagicMock()
    response.status_code = status_code
    return requests.exceptions.HTTPError(response=response)


class SteamSearchTest(TestCase):
    """Test the keyless Steam store search."""

    def setUp(self):
        """Clear the search cache between tests."""
        cache.clear()

    @patch("app.providers.steam._cover_exists", return_value=True)
    @patch("app.providers.services.session.get")
    def test_search_returns_apps_only(self, mock_get, _mock_cover):
        """Only the games of the response are kept, bundles are dropped."""
        mock_get.return_value = _response(
            {
                "total": 3,
                "items": [
                    {
                        "type": "app",
                        "name": "Portal 2",
                        "id": 620,
                        "tiny_image": "http://example.com/tiny.jpg",
                    },
                    {"type": "bundle", "name": "Portal Bundle", "id": 900},
                    {"type": "app", "name": "传送门 2", "id": 621},
                ],
            },
        )

        data = steam.search("portal", 1)

        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["total_results"], 2)
        self.assertEqual(data["results"][0]["media_id"], "620")
        self.assertEqual(data["results"][1]["title"], "传送门 2")
        self.assertEqual(data["results"][0]["source"], Sources.STEAM.value)
        self.assertEqual(data["results"][0]["media_type"], MediaTypes.GAME.value)
        self.assertTrue(
            data["results"][0]["image"].endswith("/620/library_600x900.jpg"),
        )
        self.assertTrue(data["results"][0]["image"].startswith("https://"))

    @patch("app.providers.steam._cover_exists", return_value=True)
    @patch("app.providers.services.session.get")
    def test_search_uses_chinese_locale(self, mock_get, _mock_cover):
        """The store is asked in Chinese, for the Chinese game names."""
        mock_get.return_value = _response({"total": 0, "items": []})

        steam.search("portal", 1)

        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["url"], STORE_SEARCH_URL)
        self.assertEqual(kwargs["params"]["term"], "portal")
        self.assertEqual(kwargs["params"]["l"], "schinese")
        self.assertEqual(kwargs["params"]["cc"], "cn")

    @patch("app.providers.steam._cover_exists", return_value=True)
    @patch("app.providers.services.session.get")
    def test_search_empty(self, mock_get, _mock_cover):
        """An empty store answer gives an empty result list."""
        mock_get.return_value = _response({"total": 0, "items": []})

        data = steam.search("asdkjhasd", 1)

        self.assertEqual(data["results"], [])
        self.assertEqual(data["total_results"], 0)

    @patch("app.providers.steam._cover_exists", return_value=True)
    @patch("app.providers.services.session.get")
    def test_search_http_error(self, mock_get, _mock_cover):
        """A failing store search surfaces as a provider error."""
        mock_get.side_effect = _http_error(requests.codes.bad_gateway)

        with self.assertRaises(services.ProviderAPIError):
            steam.search("portal", 1)

    def test_cover_falls_back_to_header(self):
        """Games without a portrait capsule get the wide header instead."""
        with patch("app.providers.steam._cover_exists", return_value=False):
            image = steam.resolve_cover(1234)

        self.assertTrue(image.endswith("/1234/header.jpg"))

    def test_cover_prefers_library_capsule(self):
        """The portrait capsule wins when it exists."""
        with patch("app.providers.steam._cover_exists", return_value=True):
            image = steam.resolve_cover(1234)

        self.assertTrue(image.endswith("/1234/library_600x900.jpg"))


class SteamAchievementsTest(TestCase):
    """Test the achievement sync of the Steam games."""

    def setUp(self):
        """Create a user with two Steam games."""
        self.credentials = {"username": "achiever", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        for appid, title in (("620", "Portal 2"), ("400", "Portal")):
            item = Item.objects.create(
                media_id=appid,
                source=Sources.STEAM.value,
                media_type=MediaTypes.GAME.value,
                title=title,
                image="http://example.com/cover.jpg",
            )
            Game.objects.create(
                item=item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
            )

    @patch("app.providers.services.session.get")
    def test_sync_counts_unlocked(self, mock_get):
        """Unlocked achievements are counted and written back."""
        mock_get.return_value = _response(
            {
                "playerstats": {
                    "success": True,
                    "achievements": [
                        {"apiname": "A", "achieved": 1},
                        {"apiname": "B", "achieved": 0},
                        {"apiname": "C", "achieved": 1},
                        {"apiname": "D", "achieved": 1},
                    ],
                },
            },
        )

        result = steam_achievements.sync_achievements(
            self.user,
            "76561190000000000",
            delay=0,
        )

        self.assertEqual(result["synced"], 2)
        game = Game.objects.get(item__media_id="620")
        self.assertEqual(game.achievements_total, 4)
        self.assertEqual(game.achievements_unlocked, 3)
        self.assertEqual(game.achievement_percent, 75)
        self.assertIsNotNone(game.achievements_synced_at)

    @patch("app.providers.services.session.get")
    def test_sync_skips_games_without_achievements(self, mock_get):
        """Games without achievements leave the stored counters untouched."""
        mock_get.return_value = _response({"playerstats": {"success": False}})

        result = steam_achievements.sync_achievements(
            self.user,
            "76561190000000000",
            delay=0,
        )

        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["skipped"], 2)
        game = Game.objects.get(item__media_id="620")
        self.assertEqual(game.achievements_total, 0)
        self.assertIsNone(game.achievement_percent)

    @patch("app.providers.services.session.get")
    def test_sync_single_appid(self, mock_get):
        """Only the requested games are asked for, for incremental syncs."""
        mock_get.return_value = _response(
            {
                "playerstats": {
                    "success": True,
                    "achievements": [{"apiname": "A", "achieved": 1}],
                },
            },
        )

        result = steam_achievements.sync_achievements(
            self.user,
            "76561190000000000",
            appids=["620"],
            delay=0,
        )

        self.assertEqual(result["synced"], 1)
        self.assertEqual(mock_get.call_count, 1)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["appid"], "620")
        self.assertEqual(
            Game.objects.get(item__media_id="400").achievements_total,
            0,
        )

    @patch("integrations.imports.steam_achievements.time.sleep")
    @patch("app.providers.services.session.get")
    def test_sync_retries_on_rate_limit(self, mock_get, mock_sleep):
        """A 429 is retried instead of dropping the game."""
        ok = _response(
            {
                "playerstats": {
                    "success": True,
                    "achievements": [{"apiname": "A", "achieved": 1}],
                },
            },
        )
        mock_get.side_effect = [
            _http_error(requests.codes.too_many_requests),
            ok,
        ]

        result = steam_achievements.sync_achievements(
            self.user,
            "76561190000000000",
            appids=["620"],
            delay=0,
        )

        self.assertEqual(result["synced"], 1)
        self.assertEqual(mock_get.call_count, 2)
        self.assertTrue(mock_sleep.called)
        self.assertEqual(
            Game.objects.get(item__media_id="620").achievements_unlocked,
            1,
        )

    @patch("app.providers.services.session.get")
    def test_sync_without_api_key(self, mock_get):
        """Without an API key nothing is requested at all."""
        with patch.object(steam_achievements.settings, "STEAM_API_KEY", ""):
            result = steam_achievements.sync_achievements(
                self.user,
                "76561190000000000",
            )

        self.assertEqual(result["synced"], 0)
        mock_get.assert_not_called()


class SteamFamilySharingTest(TestCase):
    """Test the family sharing helpers of the Steam provider."""

    @patch("app.providers.services.session.get")
    def test_get_family_group_id(self, mock_get):
        """The family group of the user is returned."""
        mock_get.return_value = _response(
            {
                "response": {
                    "family_groupid": "123456",
                    "is_not_member_of_any_group": False,
                },
            },
        )

        group_id = steam.get_family_group_id("token", "76561190000000000")

        self.assertEqual(group_id, "123456")
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["access_token"], "token")
        self.assertEqual(kwargs["params"]["steamid"], "76561190000000000")

    @patch("app.providers.services.session.get")
    def test_get_family_group_id_not_member(self, mock_get):
        """A user without a family group gets None."""
        mock_get.return_value = _response(
            {"response": {"is_not_member_of_any_group": True}},
        )

        self.assertIsNone(steam.get_family_group_id("token", "76561190000000000"))

    @patch("app.providers.services.session.get")
    def test_get_family_group_id_invalid_token(self, mock_get):
        """An expired token is an empty result, not an exception."""
        mock_get.side_effect = _http_error(requests.codes.unauthorized)

        self.assertIsNone(steam.get_family_group_id("bad", "76561190000000000"))

    @patch("app.providers.services.session.get")
    def test_get_shared_library(self, mock_get):
        """The shared apps of the family group are returned."""
        apps = [
            {
                "appid": 620,
                "name": "Portal 2",
                "rt_playtime": 120,
                "rt_last_played": 1700000000,
                "exclude_reason": 0,
            },
        ]
        mock_get.return_value = _response({"response": {"apps": apps}})

        shared = steam.get_shared_library("token", "123456")

        self.assertEqual(shared, apps)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["family_groupid"], "123456")
        self.assertEqual(kwargs["params"]["include_own"], 0)

    @patch("app.providers.services.session.get")
    def test_get_shared_library_forbidden(self, mock_get):
        """A rejected token gives an empty library instead of crashing."""
        mock_get.side_effect = _http_error(requests.codes.forbidden)

        self.assertEqual(steam.get_shared_library("bad", "123456"), [])


class SteamFamilyImportTest(TestCase):
    """Test that family shared games reach the importer."""

    def setUp(self):
        """Create a user for the import."""
        self.credentials = {"username": "shared", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

    @patch("app.providers.services.session.get")
    def test_shared_games_are_flagged(self, mock_get):
        """Shared games are added with the family marker in the notes."""
        from integrations.imports import steam as steam_importer  # noqa: PLC0415

        with patch.object(
            steam_importer,
            "settings",
        ) as mock_settings:
            mock_settings.STEAM_API_KEY = "key"
            mock_settings.STEAM_ACCESS_TOKEN = "token"  # noqa: S105
            mock_settings.IMG_NONE = "http://example.com/none.jpg"

            mock_get.side_effect = [
                _response({"response": {"family_groupid": "123456"}}),
                _response(
                    {
                        "response": {
                            "apps": [
                                {
                                    "appid": 620,
                                    "name": "Portal 2",
                                    "rt_playtime": 60,
                                    "rt_last_played": 0,
                                    "exclude_reason": 0,
                                },
                                {
                                    "appid": 400,
                                    "name": "Excluded",
                                    "rt_playtime": 10,
                                    "rt_last_played": 0,
                                    "exclude_reason": 1,
                                },
                            ],
                        },
                    },
                ),
            ]

            importer = steam_importer.SteamImporter(
                "76561190000000000",
                self.user,
                "new",
            )
            shared = importer._get_family_shared_games()

        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["appid"], 620)
        self.assertEqual(shared[0]["playtime_forever"], 60)
        self.assertTrue(shared[0]["family_shared"])


class SteamSearchRoutingTest(TestCase):
    """Test that the search and details pages reach the Steam provider."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "searcher", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    @patch("app.providers.steam._cover_exists", return_value=True)
    @patch("app.providers.services.session.get")
    def test_services_search_routes_to_steam(self, mock_get, _mock_cover):
        """Searching games on Steam doesn't ask IGDB."""
        cache.clear()
        mock_get.return_value = _response(
            {
                "total": 1,
                "items": [{"type": "app", "name": "传送门 2", "id": 620}],
            },
        )

        data = services.search(
            MediaTypes.GAME.value,
            "portal",
            1,
            Sources.STEAM.value,
        )

        self.assertEqual(data["results"][0]["title"], "传送门 2")
        self.assertEqual(mock_get.call_args[1]["url"], STORE_SEARCH_URL)

    @patch("app.providers.steam._cover_exists", return_value=True)
    @patch("app.providers.services.session.get")
    def test_search_view_renders_chinese_title(self, mock_get, _mock_cover):
        """The search page shows the Chinese names of the Steam store."""
        cache.clear()
        mock_get.return_value = _response(
            {
                "total": 1,
                "items": [{"type": "app", "name": "传送门 2", "id": 620}],
            },
        )

        response = self.client.get(
            reverse("search"),
            {
                "q": "portal",
                "media_type": MediaTypes.GAME.value,
                "source": Sources.STEAM.value,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "传送门 2")

    def test_details_view_shows_achievements(self):
        """The game details page renders the achievement progress."""
        item = Item.objects.create(
            media_id="620",
            source=Sources.STEAM.value,
            media_type=MediaTypes.GAME.value,
            title="传送门 2",
            image="http://example.com/cover.jpg",
        )
        game = Game.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            achievements_total=30,
            achievements_unlocked=12,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.STEAM.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "620",
                    "title": "portal-2",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "12/30")
        self.assertContains(response, "(40%)")
        self.assertEqual(game.achievement_percent, 40)

    def test_default_game_source_is_steam(self):
        """The game search defaults to Steam now."""
        from app import config  # noqa: PLC0415

        self.assertEqual(
            config.get_default_source_name(MediaTypes.GAME.value),
            Sources.STEAM,
        )
        self.assertIn(
            Sources.STEAM,
            config.get_sources(MediaTypes.GAME.value),
        )
