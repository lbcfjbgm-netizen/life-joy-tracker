"""Offline self test for the Steam importer.

Runs without a Steam API key, without network access and without IGDB, against a
throwaway test database, so the development database is never touched.

Run it from the repository root or from src:

    python ../scripts/test_steam_import.py       # from src
    python scripts/test_steam_import.py          # from the repository root

Every check prints PASS or FAIL, and the process exits non zero if one fails.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
# A local .env may leave the key empty; the importer only checks it exists
os.environ.setdefault("STEAM_API_KEY", "offline_self_test_key")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection  # noqa: E402
from requests import Response  # noqa: E402
from requests.exceptions import ConnectionError as RequestsConnectionError  # noqa: E402
from requests.exceptions import HTTPError  # noqa: E402

from app.models import Game, Item, MediaTypes, Sources, Status  # noqa: E402
from integrations.imports import helpers, steam  # noqa: E402

STEAM_ID = "76561198000000000"

# Three kinds of games: one played a lot, one never played and one that no
# metadata provider knows about, so it used to be dropped by the IGDB matching.
OWNED_GAMES = {
    "response": {
        "games": [
            {
                "appid": 730,
                "name": "Counter-Strike 2",
                "playtime_forever": 1250,
                "playtime_2weeks": 120,
            },
            {
                "appid": 570,
                "name": "Dota 2",
                "playtime_forever": 0,
                "playtime_2weeks": 0,
            },
            {
                "appid": 999999999,
                "name": "Obscure Indie Gem",
                "playtime_forever": 37,
                "playtime_2weeks": 0,
            },
        ],
    },
}

RESULTS = []


def check(label, condition, detail=""):
    """Record the result of a single assertion."""
    passed = bool(condition)
    RESULTS.append((label, passed))
    line = f"[{'PASS' if passed else 'FAIL'}] {label}"
    if detail and not passed:
        line += f" -> {detail}"
    print(line)


def equal(label, value, expected):
    """Record the result of an equality assertion."""
    check(label, value == expected, f"got {value!r}, expected {expected!r}")


def error_response(status_code):
    """Build an HTTPError with the given status code."""
    response = Response()
    response.status_code = status_code
    return HTTPError(response=response)


def user_named(username):
    """Create a user for one section of the test."""
    return get_user_model().objects.create_user(username=username, password="x")


def get_game(user, appid):
    """Return the tracked game of an app ID."""
    return Game.objects.get(user=user, item__media_id=str(appid))


def covered_url(appid, filename="library_600x900.jpg"):
    """Return the expected Steam CDN URL of a cover."""
    return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/{filename}"


def test_cold_appid_is_kept():
    """The whole point of the rewrite: unknown games are imported anyway."""
    user = user_named("steam_cold")

    with patch("integrations.imports.steam.services.api_request") as api_request:
        api_request.return_value = OWNED_GAMES
        # Any call to the metadata layer, like the old IGDB matching, is a failure
        with patch(
            "app.providers.services.get_media_metadata",
            side_effect=AssertionError("no metadata provider may be called"),
        ):
            counts, warnings = steam.importer(STEAM_ID, user, "new")

    equal("3 games imported, none dropped", counts.get(MediaTypes.GAME.value), 3)
    equal("no warnings", warnings, "")
    equal("3 games in the database", Game.objects.filter(user=user).count(), 3)
    equal(
        "3 Steam items in the database",
        Item.objects.filter(source=Sources.STEAM.value).count(),
        3,
    )

    cold = get_game(user, 999999999)
    equal("cold appid kept as media_id", cold.item.media_id, "999999999")
    equal("cold appid keeps the Steam title", cold.item.title, "Obscure Indie Gem")
    equal("cold appid uses the Steam source", cold.item.source, Sources.STEAM.value)
    equal("cold appid uses the game media type", cold.item.media_type, "game")
    equal(
        "cold appid cover built from the app ID",
        cold.item.image,
        covered_url(999999999),
    )


def test_playtime_and_status():
    """Playtime and status keep the behaviour of the old importer."""
    user = get_user_model().objects.get(username="steam_cold")
    played = get_game(user, 730)
    unplayed = get_game(user, 570)
    cold = get_game(user, 999999999)

    equal("playtime becomes progress", played.progress, 1250)
    equal("0 playtime becomes 0 progress", unplayed.progress, 0)
    equal("cold appid playtime kept", cold.progress, 37)

    equal("recent playtime is in progress", played.status, Status.IN_PROGRESS.value)
    equal("no playtime is planning", unplayed.status, Status.PLANNING.value)
    equal("old playtime is paused", cold.status, Status.PAUSED.value)

    equal("notes marked as imported", played.notes, "Imported from Steam")


def test_new_mode_syncs_without_duplicates():
    """A second import raises playtime, never duplicates or lowers it."""
    user = get_user_model().objects.get(username="steam_cold")
    before = Game.objects.filter(user=user).count()

    with patch("integrations.imports.steam.services.api_request") as api_request:
        api_request.return_value = OWNED_GAMES
        counts, _ = steam.importer(STEAM_ID, user, "new")

    equal("second new import creates nothing", counts, {})
    equal("no duplicate games", Game.objects.filter(user=user).count(), before)
    equal("playtime unchanged on a second import", get_game(user, 730).progress, 1250)


def test_overwrite_mode():
    """Overwrite mode replaces the tracked playtime instead of raising it."""
    user = get_user_model().objects.get(username="steam_cold")
    game = get_game(user, 730)
    game.progress = 5000
    game.save()

    payload = {
        "response": {
            "games": [
                {
                    "appid": 730,
                    "name": "Counter-Strike 2",
                    "playtime_forever": 800,
                    "playtime_2weeks": 0,
                },
            ],
        },
    }

    with patch("integrations.imports.steam.services.api_request") as api_request:
        api_request.return_value = payload
        counts, _ = steam.importer(STEAM_ID, user, "overwrite")

    game.refresh_from_db()
    equal("overwrite reports one game", counts.get(MediaTypes.GAME.value), 1)
    equal("overwrite lowers the playtime", game.progress, 800)
    equal("overwrite moves the status", game.status, Status.PAUSED.value)
    equal("no duplicate game from overwrite", Game.objects.filter(user=user).count(), 3)


def test_cover_fallback():
    """A missing library capsule falls back to the wide cover, then placeholder."""
    with patch("integrations.imports.steam._check_cover") as check_cover:
        check_cover.side_effect = [False, True]
        equal(
            "missing portrait cover falls back to header.jpg",
            steam.resolve_cover_url(123),
            covered_url(123, "header.jpg"),
        )

    with patch("integrations.imports.steam._check_cover") as check_cover:
        check_cover.return_value = False
        equal(
            "all covers missing gives the placeholder",
            steam.resolve_cover_url(123),
            settings.IMG_NONE,
        )

    with patch("integrations.imports.steam._check_cover") as check_cover:
        check_cover.return_value = None
        equal(
            "network error keeps the default cover",
            steam.resolve_cover_url(123),
            covered_url(123),
        )


def test_cover_check_on_import():
    """The optional cover check fixes the stored image of a broken cover."""
    user = user_named("steam_covers")
    payload = {
        "response": {
            "games": [{"appid": 999, "name": "Delisted", "playtime_forever": 5}],
        },
    }

    with patch("integrations.imports.steam.services.api_request") as api_request:
        api_request.return_value = payload
        with patch("integrations.imports.steam._check_cover", return_value=False):
            steam.importer(STEAM_ID, user, "new", verify_covers=True)

    equal(
        "broken cover replaced with the placeholder",
        Game.objects.get(user=user).item.image,
        settings.IMG_NONE,
    )

    user_default = user_named("steam_no_covers")
    with patch("integrations.imports.steam.services.api_request") as api_request:
        api_request.return_value = payload
        with patch("integrations.imports.steam._check_cover") as check_cover:
            steam.importer(STEAM_ID, user_default, "new")

    check("default import never checks the CDN", not check_cover.called)


def test_error_handling():
    """The Steam API error mapping and the rate limit retry are preserved."""
    user = user_named("steam_errors")

    for status_code, expected in (
        (403, "private or invalid"),
        (400, "check the Steam ID"),
        (401, "Invalid Steam API key"),
    ):
        with patch("integrations.imports.steam.services.api_request") as api_request:
            api_request.side_effect = error_response(status_code)
            try:
                steam.importer(STEAM_ID, user, "new")
            except helpers.MediaImportError as error:
                check(
                    f"HTTP {status_code} maps to a readable message",
                    expected in str(error),
                    str(error),
                )
            else:
                check(f"HTTP {status_code} raises MediaImportError", False)

    with patch("integrations.imports.steam.services.api_request") as api_request:
        api_request.side_effect = error_response(429)
        with patch("integrations.imports.steam.time.sleep") as sleep:
            try:
                steam.importer(STEAM_ID, user, "new")
            except helpers.MediaImportError as error:
                check(
                    "429 retries then gives up",
                    "rate limit exceeded" in str(error),
                    str(error),
                )
                equal("429 retried three times", api_request.call_count, 3)
                equal("429 waited between retries", sleep.call_count, 2)
            else:
                check("429 raises MediaImportError", False)

    with patch("integrations.imports.steam.services.api_request") as api_request:
        api_request.side_effect = RequestsConnectionError("offline")
        try:
            steam.importer(STEAM_ID, user, "new")
        except helpers.MediaImportUnexpectedError:
            check("network failure raises an unexpected error", True)
        except Exception as error:  # noqa: BLE001
            check("network failure raises an unexpected error", False, repr(error))
        else:
            check("network failure raises an unexpected error", False)

    with patch.object(settings, "STEAM_API_KEY", ""):
        try:
            steam.importer(STEAM_ID, user, "new")
        except helpers.MediaImportError as error:
            check(
                "missing API key rejected",
                "API key not configured" in str(error),
                str(error),
            )
        else:
            check("missing API key rejected", False)


SECTIONS = (
    test_cold_appid_is_kept,
    test_playtime_and_status,
    test_new_mode_syncs_without_duplicates,
    test_overwrite_mode,
    test_cover_fallback,
    test_cover_check_on_import,
    test_error_handling,
)


def main():
    """Run every section against a throwaway test database."""
    old_config = connection.creation.create_test_db(verbosity=0, autoclobber=True)
    try:
        for section in SECTIONS:
            print(f"\n--- {section.__name__} ---")
            section()
    finally:
        connection.creation.destroy_test_db(old_config, verbosity=0)

    passed = sum(1 for _, ok in RESULTS if ok)
    failed = len(RESULTS) - passed
    print(f"\n=== {passed}/{len(RESULTS)} checks passed, {failed} failed ===")
    if failed:
        for label, ok in RESULTS:
            if not ok:
                print(f"  FAILED: {label}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
