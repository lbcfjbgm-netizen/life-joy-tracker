# -*- coding: utf-8 -*-
"""把 Notion「综合评分」回填到 `Game.score`。

数据源不重新解析 JSON：`ImportItem.raw["score"]` 里已经有全部值，
且已逐条核对与 `.tools/raw/notion/2026-10-22/games_all.json` 一致。

三条铁律：
  * 只填空，不覆盖 —— `Game.score` 已有值的直接跳过（用户手动记录优先）。
  * **跳过 -10 分那一条**（Revolution Idle / appid 2763740）。
    `statistics.py` 用 `dict.fromkeys(range(11))` 再用 `int(score)` 当键，
    负分会 KeyError 让 /statistics 500；网页评分器有 `0 <= score <= 10` 守卫，
    负分写进去页面上也改不回来。留空比改成 0 诚实。
  * 不改 `Item`、不建 migration、不碰其他字段。

用法（在 src 目录跑）：
    ..\\.venv\\Scripts\\python.exe ..\\scripts\\backfill_scores.py            # dry-run
    ..\\.venv\\Scripts\\python.exe ..\\scripts\\backfill_scores.py --apply    # 真写库
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPTS_DIR.parent / "src"
sys.path.insert(0, str(SRC_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from app.models import Game  # noqa: E402

SKIP_APPIDS = {"2763740"}  # Revolution Idle：Notion 里是 -10，站点不支持负分


def collect() -> tuple[dict[int, Decimal], list[str]]:
    """返回 {game_id: score} 和跳过原因列表，全程只读。"""
    from app.models import ImportItem

    plan: dict[int, Decimal] = {}
    skipped: list[str] = []

    qs = ImportItem.objects.exclude(item__isnull=True).exclude(raw__isnull=True)
    for ii in qs:
        score = ii.raw.get("score")
        if score is None:
            skipped.append(f"item={ii.item_id} 无 score")
            continue
        try:
            value = Decimal(str(score))
        except Exception:
            skipped.append(f"item={ii.item_id} score={score!r} 解析失败")
            continue

        # Item 没有 game 反向属性，按 item_id 直接查 Game。
        game = Game.objects.filter(item_id=ii.item_id).first()
        if game is None:
            skipped.append(f"item={ii.item_id} 没有 Game")
            continue
        if game.item.media_id in SKIP_APPIDS:
            skipped.append(f"game={game.id} appid={game.item.media_id} 负分 {value} 跳过")
            continue
        if not (Decimal("0") <= value <= Decimal("10")):
            skipped.append(f"game={game.id} 越界 {value} 跳过")
            continue
        if game.score is not None:
            # 只填空不覆盖
            continue
        plan[game.id] = value

    return plan, skipped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真写库（默认 dry-run）")
    args = parser.parse_args()

    plan, skipped = collect()
    dist = Counter(str(v) for v in plan.values())

    print(f"待回填: {len(plan)} 条")
    for key in sorted(dist, key=lambda k: float(k), reverse=True):
        print(f"  {key:>5} 分: {dist[key]} 条")
    if skipped:
        print(f"跳过 {len(skipped)} 条:")
        for line in skipped:
            print(f"  - {line}")

    if not args.apply:
        print("\n[dry-run] 未写库。用 --apply 真写。")
        return 0

    updated = 0
    for game_id, value in plan.items():
        # 再查一次，避免 collect 与写入之间被别处改过
        updated += (
            Game.objects.filter(id=game_id, score__isnull=True).update(score=value)
        )
    print(f"\n已写入 {updated} 条。")

    total = Game.objects.exclude(score__isnull=True).count()
    neg = Game.objects.filter(score__lt=0).count()
    high = Game.objects.filter(score__gt=10).count()
    print(f"score 非空: {total} | 负分: {neg} | >10: {high}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
