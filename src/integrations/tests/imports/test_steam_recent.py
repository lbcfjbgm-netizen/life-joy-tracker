from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app.models import Game, Item, MediaTypes, Sources, Status
from integrations.imports import steam, steam_recent

STEAM_ID = "76561190000000000"


def _played(appid, name, playtime_forever, playtime_2weeks):
    """Return one entry shaped like the recently played API of Steam."""
    return {
        "appid": appid,
        "name": name,
        "playtime_forever": playtime_forever,
        "playtime_2weeks": playtime_2weeks,
        "rtime_last_played": 1704067200,
    }


class RecentSteamSyncTest(TestCase):
    """Test the daily sync of the games played during the last two weeks.

    The point of these tests is the red line of the operator: a game that was
    rated, annotated or dropped by hand must come out of a sync untouched, while
    a blank one is filled in.
    """

    def setUp(self):
        """Create a user with one curated game per rule and one blank game.

        Covers are never checked: that would need the Steam CDN.
        """
        self.user = get_user_model().objects.create_user(
            username="recent",
            password="12345",  # noqa: S106
        )

        cover_check = patch("integrations.imports.steam._check_cover")
        self.mock_cover_check = cover_check.start()
        self.mock_cover_check.return_value = True
        self.addCleanup(cover_check.stop)

        self.scored = self._game(
            "100",
            "Rated Game",
            score="8.0",
            status=Status.COMPLETED.value,
            notes="Imported from Steam",
            progress=600,
            start_date="2025-01-01T00:00:00Z",
            end_date="2025-02-01T00:00:00Z",
        )
        self.noted = self._game(
            "200",
            "Noted Game",
            notes="打了一半卡在第三关",
            status=Status.IN_PROGRESS.value,
            progress=300,
        )
        self.dropped = self._game(
            "300",
            "Dropped Game",
            status=Status.DROPPED.value,
            notes="Imported from Steam",
            progress=120,
        )
        self.paused = self._game(
            "400",
            "Paused Game",
            status=Status.PAUSED.value,
            notes="Imported from Steam",
            progress=90,
        )
        self.blank = self._game(
            "500",
            "Blank Game",
            status=Status.IN_PROGRESS.value,
            notes="",
            progress=60,
        )

    def _game(self, appid, title, **fields):
        """Create a Steam game of the user with the given fields."""
        item = Item.objects.create(
            media_id=appid,
            source=Sources.STEAM.value,
            media_type=MediaTypes.GAME.value,
            title=title,
            image="http://example.com/cover.jpg",
        )
        return Game.objects.create(item=item, user=self.user, **fields)

    def _sync(self, played):
        """Run the recent sync of a fixed list of played games."""
        with (
            patch(
                "integrations.imports.steam.fetch_recently_played_games",
                return_value=played,
            ),
            patch(
                "integrations.imports.steam_achievements.sync_achievements",
                return_value={"synced": 0, "skipped": 0},
            ),
        ):
            return steam_recent.sync_recent_games(self.user, STEAM_ID)

    def test_rated_game_keeps_its_score(self):
        """A rated game keeps its rating and status, only its playtime moves."""
        self._sync([_played(100, "Rated Game", 900, 300)])
        game = Game.objects.get(pk=self.scored.pk)

        self.assertEqual(game.score, 8.0)
        self.assertEqual(game.status, Status.COMPLETED.value)
        self.assertEqual(game.progress, 900)

    def test_noted_game_keeps_its_note(self):
        """A game with a note of the operator keeps it."""
        self._sync([_played(200, "Noted Game", 450, 150)])
        game = Game.objects.get(pk=self.noted.pk)

        self.assertEqual(game.notes, "打了一半卡在第三关")
        self.assertEqual(game.progress, 450)

    def test_dropped_game_keeps_its_status(self):
        """A dropped game stays dropped even after being played again."""
        self._sync([_played(300, "Dropped Game", 300, 180)])
        game = Game.objects.get(pk=self.dropped.pk)

        self.assertEqual(game.status, Status.DROPPED.value)
        self.assertEqual(game.progress, 300)

    def test_paused_game_keeps_its_status(self):
        """A paused game stays paused, it is not pushed to in progress."""
        self._sync([_played(400, "Paused Game", 200, 110)])
        game = Game.objects.get(pk=self.paused.pk)

        self.assertEqual(game.status, Status.PAUSED.value)
        self.assertEqual(game.progress, 200)

    def test_blank_game_is_filled_in(self):
        """A game nobody edited gets its playtime and status moved forward."""
        self._sync([_played(500, "Blank Game", 180, 120)])
        game = Game.objects.get(pk=self.blank.pk)

        self.assertEqual(game.progress, 180)
        self.assertEqual(game.status, Status.IN_PROGRESS.value)
        # The sync fills what the operator left empty, it never writes on top
        # of an existing value
        self.assertEqual(game.score, None)

    def test_playtime_never_goes_down(self):
        """A game already tracked further keeps its higher playtime."""
        self._sync([_played(500, "Blank Game", 10, 5)])
        game = Game.objects.get(pk=self.blank.pk)

        self.assertEqual(game.progress, 60)

    def test_new_game_is_imported(self):
        """A game that was never tracked is created with its playtime."""
        counts = self._sync(
            [
                _played(600, "Brand New Game", 240, 240),
                _played(1000, "Another New Game", 30, 30),
            ],
        )

        self.assertEqual(counts["created"], 2)
        game = Game.objects.get(user=self.user, item__media_id="600")
        self.assertEqual(game.item.title, "Brand New Game")
        self.assertEqual(game.progress, 240)
        self.assertEqual(game.status, Status.IN_PROGRESS.value)
        self.assertEqual(game.notes, "Imported from Steam")

    def test_nothing_played_is_a_no_op(self):
        """An empty answer changes nothing and does not raise."""
        counts = self._sync([])

        self.assertEqual(counts["recent"], 0)
        self.assertEqual(counts["created"], 0)
        self.assertEqual(counts["updated"], 0)

    def test_achievements_only_ask_for_that_batch(self):
        """Only the games of the run are asked for, never the whole library."""
        with (
            patch(
                "integrations.imports.steam.fetch_recently_played_games",
                return_value=[
                    _played(100, "Rated Game", 900, 300),
                    _played(700, "Fresh Game", 60, 60),
                ],
            ),
            patch(
                "integrations.imports.steam_achievements.sync_achievements",
                return_value={"synced": 0, "skipped": 0},
            ) as mock_sync,
        ):
            steam_recent.sync_recent_games(self.user, STEAM_ID)

        _, kwargs = mock_sync.call_args
        self.assertEqual(sorted(kwargs["appids"]), [100, 700])


