"""Verify the achievement_percent 500 regression fix with the Django test client.

Read-only: logs in as the local admin (pk=1) and GETs a list of detail pages,
printing status codes plus a few content assertions. Nothing is written to the
database.
"""

import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, "src")

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402

from app import achievements  # noqa: E402

URLS = [
    ("weread book 1", "/details/weread/book/3300024217/tou20shu20zei20"),
    ("weread book 2", "/details/weread/book/810506/niu20peng20za20yi20"),
    ("weread book 3", "/details/weread/book/230104/fen20shen20"),
    ("douban tv 1", "/details/douban/tv/26647087/san20ti20"),
    ("douban tv 2", "/details/douban/tv/37033221/fan20ren20lei20bao20xing20"),
    ("douban tv 3", "/details/douban/tv/2210031/xian20jian20qi20xia20chuan20"),
    ("douban movie", "/details/douban/movie/37116446/gei20a20ma20de20qing20shu20"),
    ("steam game", "/details/steam/game/2967950/the-witchs-bakery"),
    ("steam game ach", "/details/steam/game/4267800/x"),
    ("tmdb tv", "/details/tmdb/tv/999999/x"),
    ("openlibrary book", "/details/openlibrary/book/999999/x"),
]

BAD_MARKERS = ("achievement_percent", "Traceback", "Exception Value")


def main():
    user = get_user_model().objects.get(pk=1)
    client = Client()
    client.force_login(user)

    failures = []
    bodies = {}
    for label, url in URLS:
        try:
            response = client.get(url)
        except Exception as exc:  # a raise means the view is still 500ing
            print(f"{label:18s} {url} -> RAISED {type(exc).__name__}: {exc}")
            failures.append(label)
            continue
        body = response.content.decode("utf-8", "replace")
        bodies[label] = body
        bad = [m for m in BAD_MARKERS if m in body]
        print(
            f"{label:18s} {url} -> {response.status_code} "
            f"len={len(body)} bad={bad}",
        )
        if response.status_code != 200:
            failures.append(label)

    print("\n--- content checks ---")

    # Book page: the achievement card is a game-only partial, so a book detail
    # page must not render the numbers row at all.
    book_body = bodies.get("weread book 1", "")
    checks = {
        "book 无 '0 / 0 已解锁'": "0 / 0 已解锁" not in book_body,
        "book 无 NaN%": "NaN%" not in book_body,
        "book 无 已解锁 0%": "已解锁 0%" not in book_body,
        "vanilla book 页面非空": len(book_body) > 5000,
    }
    for name, ok in checks.items():
        print(f"{'OK ' if ok else 'FAIL'} {name}")
        if not ok:
            failures.append(name)

    # Steam game: the achievement card must still be there with real numbers.
    steam_body = bodies.get("steam game ach", "")
    steam_checks = {
        "steam 有 '已解锁'": "已解锁" in steam_body,
        "steam 有 '/ 59'": "/ 59" in steam_body,
        "steam 无 '无成就清单'": "无成就清单" not in steam_body,
    }
    for name, ok in steam_checks.items():
        print(f"{'OK ' if ok else 'FAIL'} {name}")
        if not ok:
            failures.append(name)

    # Direct helper checks: the function itself must never touch a Book's
    # missing attribute.
    from app.models import Game

    game = (
        Game.objects.filter(item__source="steam", item__media_id="4267800")
        .select_related("item")
        .first()
    )
    if game is not None:
        summary = achievements.achievement_summary(game)
        print(
            f"\nsteam summary: total={summary['total']} "
            f"unlocked={summary['unlocked']} percent={summary['percent']}",
        )
        if summary["percent"] is None:
            failures.append("steam percent is None")
    else:
        print("\nsteam game row not found, skipped summary check")
        failures.append("steam row missing")

    # Non-game instance: must return the clean empty shape, not raise.
    from app.models import Book

    book = Book.objects.select_related("item").first()
    if book is not None:
        summary = achievements.achievement_summary(book)
        print(
            f"book summary: total={summary['total']} "
            f"unlocked={summary['unlocked']} percent={summary['percent']}",
        )
        if summary["total"] or summary["unlocked"] or summary["percent"] is not None:
            failures.append("book summary not empty-shaped")
    else:
        print("no book row, skipped non-game summary check")

    print("\nRESULT:", "ALL PASS" if not failures else f"FAILURES: {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
