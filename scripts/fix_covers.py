"""Repair the broken Steam cover images of the already imported games.

The importer used to store ``library_600x900.jpg`` for every game, but that
portrait asset is missing for a large share of the Steam catalog (delisted,
region locked or simply never uploaded titles). This script walks the stored
Steam items and replaces the cover with the best variant the CDN actually
serves, falling back from the portrait library capsule to the wide header and
then to the small capsule.

Usage (from ``src``)::

    ../.venv/Scripts/python.exe ../scripts/fix_covers.py
    ../.venv/Scripts/python.exe ../scripts/fix_covers.py --dry-run
    ../.venv/Scripts/python.exe ../scripts/fix_covers.py --sample 40

``--dry-run`` only measures, it never writes to the database.
``--sample N`` repairs (or measures) a random sample of N games instead of the
whole library, which is handy to compare the before/after health.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from app.models import Item  # noqa: E402

# Only this Steam asset host answered reliably from this machine: the
# Cloudflare and Akamai variants time out for a large share of app IDs. It is
# asked first, the others only serve as a fallback when it is unreachable.
CDN_HOSTS = (
    "https://media.steampowered.com/steam/apps",
    "https://cdn.cloudflare.steamstatic.com/steam/apps",
)

# Games released after the storefront migration (App ID of three million or
# more) were never mirrored to the old path, so their covers live on the new
# asset host. Older games stay on the old host, which still serves their art.
NEW_APPID_MIN = 3_000_000
NEW_CDN_BASE = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps"

# Ordered from the prettiest (portrait, matches the card aspect ratio) to the
# one that is nearly always present.
COVER_KINDS = ("library_600x900.jpg", "header.jpg", "capsule_616x353.jpg")

# Codes the CDN uses for a cover that was never uploaded
MISSING_CODES = (403, 404, 410)

REQUEST_TIMEOUT = 10
HEAD_RETRIES = 2
WORKERS = 10
BATCH_SIZE = 200
USER_AGENT = "Mozilla/5.0 (compatible; YamtrackCoverRepair/1.0)"

_local = threading.local()


def _session() -> requests.Session:
    """Return the requests session of the current worker thread."""
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        session.mount(
            "https://",
            requests.adapters.HTTPAdapter(pool_connections=WORKERS, pool_maxsize=WORKERS),
        )
        _local.session = session
    return session


def check(url: str) -> bool | None:
    """Return True when ``url`` serves a real image, None when it can't be told.

    A connection error says nothing about the cover, so it is reported as
    unknown instead of "missing": that keeps a network hiccup from throwing
    away a perfectly valid URL.
    """
    unknown_seen = False
    for attempt in range(HEAD_RETRIES):
        try:
            response = _session().head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        except requests.RequestException:
            unknown_seen = True
            time.sleep(0.25 * (attempt + 1))
            continue

        if response.status_code == 200:
            length = int(response.headers.get("Content-Length") or 0)
            ctype = response.headers.get("Content-Type", "")
            if length > 2000 and "image" in ctype:
                return True
            # A 200 with no image body is a soft error, keep looking
            unknown_seen = True
            continue
        if response.status_code in MISSING_CODES:
            # A definite answer, no need to retry this URL
            return False
        unknown_seen = True
        time.sleep(0.25 * (attempt + 1))

    return None if unknown_seen else False


def urls_for(appid: str, kind: str) -> list[str]:
    """Return the candidate URLs of a cover, best host first."""
    try:
        is_new = int(appid) >= NEW_APPID_MIN
    except (TypeError, ValueError):
        is_new = False
    if is_new:
        # The plain paths of a new game miss on every old host; only the new
        # asset host has a chance of answering.
        return [f"{NEW_CDN_BASE}/{appid}/{kind}"]
    primary = f"{CDN_HOSTS[0]}/{appid}/{kind}"
    fallback = f"{CDN_HOSTS[1]}/{appid}/{kind}"
    return [primary, fallback]


def resolve(appid: str) -> tuple[str | None, bool]:
    """Return the best reachable cover of a game and whether it is certain.

    Only an explicit 403/404/410 downgrades to the next variant, and the
    fallback host is only tried when the primary one failed for a *network*
    reason: asking both hosts for every missing file doubles the run time for
    no better result. The second value is False when the outcome was
    inconclusive, so the caller leaves the stored cover untouched.
    """
    inconclusive = False
    for kind in COVER_KINDS:
        primary, fallback = urls_for(appid, kind)
        found = check(primary)
        if found is True:
            return primary, True
        if found is None:
            # The primary host is unreachable, this one is worth a try
            inconclusive = True
            if check(fallback) is True:
                return fallback, True
            continue
        # A definite 404 on the primary host also means missing on the mirror
    if inconclusive:
        return None, False
    return None, True


def cover_kind(url: str | None) -> str:
    """Return the asset file name of a cover URL, '' when there is none."""
    if not url:
        return ""
    return url.rsplit("/", 1)[-1]


def repair(items, *, dry_run: bool, label: str) -> dict:
    """Resolve the covers of ``items`` and write the improved ones back."""
    total = len(items)
    print(f"[{label}] checking {total} games with {WORKERS} workers", flush=True)

    started = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        resolved = list(pool.map(lambda entry: resolve(str(entry.media_id)), items))

    changed, unknown, dead = [], 0, []
    for item, (url, certain) in zip(items, resolved):
        if url:
            if url != item.image:
                item.image = url
                changed.append(item)
        elif certain:
            # Nothing on the CDN, fall back to the placeholder
            dead.append(item)
            if item.image != settings.IMG_NONE:
                item.image = settings.IMG_NONE
                changed.append(item)
        else:
            # Inconclusive network error, keep whatever is stored
            unknown += 1
            print(f"[{label}]   network error, untouched: {item.media_id}", flush=True)

    if changed and not dry_run:
        for start in range(0, len(changed), BATCH_SIZE):
            Item.objects.bulk_update(changed[start : start + BATCH_SIZE], ["image"])

    kinds: dict[str, int] = {}
    for item in items:
        name = cover_kind(item.image) or "(none)"
        kinds[name] = kinds.get(name, 0) + 1

    elapsed = time.time() - started
    report = {
        "label": label,
        "checked": total,
        "changed": len(changed),
        "portrait": kinds.get("library_600x900.jpg", 0),
        "wide": kinds.get("header.jpg", 0),
        "capsule": kinds.get("capsule_616x353.jpg", 0),
        "placeholder": len(dead),
        "unknown": unknown,
        "kinds": kinds,
        "seconds": round(elapsed, 1),
        "dry_run": dry_run,
    }
    print(
        f"[{label}] checked={total} portrait={report['portrait']} "
        f"wide={report['wide']} capsule={report['capsule']} "
        f"placeholder={len(dead)} network_error_untouched={unknown} "
        f"written={len(changed) if not dry_run else 0} in {elapsed:.1f}s",
        flush=True,
    )
    return report


def sample_health(items, *, workers: int = WORKERS) -> dict:
    """Return the share of games whose *stored* cover actually loads."""
    def alive(item):
        return check(item.image) is True

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(alive, items))

    good = sum(1 for value in results if value)
    return {
        "sample": len(items),
        "ok": good,
        "rate": round(100 * good / max(1, len(items)), 1),
    }


def steam_items():
    """Return every imported Steam game item."""
    return list(
        Item.objects.filter(source="steam", media_type="game").only("id", "media_id", "image"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="measure only, never write")
    parser.add_argument("--sample", type=int, default=0, help="only handle N random games")
    parser.add_argument("--seed", type=int, default=42, help="seed of the random sample")
    parser.add_argument("--workers", type=int, default=WORKERS, help="parallel cover checks")
    parser.add_argument("--health", type=int, default=40, help="size of the health sample")
    parser.add_argument(
        "--all",
        action="store_true",
        help="also check covers that are not the library capsule",
    )
    parser.add_argument("--json", type=Path, default=None, help="write the report to this file")
    args = parser.parse_args()

    globals()["WORKERS"] = max(1, args.workers)

    items = steam_items()
    if not items:
        print("no Steam items found, nothing to do")
        return 0

    random.seed(args.seed)
    health_sample = random.sample(items, min(args.health, len(items)))

    print(
        f"steam games={len(items)} health_sample={len(health_sample)} "
        f"dry_run={args.dry_run} workers={WORKERS}",
        flush=True,
    )

    before = sample_health(health_sample)

    if args.sample:
        target = random.sample(items, min(args.sample, len(items)))
    elif args.all:
        target = items
    else:
        target = [item for item in items if "library_600x900" in (item.image or "")]

    print(f"target={len(target)}", flush=True)
    report = {
        "before": before,
        "repair": repair(target, dry_run=args.dry_run, label="repair"),
    }

    if not args.dry_run:
        # Re-read the stored rows so the numbers reflect what is really saved
        stored = {
            row.pk: row.image
            for row in Item.objects.filter(pk__in=[item.pk for item in health_sample])
        }
        for item in health_sample:
            item.image = stored.get(item.pk, item.image)

    report["after"] = sample_health(health_sample)

    print(
        f"health of {before['sample']} sampled games: "
        f"before={before['rate']}% ({before['ok']}/{before['sample']}) "
        f"after={report['after']['rate']}% ({report['after']['ok']}/{report['after']['sample']})",
        flush=True,
    )

    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"report written to {args.json}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
