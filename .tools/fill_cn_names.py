# ruff: noqa: INP001, RUF001
r"""Fill the Chinese titles of the Steam items, manual table first.

The library imported from Steam carries the name the Web API returns, which is
the one Valve ships to every language, so most cards are titled in plain Latin
letters. The search screen only looks at ``title``, ``title_zh`` and
``title_en``, so a game is findable by its Chinese name only once that name is
stored in ``title_zh``.

Two sources feed it, and the order matters:

1. **The manual table**, ``.tools/raw/cn_names.json``. It is written by hand and
   is therefore trusted completely. A digits only key is a Steam App ID, any
   other key is the exact English title of the game.
2. **The Steam store**, in Simplified Chinese. It only carries a localized name
   for the games whose publisher filled one in, about one in sixty here, so it
   is a bonus rather than the plan. It asks one game at a time and is throttled.

Only ``Item.title_zh`` is ever written. ``title`` stays as imported, and a row
that already holds a different Chinese name is left alone.

Usage (from ``yamtrack/``, always dry-run first)::

    .venv\\Scripts\\python.exe .tools\\fill_cn_names.py --dry-run
    .venv\\Scripts\\python.exe .tools\\fill_cn_names.py --dry-run --limit 50
    .venv\\Scripts\\python.exe .tools\\fill_cn_names.py --apply
    .venv\\Scripts\\python.exe .tools\\fill_cn_names.py --apply --limit 50
    .venv\\Scripts\\python.exe .tools\\fill_cn_names.py --apply --auto-only
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

for stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):
        stream.reconfigure(encoding="utf-8", errors="replace")

import django  # noqa: E402

SAMPLE_SIZE = 15


def say(message):
    """Print one line of progress, flushed so a long run shows it live."""
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def titles_of(appids):
    """Return ``{appid: title_zh}`` for the given Steam games.

    Args:
        appids (Iterable): Steam App IDs to read

    Returns:
        dict: the current Chinese name of every game, empty string when none
    """
    from app.models import Item  # noqa: PLC0415

    wanted = [str(appid) for appid in appids or ()]
    if not wanted:
        return {}

    rows = Item.objects.filter(
        source="steam",
        media_type="game",
        media_id__in=wanted,
    ).values_list("media_id", "title_zh")
    return {str(media_id): (title_zh or "").strip() for media_id, title_zh in rows}


def show(names, label):
    """Print a sample of a name mapping, so a run shows what it found."""
    if not names:
        say(f"    (没有新的{label})")
        return
    for key, name in list(names.items())[:SAMPLE_SIZE]:
        say(f"    {key} -> {name}")
    if len(names) > SAMPLE_SIZE:
        say(f"    ... 还有 {len(names) - SAMPLE_SIZE} 个")


def main(argv=None):
    """Read the manual table, then the store, and store the Chinese names."""
    parser = argparse.ArgumentParser(
        description="Fill Item.title_zh from the manual table, then the store",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the changes without writing them",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the Chinese names into Item.title_zh",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="stop after this many games, 0 for every one of them",
    )
    parser.add_argument(
        "--auto-only",
        action="store_true",
        help="skip the Steam store and use the hand written table only",
    )
    parser.add_argument(
        "--auto-limit",
        type=int,
        default=0,
        help="ask the store about at most this many games, 0 for every one",
    )
    args = parser.parse_args(argv)

    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    dry_run = not args.apply
    django.setup()

    # The Django settings log at DEBUG, which would bury the report under the
    # per game lines of the module; the report itself is what this tool prints
    logging.getLogger("integrations.imports.cn_names").setLevel(logging.WARNING)

    from integrations.imports import cn_names  # noqa: PLC0415

    mode = "DRY-RUN(不写库)" if dry_run else "APPLY(只写 title_zh)"
    candidates = cn_names.candidate_appids(max(args.limit, 0) or None)
    say(f"[STAGE] 选条目 — 候选 {len(candidates)} 个 / 模式 {mode}")

    # 1. The manual table, which is never wrong and costs no request
    manual = cn_names.resolve_manual_names(set(candidates))
    say(f"[STAGE] 手工表 — {len(manual)} 个候选有手工名")
    show(manual, "手工名")
    manual_counts = cn_names.apply_names(manual, dry_run=dry_run) if manual else {}

    # 2. The store, for the games the manual table did not name
    auto = {}
    if args.auto_only:
        say("[STAGE] Steam 抓取 — 已按要求跳过")
    else:
        pending = [appid for appid in candidates if appid not in manual]
        if args.auto_limit:
            pending = pending[: args.auto_limit]
        say(f"[STAGE] Steam 抓取 — 待问 {len(pending)} 个")
        if pending:
            auto = cn_names.auto_names(pending)
            if cn_names.LAST_RUN_GAVE_UP:
                say(
                    "    Steam 商店连不上，已连续失败 5 次后放弃剩下的; "
                    "这次只算手工表的结果",
                )
        show(auto, "Steam 名")
    auto_counts = cn_names.apply_names(auto, dry_run=dry_run) if auto else {}

    # 3. How many of the candidates still have no Chinese name once this run is
    # done. The names found but not written yet count as written here, so a
    # dry run reports the same number the apply run will produce.
    current = titles_of(candidates)
    filled = set(manual) | set(auto)
    still = sum(
        1
        for appid in candidates
        if not current.get(appid, "") and appid not in filled
    )
    updated = manual_counts.get("updated", 0) + auto_counts.get("updated", 0)
    skipped = manual_counts.get("skipped", 0) + auto_counts.get("skipped", 0)

    say("")
    say("=== 预演结果（没有写库）===" if dry_run else "=== 写入结果 ===")
    say(f"手工表命中       : {len(manual)} 个")
    say(f"Steam 自动拿到   : {len(auto)} 个")
    say(f"{'将要写入' if dry_run else '写入'} title_zh  : {updated} 条")
    say(f"跳过（已有值）    : {skipped} 条")
    say(f"仍然没有中文名    : {still} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
