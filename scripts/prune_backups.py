#!/usr/bin/env python
"""Retention policy for the SQLite hot backups in yamtrack/.tools/backups.

Why
---
Nothing ever deleted a backup. By 2026-10-22 there were 22 entries / 54.7 MB
after roughly one day of work, with no pruning anywhere: at that rate a year is
about 9 GB of near-identical copies of the same database.

Policy (tunable, defaults below)
--------------------------------
* keep the newest backup of every hour for the last 24 hours
* keep the newest backup of every day for the last 7 days
* keep the newest backup of every calendar month, older data included
* everything else is a deletion candidate

The keep set is the union of those three buckets, so a file is only deleted
when no rule wants it. A few floors are applied on top, because deleting a
backup is unrecoverable:

* the newest backup is always kept, whatever the buckets say
* if fewer than ``--min-keep`` (default 5) backups would survive, nothing is
  deleted at all and the script exits 2
* only ``db*.sqlite3`` files (and their ``-wal`` / ``-shm`` sidecars) inside the
  backup directory are ever considered

Dry run is the default
----------------------
``--apply`` is required to actually delete anything. Without it the script only
prints what it would do, so the decision stays with a human.

Usage
-----
    python scripts/prune_backups.py                       # dry run, prints plan
    python scripts/prune_backups.py --json                # machine readable
    python scripts/prune_backups.py --apply               # really delete
    python scripts/prune_backups.py --keep-hourly 48 --keep-daily 30 --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKUP_DIR = PROJECT_ROOT / ".tools" / "backups"

MAIN_SUFFIX = ".sqlite3"
SIDECAR_SUFFIXES = ("-wal", "-shm")


@dataclass
class Entry:
    """One backup (or sidecar) file with the decisions made about it."""

    path: Path
    mtime: datetime
    size: int
    is_sidecar: bool
    keep: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name


def scan(backup_dir: Path) -> tuple[list[Entry], list[Entry]]:
    """Return (main backups, sidecars) found in the backup directory."""
    mains: list[Entry] = []
    sidecars: list[Entry] = []
    for path in sorted(backup_dir.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if name.endswith(SIDECAR_SUFFIXES):
            # sidecar 跟着主文件走：主文件留下它就留下，主文件删了它也删
            stat = path.stat()
            sidecars.append(Entry(path, datetime.fromtimestamp(stat.st_mtime), stat.st_size, True))
            continue
        if not name.startswith("db") or not name.endswith(MAIN_SUFFIX):
            continue
        stat = path.stat()
        mains.append(Entry(path, datetime.fromtimestamp(stat.st_mtime), stat.st_size, False))
    mains.sort(key=lambda e: e.mtime, reverse=True)
    return mains, sidecars


def _strip_sidecar(name: str) -> str:
    for suffix in SIDECAR_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def plan(mains: list[Entry], now: datetime, keep_hourly: int, keep_daily: int) -> list[Entry]:
    """Mark the keep set on ``mains`` and return it (entries mutated in place).

    Windows are measured from ``now``. If the newest backup is already older
    than the daily window the hourly/daily buckets stay empty on purpose: only
    the monthly bucket keeps one file per month, and the surviving count drops
    below ``--min-keep`` often enough for the caller to abort instead of
    wiping a stale directory.
    """
    if not mains:
        return []

    # 1. 最近 24 小时：每小时留最新的一份
    hourly_cut = now - timedelta(hours=keep_hourly)
    hourly: dict[tuple[int, int, int, int], Entry] = {}
    for entry in mains:  # 已按时间倒序，先到先得 = 这一小时里最新的
        if entry.mtime >= hourly_cut:
            hourly.setdefault(
                (entry.mtime.year, entry.mtime.month, entry.mtime.day, entry.mtime.hour),
                entry,
            )

    # 2. 最近 7 天：每天留最新的一份
    daily_cut = now - timedelta(days=keep_daily)
    daily: dict[tuple[int, int, int], Entry] = {}
    for entry in mains:
        if entry.mtime >= daily_cut:
            daily.setdefault((entry.mtime.year, entry.mtime.month, entry.mtime.day), entry)

    # 3. 每月留最新的一份（不限时间）
    monthly: dict[tuple[int, int], Entry] = {}
    for entry in mains:
        monthly.setdefault((entry.mtime.year, entry.mtime.month), entry)

    for entry in hourly.values():
        entry.keep = True
        if "hourly" not in entry.reasons:
            entry.reasons.append("hourly")
    for entry in daily.values():
        entry.keep = True
        if "daily" not in entry.reasons:
            entry.reasons.append("daily")
    for entry in monthly.values():
        entry.keep = True
        if "monthly" not in entry.reasons:
            entry.reasons.append("monthly")

    # 永远不删最新那份（它还可能是「最近一次成功备份」，就算三条规则都没选中它）
    if not mains[0].keep:
        mains[0].keep = True
        mains[0].reasons.append("newest")

    return [e for e in mains if e.keep]


def human_mb(size: int) -> float:
    return round(size / (1024 * 1024), 2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prune yamtrack SQLite hot backups (dry run by default).",
    )
    parser.add_argument("--dir", default=str(DEFAULT_BACKUP_DIR), help="backup directory")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="really delete. Without this flag the script only prints the plan.",
    )
    parser.add_argument("--keep-hourly", type=int, default=24, help="hourly window in hours")
    parser.add_argument("--keep-daily", type=int, default=7, help="daily window in days")
    parser.add_argument("--min-keep", type=int, default=5, help="abort if fewer would survive")
    parser.add_argument("--json", action="store_true", help="machine readable output")
    parser.add_argument(
        "--now",
        default=None,
        help="simulate at this time, format 'YYYY-MM-DD HH:MM' (dry run testing)",
    )
    args = parser.parse_args(argv)

    backup_dir = Path(args.dir)
    if not backup_dir.is_dir():
        print(f"ERROR backup directory not found: {backup_dir}", file=sys.stderr)
        return 2

    now = datetime.strptime(args.now, "%Y-%m-%d %H:%M") if args.now else datetime.now()

    mains, sidecars = scan(backup_dir)
    if not mains:
        print(f"nothing to do: no db*.sqlite3 files in {backup_dir}")
        return 0

    keepers = plan(mains, now, args.keep_hourly, args.keep_daily)
    keep_ids = {e.path for e in keepers}

    # 主文件要删 -> 它的 sidecar 跟着删；主文件留下 -> sidecar 也留下
    doomed_sidecars: list[Entry] = []
    for sidecar in sidecars:
        parent_name = _strip_sidecar(sidecar.name)
        parent_kept = any(k.name == parent_name for k in keepers)
        if not parent_kept:
            doomed_sidecars.append(sidecar)

    doomed = [e for e in mains if e.path not in keep_ids]
    total = len(mains)
    keep_count = len(keepers)
    doomed_bytes = sum(e.size for e in doomed) + sum(e.size for e in doomed_sidecars)
    keep_bytes = sum(e.size for e in keepers)
    all_bytes = sum(e.size for e in mains) + sum(e.size for e in sidecars)

    if keep_count < args.min_keep:
        print(
            f"ABORT keeping only {keep_count} backups, below --min-keep {args.min_keep}. "
            f"Nothing deleted.",
            file=sys.stderr,
        )
        return 2

    stale_days = (now - mains[0].mtime).total_seconds() / 86400
    if stale_days > args.keep_daily:
        # 目录本身已经很久没出新备份了：这不是「旧备份多」，而是「备份停了」。
        # 这时候按策略删等于把仅存的历史也砍了，直接拒绝。
        print(
            f"ABORT newest backup is {stale_days:.1f} days old (>{args.keep_daily}d). "
            f"Backups look halted, not bloated. Nothing deleted.",
            file=sys.stderr,
        )
        return 3

    if args.json:
        print(
            json.dumps(
                {
                    "backup_dir": str(backup_dir),
                    "now": now.strftime("%Y-%m-%d %H:%M"),
                    "apply": bool(args.apply),
                    "total_backups": total,
                    "keep": [
                        {
                            "name": e.name,
                            "mtime": e.mtime.strftime("%Y-%m-%d %H:%M"),
                            "mb": human_mb(e.size),
                            "reasons": e.reasons,
                        }
                        for e in keepers
                    ],
                    "delete": [
                        {
                            "name": e.name,
                            "mtime": e.mtime.strftime("%Y-%m-%d %H:%M"),
                            "mb": human_mb(e.size),
                        }
                        for e in sorted(doomed, key=lambda x: x.mtime)
                    ],
                    "delete_sidecars": [e.name for e in doomed_sidecars],
                    "summary": {
                        "delete_count": len(doomed),
                        "keep_count": keep_count,
                        "reclaim_mb": human_mb(doomed_bytes),
                        "remaining_mb": human_mb(keep_bytes),
                        "current_total_mb": human_mb(all_bytes),
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        return 0

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== prune_backups [{mode}] dir={backup_dir}")
    print(f"=== now={now:%Y-%m-%d %H:%M}  hourly={args.keep_hourly}h daily={args.keep_daily}d monthly=on")
    print()
    for entry in sorted(mains, key=lambda e: e.mtime):
        tag = "KEEP  " if entry.path in keep_ids else "DELETE"
        why = "+".join(entry.reasons) if entry.keep else "-"
        print(
            f"  {tag} {entry.mtime:%Y-%m-%d %H:%M}  {human_mb(entry.size):6.2f} MB  "
            f"{entry.name}  [{why}]",
        )
    for sidecar in sidecars:
        state = "DELETE" if sidecar in doomed_sidecars else "KEEP  "
        print(f"  {state} {sidecar.mtime:%Y-%m-%d %H:%M}  {human_mb(sidecar.size):6.2f} MB  {sidecar.name}  [sidecar]")
    print()
    print(
        f"SUMMARY total={total} backups  keep={keep_count}  delete={len(doomed)}"
        f"{f' (+{len(doomed_sidecars)} sidecar)' if doomed_sidecars else ''}  "
        f"reclaim={human_mb(doomed_bytes):.2f} MB  "
        f"remaining={human_mb(keep_bytes):.2f} MB  "
        f"current={human_mb(all_bytes):.2f} MB",
    )

    if not args.apply:
        print("DRY-RUN: nothing was deleted. Re-run with --apply to actually remove these files.")
        return 0

    removed = 0
    freed = 0
    for entry in doomed + doomed_sidecars:
        try:
            size = entry.size
            entry.path.unlink()
            removed += 1
            freed += size
            print(f"deleted {entry.name}")
        except OSError as exc:
            print(f"FAILED to delete {entry.name}: {exc}", file=sys.stderr)
    print(f"APPLIED removed={removed} freed={human_mb(freed):.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
