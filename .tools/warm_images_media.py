"""Warm the local cover cache for books, films and series.

The relay (``src/app/image_relay.py``) stores every image it serves twice:
in Redis (30 days) and on disk under::

    .tools/raw/covers/<sha256[:2]>/<sha256>.<ext>

where ``<sha256>`` is the sha256 of the **upstream** URL, never of the relay
URL. Redis on this machine runs without persistence, so the disk copy is the
durable one -- and it is what this script fills.

Games were already warmed by ``.tools/warm_images.py``. Books/films/series
were skipped because they are only cached when somebody visits the page, and
Douban throttles hard, so the first visit of a library page pays for every
cover. This script walks ``app_item`` ahead of time instead.

Two things make this less trivial than it looks:

* ``app_item.image`` is not uniformly an http URL. Movies and series store a
  relative ``/image_relay?url=https%3A%2F%2F...`` (the output of
  ``image_relay.localize``); books store bare URLs (WeRead's CDN). The value
  has to be unquoted back to the upstream URL first, because *that* URL is
  the cache key. Warming the relay URL would write files nobody ever reads.
* Douban refuses without a referer (418 / 403), so the fetch has to replay
  the relay's own headers. Rather than reimplement that, the script calls
  ``image_relay.fetch_image`` -- the exact function the view calls -- so the
  headers, the size limit, the digest and therefore the cache path cannot
  drift away from the relay.

Downloads are strictly serial with a random 0.8-1.2 s pause between them:
Douban throttles per IP, and a burst would get the user's address blocked,
which is worse than a slow warm.

Usage (absolute paths; run from anywhere)::

    .venv\\Scripts\\python.exe .tools\\warm_images_media.py --dry-run
    .venv\\Scripts\\python.exe .tools\\warm_images_media.py --dry-run --only book
    .venv\\Scripts\\python.exe .tools\\warm_images_media.py --apply --limit 30 --only movie
    .venv\\Scripts\\python.exe .tools\\warm_images_media.py --apply

``--dry-run`` is the default; nothing is downloaded unless ``--apply`` is
given. Everything already on disk is skipped, so re-running resumes.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------
# Paths. Resolved from this file so the script works from any cwd.
# --------------------------------------------------------------------------
TOOLS_DIR = Path(__file__).resolve().parent
REPO_DIR = TOOLS_DIR.parent
SRC_DIR = REPO_DIR / "src"
DB_PATH = SRC_DIR / "db" / "db.sqlite3"
LOG_DIR = TOOLS_DIR / "logs"
FAILURE_LOG = LOG_DIR / "warm_media_failures.txt"

sys.path.insert(0, str(SRC_DIR))

import os  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from app import image_relay  # noqa: E402

IMG_NONE = settings.IMG_NONE

MEDIA_TYPES = ("book", "movie", "tv")

# Why a URL is not fetchable, in words the user can act on.
SKIP_DATA = "data-uri"
SKIP_PLACEHOLDER = "placeholder"
SKIP_HOST = "host-not-allowed"


def upstream_url(stored):
    """Return the real image URL for a stored ``app_item.image`` value.

    Movies and series keep the relay URL that ``image_relay.localize`` built,
    i.e. ``/image_relay?url=<percent-encoded upstream>``; books and games keep
    the upstream URL itself. The relay keys its cache on the *upstream* URL,
    so it is unquoted here. Anything that cannot be decoded is returned as is
    and then dropped by :func:`classify`.
    """
    value = (stored or "").strip()
    if not value:
        return ""
    if value.startswith("data:"):
        return value
    parsed = urlparse(value)
    # Relative form written by localize(): the query string still holds the
    # percent encoded upstream URL. Absolute URLs on our own host would look
    # the same, so the path check is what distinguishes them.
    if parsed.path.rstrip("/").endswith("image_relay"):
        candidates = parse_qs(parsed.query).get("url") or []
        if candidates:
            return candidates[0].strip()
        return ""
    return value


def classify(url):
    """Return ``None`` when the URL is worth warming, else a skip reason."""
    if not url:
        return "empty"
    if url == IMG_NONE:
        return SKIP_PLACEHOLDER
    if url.startswith("data:"):
        return SKIP_DATA
    if not url.startswith(("http://", "https://")):
        return "not-http"
    # The relay only fetches hosts on its whitelist; anything else would be
    # a wasted request (and the page never asks the relay for it either).
    if not image_relay.should_cache(url):
        return SKIP_HOST
    if not image_relay.host_allowed(url):
        return SKIP_HOST
    return None


def load_covers(database, media_types, row_counts):
    """Yield ``(media_type, upstream_url)`` for every distinct stored cover.

    ``row_counts`` is filled with the raw number of non-empty ``image`` rows per
    media type, so the report can show "279 rows -> 278 distinct URLs" instead
    of quietly dropping the duplicate.
    """
    placeholders = ",".join("?" * len(media_types))
    sql = (
        "SELECT media_type, image FROM app_item "
        f"WHERE media_type IN ({placeholders}) "
        "AND image IS NOT NULL AND image <> '' "
        "ORDER BY media_type, id"
    )
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = []
        # The site may be writing while we read; SQLite answers "locked" for a
        # moment, so wait and retry instead of dying.
        for attempt in range(5):
            try:
                rows = list(connection.execute(sql, media_types))
                break
            except sqlite3.OperationalError as error:
                if "locked" not in str(error) or attempt == 4:
                    raise
                print(f"[warm-media] database locked, retry {attempt + 1}/5", flush=True)
                time.sleep(5)
        seen = set()
        for media_type, stored in rows:
            row_counts[media_type] = row_counts.get(media_type, 0) + 1
            url = upstream_url(stored)
            key = (media_type, url)
            if url and key in seen:
                continue
            if url:
                seen.add(key)
            yield media_type, url
    finally:
        connection.close()


def cached_path(url):
    """Return the on-disk path already holding this URL, or None.

    Mirrors the relay: an image lives at ``<dir>/<digest[:2]>/<digest><ext>``
    and the extension comes from the content type actually fetched, so every
    suffix the relay knows has to be probed rather than assumed.
    """
    digest = image_relay.image_digest(url)
    bucket = image_relay.IMAGE_DIR / digest[:2]
    for suffix in image_relay._CONTENT_TYPE_BY_SUFFIX:  # noqa: SLF001
        path = bucket / f"{digest}{suffix}"
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return None


def expected_paths(url):
    """Return the possible cache paths for this URL (for reporting)."""
    digest = image_relay.image_digest(url)
    bucket = image_relay.IMAGE_DIR / digest[:2]
    return [bucket / f"{digest}{s}" for s in image_relay._CONTENT_TYPE_BY_SUFFIX]  # noqa: SLF001


def describe_failure(url, response):
    """Return a one-line reason for a failed fetch (status / error kind)."""
    if response is None:
        return "timeout-or-connection-error"
    status = getattr(response, "status_code", 0)
    if status:
        return f"HTTP {status}"
    return "no-response"


def fetch_one(url):
    """Fetch one image through the relay's own code path.

    Returns ``(content, content_type, response, error)``. ``fetch_image``
    swallows the reason, so the request is retried here only to recover a
    readable reason; the relay is left untouched.
    """
    content, content_type = image_relay.fetch_image(url)
    if content is not None:
        return content, content_type, None, None

    # Second attempt, keeps the reason this time. Same headers the relay uses.
    import requests  # noqa: PLC0415

    headers = {"User-Agent": image_relay.USER_AGENT}
    referer = image_relay.referer_for(url)
    if referer:
        headers["Referer"] = referer
    try:
        response = requests.get(
            url, headers=headers, timeout=image_relay.REQUEST_TIMEOUT
        )
    except requests.Timeout:
        return None, None, None, "timeout"
    except requests.RequestException as error:
        return None, None, None, type(error).__name__
    return None, None, response, describe_failure(url, response)


def append_failure(media_type, url, reason):
    """Append one line to the failure log (UTF-8; the machine defaults to GBK)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with FAILURE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp}\t{media_type}\t{reason}\t{url}\n")


