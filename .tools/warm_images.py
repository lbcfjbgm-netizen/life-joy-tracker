"""Warm the local cover cache so a page never has to wait on a cold CDN.

Every image the relay serves is cached twice: in Redis (30 days) and on disk in
``.tools/raw/covers/<sha256[:2]>/<sha256>.<ext>``. The disk copy is the durable
one, because the Redis on this machine runs without persistence.

The first visitor of a page still pays for the download. This script walks the
database ahead of time and fills that cache, using the very same
``app.image_relay.fetch_image`` the view calls, so the cache keys match exactly
(sha256 of the *upstream* URL, never of the relay URL).

Usage (run from ``yamtrack/src`` or anywhere; paths are absolute):

    python .tools/warm_images.py --dry-run
    python .tools/warm_images.py --limit 100
    python .tools/warm_images.py                # everything
    python .tools/warm_images.py --only heroes  # library_hero.jpg only
    python .tools/warm_images.py --active-only  # games in the default list view

Already cached images are skipped, so re-running resumes where it stopped.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

import os  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from app import image_relay  # noqa: E402

DB_PATH = SRC_DIR / "db" / "db.sqlite3"
# Read from settings rather than a hardcoded string: the placeholder used to be
# a themoviedb.org SVG and is now an inline data URI, so a literal would silently
# stop matching after that change and every game without a cover would be
# downloaded as if it were a real one.
IMG_NONE = settings.IMG_NONE

# "Normally displayed" == what the default game list view shows: media_type is
# game and the status is anything but Dropped. The books/films/series/episodes
# rows in app_item never appear on that page, so they are excluded on purpose.
ACTIVE_ONLY_SQL = (
    "SELECT DISTINCT i.image "
    "FROM app_item i "
    "JOIN app_game g ON g.item_id = i.id "
    "WHERE i.media_type = 'game' "
    "AND i.image IS NOT NULL AND i.image <> '' "
    "AND g.status <> 'Dropped'"
)
ALL_SQL = "SELECT DISTINCT image FROM app_item WHERE image IS NOT NULL AND image <> ''"


def cover_urls(database, active_only=False):
    """Yield every distinct cover URL the site can render from the database.

    With ``active_only`` only the covers of games in the default list view are
    returned; without it every row of ``app_item`` is included, books and films
    too.
    """
    sql = ACTIVE_ONLY_SQL if active_only else ALL_SQL
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        # Another process may restart the site while we read; SQLite then says
        # "database is locked" for a moment, so wait and try again.
        rows = []
        for attempt in range(5):
            try:
                rows = list(connection.execute(sql))
                break
            except sqlite3.OperationalError as error:
                if "locked" not in str(error) or attempt == 4:
                    raise
                print(f"[warm] database locked, retry {attempt + 1}/5", flush=True)
                time.sleep(5)
        seen = set()
        for (image,) in rows:
            if image in seen or image == IMG_NONE or image.startswith("data:"):
                continue
            seen.add(image)
            yield image
    finally:
        connection.close()


def hero_urls(database):
    """Yield every Steam ``library_hero.jpg`` the giant-screen mode needs."""
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        seen = set()
        rows = connection.execute(
            "SELECT DISTINCT media_id FROM app_game "
            "JOIN app_item ON app_item.id = app_game.item_id",
        )
        for (media_id,) in rows:
            url = image_relay.steam_hero_url(media_id)
            if not url or url in seen:
                continue
            seen.add(url)
            yield url
    finally:
        connection.close()


def already_cached(url):
    """Return True when the bytes are already on disk for this URL."""
    digest = image_relay.hashlib.sha256(url.encode()).hexdigest()
    return image_relay._read_from_disk(digest) is not None  # noqa: SLF001


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="stop after N images (0 = all)")
    parser.add_argument("--threads", type=int, default=6, help="parallel downloads")
    parser.add_argument(
        "--only",
        choices=("all", "covers", "heroes"),
        default="all",
        help="which image family to warm",
    )
    parser.add_argument("--dry-run", action="store_true", help="only report the counts")
    parser.add_argument(
        "--active-only",
        action="store_true",
        help="only covers of games shown in the default list view (not Dropped)",
    )
    args = parser.parse_args()

    targets = []
    if args.only in ("all", "covers"):
        targets.extend(
            ("cover", url)
            for url in cover_urls(DB_PATH, active_only=args.active_only)
        )
    # Giant-screen heroes are a different family; --active-only is about the
    # list-page covers only, so they are left out of that mode.
    if args.only == "heroes" or (args.only == "all" and not args.active_only):
        targets.extend(("hero", url) for url in hero_urls(DB_PATH))

    # Only hosts the relay is willing to fetch; everything else is left alone.
    before_filter = len(targets)
    targets = [(kind, url) for kind, url in targets if image_relay.should_cache(url)]
    print(
        f"[warm] mode={'active-only' if args.active_only else 'all'} "
        f"urls={before_filter} not_cacheable={before_filter - len(targets)}",
        flush=True,
    )

    cached = 0
    pending = []
    for kind, url in targets:
        if already_cached(url):
            cached += 1
        else:
            pending.append((kind, url))

    print(
        f"[warm] targets={len(targets)} already_cached={cached} to_fetch={len(pending)}",
        flush=True,
    )
    if args.dry_run or not pending:
        return 0

    if args.limit:
        pending = pending[: args.limit]

    started = time.time()
    done = 0
    failed = 0
    bytes_total = 0
    failures = []
    ok_by_kind = {"cover": 0, "hero": 0}
    lock = __import__("threading").Lock()

    def warm(item):
        kind, url = item
        content, _content_type = image_relay.fetch_image(url)
        if content is None:
            # One retry after a short pause: most of these are a dropped
            # connection or a 429, not a cover that is really missing.
            time.sleep(0.5)
            content, _content_type = image_relay.fetch_image(url)
        return kind, url, content

    with ThreadPoolExecutor(max_workers=max(1, args.threads)) as pool:
        futures = [pool.submit(warm, item) for item in pending]
        for future in as_completed(futures):
            kind, url, content = future.result()
            with lock:
                done += 1
                if content is None:
                    failed += 1
                    if len(failures) < 20:
                        failures.append(url)
                else:
                    ok_by_kind[kind] = ok_by_kind.get(kind, 0) + 1
                    bytes_total += len(content)
                if done % 25 == 0 or done == len(pending):
                    elapsed = time.time() - started
                    rate = done / elapsed if elapsed else 0
                    print(
                        f"[warm] {done}/{len(pending)} "
                        f"ok={done - failed} fail={failed} "
                        f"{bytes_total / 1_048_576:.1f} MiB "
                        f"{rate:.1f} img/s {elapsed:.0f}s "
                        f"last={kind}:{url.rsplit('/', 1)[-2]}",
                        flush=True,
                    )

    elapsed = time.time() - started
    print(
        f"[warm] finished: ok={done - failed} fail={failed} "
        f"covers_ok={ok_by_kind.get('cover', 0)} heroes_ok={ok_by_kind.get('hero', 0)} "
        f"{bytes_total / 1_048_576:.1f} MiB in {elapsed:.0f}s "
        f"-> {image_relay.IMAGE_DIR}",
        flush=True,
    )
    for url in failures:
        print(f"[warm] failed: {url}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
