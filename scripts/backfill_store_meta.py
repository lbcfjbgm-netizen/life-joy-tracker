# -*- coding: utf-8 -*-
"""把本地已抓好的 Steam 商店原始 JSON 回填到 `Item` 的四个新字段。

零联网，只读本地 JSON。数据源：
    .tools/raw/steam_store/2026-10-22/<appid>.json          （中文 schinese 查询结果）
    .tools/raw/steam_store/2026-10-22/<appid>.english.json  （英文查询结果，部分有）

回填字段与铁律：
  * synopsis      ← short_description（中文、无 HTML）。**只在当前为空时填**，已有值绝不动。
  * genres        ← genres[].description（中文类型名数组）。只在当前是 [] 时填。
  * release_date  ← release_date.date「2020 年 12 月 9 日」→ datetime.date。
                    只在当前是 NULL 时填；「即将推出 / Coming Soon / Q1 2024」这类
                    非具体日期的直接跳过。
  * title_zh      ← 中文 JSON 的 name。只有确实含中日韩汉字时才填，纯英文/纯符号跳过。
  * title_en      ← 英文 JSON 的 name。没有 english 文件的不填，只在报告里记缺口。

**title 永远不动** —— 那是用户现在看到的显示名，他要自己手动改中文名。

用法（在 src 目录跑）：
    ..\\.venv\\Scripts\\python.exe ..\\scripts\\backfill_store_meta.py            # 默认 dry-run
    ..\\.venv\\Scripts\\python.exe ..\\scripts\\backfill_store_meta.py --apply    # 真写库
    ..\\.venv\\Scripts\\python.exe ..\\scripts\\backfill_store_meta.py --apply --continue

断点续传：--apply 每处理完一批就把 appid 记进 `.tools/store_meta_progress.json`
（复用 scripts/progress_cursor.py）。重跑时已记录的 appid 直接跳过。
dry-run 不碰游标文件，每次都完整统计。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPTS_DIR.parent / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402

from app.models import Item  # noqa: E402

from progress_cursor import open_cursor  # noqa: E402

PROJECT_ROOT = SRC_DIR.parent
RAW_DIR = PROJECT_ROOT / ".tools" / "raw" / "steam_store" / "2026-10-22"
CURSOR_NAME = "store_meta"
BATCH_SIZE = 100

# 中日韩汉字（含扩展 A 区与兼容区）；用来判断名字到底有没有中文
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# Steam 中文日期：「2020 年 12 月 9 日」，空格可有可无
ZH_DATE = re.compile(r"^\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*$")
HTML_TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"[\s\u3000]+")

FIELD_KEYS = ("synopsis", "genres", "release_date", "title_zh", "title_en")


def clean_text(value):
    """Collapse whitespace and strip stray HTML, returning '' for nothing usable."""
    if not isinstance(value, str):
        return ""
    text = HTML_TAG.sub(" ", value)
    text = text.replace("\u200b", " ").replace("\xa0", " ")
    text = WS.sub(" ", text).strip()
    return text


def parse_zh_date(raw):
    """'2020 年 12 月 9 日' -> date(2020, 12, 9); 非具体日期返回 None."""
    if not isinstance(raw, str):
        return None
    match = ZH_DATE.match(raw)
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def load_zh_payload(appid):
    """Return (data, raw_release) for <appid>.json; (None, ...) when unusable."""
    path = RAW_DIR / f"{appid}.json"
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    entry = payload.get(str(appid))
    if not isinstance(entry, dict) or not entry.get("success"):
        return None
    data = entry.get("data")
    return data if isinstance(data, dict) else None


def load_en_name(appid):
    """Return the English store name for <appid>.english.json, or None."""
    path = RAW_DIR / f"{appid}.english.json"
    if not path.exists():
        return None, "no_english_file"
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None, "english_unreadable"
    if not isinstance(payload, dict):
        return None, "english_unreadable"
    entry = payload.get(str(appid))
    if not isinstance(entry, dict) or not entry.get("success"):
        return None, "english_unsuccessful"
    data = entry.get("data") or {}
    name = clean_text(data.get("name"))
    if not name:
        return None, "english_name_empty"
    return name[:500], "ok"


def plan_item(item, stats):
    """Work out the changes for one Item and record per-field counters."""
    appid = str(item.media_id)
    data = load_zh_payload(appid)
    changes = {}

    if data is None:
        for key in ("synopsis", "genres", "release_date", "title_zh"):
            stats[f"{key}.skip.no_store_data"] += 1
        stats["items.store_failed"] += 1
    else:
        # ---- synopsis: 只在空的时候填，绝不覆盖已有值 ----
        current = (item.synopsis or "").strip()
        if current:
            stats["synopsis.skip.already_set"] += 1
        else:
            text = clean_text(data.get("short_description"))
            if text:
                changes["synopsis"] = text
                stats["synopsis.fill"] += 1
            else:
                stats["synopsis.skip.no_data"] += 1

        # ---- genres: 只填空数组 ----
        if item.genres:
            stats["genres.skip.already_set"] += 1
        else:
            names = []
            for entry in data.get("genres") or []:
                if isinstance(entry, dict):
                    label = clean_text(entry.get("description"))
                    if label and label not in names:
                        names.append(label)
            if names:
                changes["genres"] = names
                stats["genres.fill"] += 1
            else:
                stats["genres.skip.no_data"] += 1

        # ---- release_date: 只填 NULL，且必须是具体日期 ----
        if item.release_date is not None:
            stats["release_date.skip.already_set"] += 1
        else:
            raw_date = (data.get("release_date") or {}).get("date")
            parsed = parse_zh_date(raw_date)
            if parsed is not None:
                changes["release_date"] = parsed
                stats["release_date.fill"] += 1
            elif not clean_text(raw_date):
                stats["release_date.skip.no_data"] += 1
            else:
                stats["release_date.skip.bad_format"] += 1

        # ---- title_zh: 只在空且名字确实含中文时填 ----
        if (item.title_zh or "").strip():
            stats["title_zh.skip.already_set"] += 1
        else:
            name = clean_text(data.get("name"))
            if name and CJK.search(name):
                changes["title_zh"] = name[:500]
                stats["title_zh.fill"] += 1
            elif not name:
                stats["title_zh.skip.no_data"] += 1
            else:
                stats["title_zh.skip.no_cjk"] += 1

    # ---- title_en: 只认 english 文件 ----
    if (item.title_en or "").strip():
        stats["title_en.skip.already_set"] += 1
    else:
        en_name, reason = load_en_name(appid)
        if en_name:
            changes["title_en"] = en_name
            stats["title_en.fill"] += 1
        else:
            stats[f"title_en.skip.{reason}"] += 1
            if reason == "no_english_file":
                stats["title_en.gap_no_english_file"] += 1

    return changes


def main():
    parser = argparse.ArgumentParser(description="回填 Steam 商店元数据（synopsis/genres/release_date/title_zh/title_en）")
    parser.add_argument("--apply", action="store_true", help="真写库（默认只试算）")
    parser.add_argument("--dry-run", action="store_true", help="显式试算（默认行为，写出来更清楚）")
    parser.add_argument("--continue", dest="resume", action="store_true", help="按游标续跑（--apply 时默认开启）")
    parser.add_argument("--fresh", action="store_true", help="忽略并清空游标，从头跑")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少条（调试用）")
    parser.add_argument("--stats-out", default=str(PROJECT_ROOT / ".tools" / "store_meta_backfill_stats.json"))
    args = parser.parse_args()

    dry_run = not args.apply
    if args.dry_run and args.apply:
        parser.error("--dry-run 和 --apply 只能选一个")
    stats = Counter()

    cursor = None
    if not dry_run:
        if args.fresh:
            path = PROJECT_ROOT / ".tools" / f"{CURSOR_NAME}_progress.json"
            if path.exists():
                path.unlink()
        cursor = open_cursor(CURSOR_NAME, every=25, verbose=False)

    items = list(
        Item.objects.filter(media_type="game")
        .only("id", "media_id", "synopsis", "genres", "release_date", "title_zh", "title_en")
        .order_by("id")
    )
    stats["items.total"] = len(items)

    pending = []
    for item in items:
        key = str(item.media_id)
        if cursor is not None and key in cursor:
            stats["items.already_in_cursor"] += 1
            continue
        pending.append(item)
        if args.limit and len(pending) >= args.limit:
            break

    print(f"{'DRY-RUN' if dry_run else 'APPLY'} | 待处理 {len(pending)} 条 / 库内游戏 {len(items)} 条")
    print(f"数据源: {RAW_DIR}")

    batch = []
    touched_fields = Counter()
    touched_items = 0

    def flush_batch():
        if not batch:
            return
        items_to_save = [obj for obj, _ in batch]
        if not dry_run:
            with transaction.atomic():
                Item.objects.bulk_update(
                    items_to_save,
                    ["synopsis", "genres", "release_date", "title_zh", "title_en"],
                    batch_size=BATCH_SIZE,
                )
                if cursor is not None:
                    cursor.mark_all([str(o.media_id) for o in items_to_save], "applied")
        batch.clear()

    for index, item in enumerate(pending, 1):
        changes = plan_item(item, stats)
        if changes:
            for key, value in changes.items():
                setattr(item, key, value)
                touched_fields[key] += 1
            touched_items += 1
            batch.append((item, changes))
            if len(batch) >= BATCH_SIZE:
                flush_batch()
        elif cursor is not None:
            # 没东西可改也要记游标，否则重启会重复扫
            cursor.mark(str(item.media_id), "noop")
        if index % 500 == 0:
            print(f"  ... 已处理 {index}/{len(pending)}")
    flush_batch()
    if cursor is not None:
        cursor.flush(force=True)

    # “至少改了一个字段的条目数”，与每字段的 fill 数不是一回事
    stats["items.changed"] = touched_items

    print("\n=== 每字段结果 ===")
    for key in FIELD_KEYS:
        fill = stats.get(f"{key}.fill", 0)
        skips = sorted(
            ((k.split(".", 2)[2], v) for k, v in stats.items() if k.startswith(f"{key}.skip.")),
        )
        total_skip = sum(v for _, v in skips)
        detail = ", ".join(f"{name}={v}" for name, v in skips)
        print(f"{key:<12} 填 {fill:>5}   跳过 {total_skip:>5}   ({detail})")

    print(f"\n至少改一个字段的条目: {stats.get('items.changed', 0)}")
    print(f"游标: {(PROJECT_ROOT / '.tools' / (CURSOR_NAME + '_progress.json')) if cursor is not None else '(dry-run 不写)'}")

    payload = {
        "mode": "apply" if not dry_run else "dry-run",
        "raw_dir": str(RAW_DIR),
        "fields": {
            key: {
                "fill": stats.get(f"{key}.fill", 0),
                "skip": {
                    k.split(".", 2)[2]: v
                    for k, v in stats.items()
                    if k.startswith(f"{key}.skip.")
                },
            }
            for key in FIELD_KEYS
        },
        "items": {
            "total": stats.get("items.total", 0),
            "changed": stats.get("items.changed", 0),
            "already_in_cursor": stats.get("items.already_in_cursor", 0),
            "store_failed": stats.get("items.store_failed", 0),
        },
        "raw_stats": {k: v for k, v in stats.items() if k.startswith("title_en.gap")},
    }
    Path(args.stats_out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"统计已写入: {args.stats_out}")


if __name__ == "__main__":
    main()