class RecentlyPlayedPayloadTest(TestCase):
    """Test the reading of the recently played answer of Steam."""

    def _payload(self, games):
        """Return a fake Steam answer holding ``games``."""

        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"response": {"games": games}}

        return _Response()

    @patch("integrations.imports.steam.steam_proxies", return_value={})
    @patch("integrations.imports.steam.requests.get")
    def test_payload_keeps_playtime(self, mock_get, _mock_proxies):
        """The two counters and the name are read from the answer."""
        mock_get.return_value = self._payload(
            [
                {
                    "appid": 620,
                    "name": "Portal 2",
                    "playtime_forever": 600,
                    "playtime_2weeks": 90,
                },
                {
                    "appid": 400,
                    "name": "Portal",
                    "playtime_forever": 120,
                    "playtime_2weeks": 0,
                },
            ],
        )

        games = steam.fetch_recently_played_games("key", STEAM_ID)

        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["appid"], 620)
        self.assertEqual(games[0]["playtime_forever"], 600)
        self.assertEqual(games[0]["playtime_2weeks"], 90)
        self.assertEqual(steam.fetch_recently_played("key", STEAM_ID), {620})

    @patch("integrations.imports.steam.steam_proxies", return_value={})
    @patch("integrations.imports.steam.requests.get")
    def test_broken_answer_is_empty(self, mock_get, _mock_proxies):
        """An unreadable answer gives an empty list instead of raising."""
        mock_get.side_effect = steam.requests.RequestException("nope")

        self.assertEqual(steam.fetch_recently_played_games("key", STEAM_ID), [])