def parse_only(value):
    """Turn ``--only book,movie`` into a validated list of media types."""
    if not value:
        return list(MEDIA_TYPES)
    chosen = []
    for part in value.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name not in MEDIA_TYPES:
            raise argparse.ArgumentTypeError(
                f"unknown media type {name!r} (choose from {', '.join(MEDIA_TYPES)})"
            )
        if name not in chosen:
            chosen.append(name)
    if not chosen:
        raise argparse.ArgumentTypeError("--only needs at least one media type")
    return chosen


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Warm the book/film/series cover cache (serial, resumable).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="only report totals, download nothing (this is the default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="actually download the missing covers",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="stop after N downloads (0 = all missing)",
    )
    parser.add_argument(
        "--only",
        default=",".join(MEDIA_TYPES),
        help="comma separated subset of book,movie,tv (default: all three)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print one line per image",
    )
    args = parser.parse_args(argv)

    try:
        media_types = parse_only(args.only) if args.only else list(MEDIA_TYPES)
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    apply = bool(args.apply)
    if args.dry_run:
        apply = False

    print(
        f"[warm-media] mode={'apply' if apply else 'dry-run'} "
        f"only={','.join(media_types)} limit={args.limit or 'all'}",
        flush=True,
    )
    print(f"[warm-media] cache dir={image_relay.IMAGE_DIR}", flush=True)

    # ---------------------------------------------------------------- gather
    # ``total`` is the raw number of non-empty image rows, because that is what
    # "total=279" means to the user. Two books can point at the same default
    # placeholder image, so the number of distinct *URLs* may be smaller; that
    # is reported separately as ``distinct`` rather than silently folded in.
    stats = {
        media_type: {
            "total": 0,
            "distinct": 0,
            "already_cached": 0,
            "to_fetch": 0,
            "ok": 0,
            "fail": 0,
        }
        for media_type in media_types
    }
    skipped = {media_type: {} for media_type in media_types}
    row_counts = {}
    pending = []

    for media_type, url in load_covers(DB_PATH, media_types, row_counts):
        bucket = stats[media_type]
        bucket["distinct"] += 1
        reason = classify(url)
        if reason is not None:
            skipped[media_type][reason] = skipped[media_type].get(reason, 0) + 1
            if args.verbose:
                print(f"[warm-media] skip {media_type} {reason} {url[:100]}", flush=True)
            continue
        path = cached_path(url)
        if path is not None:
            bucket["already_cached"] += 1
            if args.verbose:
                print(f"[warm-media] have {media_type} {path}", flush=True)
            continue
        bucket["to_fetch"] += 1
        pending.append((media_type, url))

    for media_type in media_types:
        stats[media_type]["total"] = row_counts.get(media_type, 0)

    for media_type in media_types:
        bucket = stats[media_type]
        extra = ""
        if bucket["distinct"] != bucket["total"]:
            extra += f" distinct={bucket['distinct']}"
        if skipped[media_type]:
            detail = " ".join(
                f"{reason}={count}" for reason, count in sorted(skipped[media_type].items())
            )
            extra += f" skipped[{detail}]"
        print(
            f"[warm-media] {media_type:5s} total={bucket['total']:4d} "
            f"already_cached={bucket['already_cached']:4d} "
            f"to_fetch={bucket['to_fetch']:4d}{extra}",
            flush=True,
        )

    total_pending = len(pending)
    if not apply:
        print(
            f"[warm-media] dry-run: {total_pending} image(s) would be fetched; "
            "nothing downloaded (add --apply to do it)",
            flush=True,
        )
        return 0
    if not pending:
        print("[warm-media] nothing to fetch, cache is already warm", flush=True)
        return 0

    if args.limit and args.limit > 0:
        pending = pending[: args.limit]
    print(f"[warm-media] fetching {len(pending)} of {total_pending} missing", flush=True)

    # ---------------------------------------------------------------- fetch
    started = time.time()
    failures = 0
    for index, (media_type, url) in enumerate(pending, start=1):
        content, content_type, response, error = fetch_one(url)
        bucket = stats[media_type]
        if content is None:
            bucket["fail"] += 1
            failures += 1
            reason = error or describe_failure(url, response)
            append_failure(media_type, url, reason)
            print(
                f"[warm-media] {index}/{len(pending)} FAIL {media_type} {reason} {url[:110]}",
                flush=True,
            )
        else:
            bucket["ok"] += 1
            path = cached_path(url)
            if args.verbose:
                print(
                    f"[warm-media] {index}/{len(pending)} ok {media_type} "
                    f"{len(content) / 1024:.0f} KiB {content_type} "
                    f"{path if path else 'NOT-ON-DISK!'}",
                    flush=True,
                )
            elif index % 10 == 0 or index == len(pending):
                elapsed = time.time() - started
                rate = index / elapsed if elapsed else 0
                print(
                    f"[warm-media] {index}/{len(pending)} ok={sum(b['ok'] for b in stats.values())} "
                    f"fail={failures} {rate:.2f} img/s {elapsed:.0f}s",
                    flush=True,
                )
        if index < len(pending):
            # Douban throttles per IP. Serial + a human-ish pause is the whole
            # point; a burst would get the address blocked, not just slow.
            time.sleep(random.uniform(0.8, 1.2))

    elapsed = time.time() - started

    # -------------------------------------------------------------- summary
    print("[warm-media] ---- summary ----", flush=True)
    totals = {"total": 0, "distinct": 0, "already_cached": 0, "to_fetch": 0, "ok": 0, "fail": 0}
    for media_type in media_types:
        bucket = stats[media_type]
        for key in totals:
            totals[key] += bucket[key]
        print(
            f"[warm-media] {media_type:5s} total={bucket['total']:4d} "
            f"already_cached={bucket['already_cached']:4d} "
            f"to_fetch={bucket['to_fetch']:4d} "
            f"ok={bucket['ok']:4d} fail={bucket['fail']:4d}",
            flush=True,
        )
    print(
        f"[warm-media] ALL   total={totals['total']:4d} "
        f"already_cached={totals['already_cached']:4d} "
        f"to_fetch={totals['to_fetch']:4d} "
        f"ok={totals['ok']:4d} fail={totals['fail']:4d} "
        f"in {elapsed:.1f}s",
        flush=True,
    )
    if failures:
        print(f"[warm-media] failure log: {FAILURE_LOG}", flush=True)
    remaining = totals["to_fetch"] - len(pending)
    if remaining > 0:
        print(
            f"[warm-media] {remaining} image(s) still missing (--limit); re-run to continue",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