class BackfillTest(TestCase):
    """Test the one-time achievement backfill."""

    def setUp(self):
        """Create a user with two Steam games missing their achievements."""
        self.user = get_user_model().objects.create_user(
            username="backfill",
            password="12345",  # noqa: S106
        )
        for appid in ("620", "400"):
            item = Item.objects.create(
                media_id=appid,
                source=Sources.STEAM.value,
                media_type=MediaTypes.GAME.value,
                title=f"Game {appid}",
                image="http://example.com/cover.jpg",
            )
            Game.objects.create(
                item=item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
            )

    @patch("integrations.imports.steam_recent.time.sleep")
    @patch(
        "integrations.imports.steam_achievements.sync_achievements",
        return_value={"synced": 1, "skipped": 0},
    )
    def test_backfill_walks_every_pending_game(self, mock_sync, mock_sleep):
        """Every game without achievements is asked for exactly once."""
        result = steam_recent.backfill_achievements(
            self.user,
            STEAM_ID,
            batch_size=1,
            delay=0,
        )

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["batches"], 2)
        self.assertEqual(mock_sync.call_count, 2)
        self.assertEqual(result["synced"], 2)
        self.assertTrue(mock_sleep.called)

    @patch("integrations.imports.steam_achievements.sync_achievements")
    def test_backfill_skips_synced_games(self, mock_sync):
        """A game that already has its achievements is left out."""
        Game.objects.filter(item__media_id="400").update(
            achievements_synced_at=timezone.now(),
        )

        result = steam_recent.backfill_achievements(
            self.user,
            STEAM_ID,
            batch_size=10,
            delay=0,
        )

        self.assertEqual(result["total"], 1)
        _, kwargs = mock_sync.call_args
        self.assertEqual(kwargs["appids"], ["620"])

    @patch("integrations.imports.steam_achievements.sync_achievements")
    def test_backfill_without_api_key(self, mock_sync):
        """Without an API key nothing is requested at all."""
        with patch.object(steam_recent.settings, "STEAM_API_KEY", ""):
            result = steam_recent.backfill_achievements(self.user, STEAM_ID)

        self.assertEqual(result["total"], 0)
        mock_sync.assert_not_called()


class UserCuratedTest(TestCase):
    """Test the rule that decides whether a game was edited by hand."""

    def setUp(self):
        """Create a user to hang the games on."""
        self.user = get_user_model().objects.create_user(
            username="curated",
            password="12345",  # noqa: S106
        )

    def _game(self, **fields):
        """Create a Steam game with the given fields."""
        appid = str(Game.objects.count() + 1000)
        item = Item.objects.create(
            media_id=appid,
            source=Sources.STEAM.value,
            media_type=MediaTypes.GAME.value,
            title="Game",
            image="http://example.com/cover.jpg",
        )
        return Game.objects.create(item=item, user=self.user, **fields)

    def test_score_means_curated(self):
        """A rating is a decision of the operator."""
        self.assertTrue(steam_recent._is_user_curated(self._game(score="7.0")))

    def test_own_note_means_curated(self):
        """A note written by the operator is a decision."""
        self.assertTrue(steam_recent._is_user_curated(self._game(notes="真结局没打")))

    def test_dropped_means_curated(self):
        """A dropped game was dropped on purpose."""
        self.assertTrue(
            steam_recent._is_user_curated(self._game(status=Status.DROPPED.value)),
        )

    def test_paused_means_curated(self):
        """A paused game was paused on purpose."""
        self.assertTrue(
            steam_recent._is_user_curated(self._game(status=Status.PAUSED.value)),
        )

    def test_imported_note_is_not_curated(self):
        """The note written by the import itself is not a decision."""
        self.assertFalse(
            steam_recent._is_user_curated(
                self._game(
                    notes="Imported from Steam",
                    status=Status.IN_PROGRESS.value,
                ),
            ),
        )
        self.assertFalse(
            steam_recent._is_user_curated(
                self._game(
                    notes="Imported from Steam（家庭共享）",  # noqa: RUF001
                    status=Status.IN_PROGRESS.value,
                ),
            ),
        )

    def test_blank_game_is_not_curated(self):
        """An untouched game is fair game for the sync."""
        self.assertFalse(
            steam_recent._is_user_curated(
                self._game(notes="", status=Status.IN_PROGRESS.value),
            ),
        )
