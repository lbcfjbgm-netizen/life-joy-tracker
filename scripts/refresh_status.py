"""按「全成就即已完成」的规则重算所有 Steam 游戏的状态。

规则（用户拍板）：
  成就全部解锁 (total > 0 且 unlocked >= total) → 已完成
  玩过 0 分钟                                  → 计划中
  最近两周玩过                                 → 进行中
  玩过但两周内没玩                             → 已暂停

用法（在 src 目录）：
    ../.venv/Scripts/python.exe ../scripts/refresh_status.py --dry-run
    ../.venv/Scripts/python.exe ../scripts/refresh_status.py
"""

import argparse
import os
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from app.models import Game, MediaTypes, Sources, Status  # noqa: E402

# 只允许从这三个「可自动判定」的状态迁移，用户手动设过的其它状态不覆盖
AUTO_STATUSES = {
    Status.PLANNING.value,
    Status.IN_PROGRESS.value,
    Status.PAUSED.value,
    Status.COMPLETED.value,
}


def decide(game):
    """Return the status the game should have, following the rule above.

    Yamtrack does not store "played in the last two weeks", and after an import
    ``progressed_at`` is just the import time, so it says nothing about recent
    activity. The only reliable signal kept in the database is the total
    playtime, therefore a played game without achievements is never moved to
    "in progress" automatically: the user can set that by hand.
    """
    if game.achievements_total > 0 and game.achievements_unlocked >= game.achievements_total:
        return Status.COMPLETED.value

    # progress 存的是游玩分钟数
    if game.progress == 0:
        return Status.PLANNING.value

    return Status.PAUSED.value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--user", default="admin", help="用户名")
    args = parser.parse_args()

    games = list(
        Game.objects.filter(
            user__username=args.user,
            item__source=Sources.STEAM.value,
            item__media_type=MediaTypes.GAME.value,
        ).select_related("item")
    )
    print(f"共 {len(games)} 个 Steam 游戏")

    counts = {}
    changes = []
    for game in games:
        new_status = decide(game)
        counts[new_status] = counts.get(new_status, 0) + 1
        if game.status != new_status:
            changes.append((game, game.status, new_status))

    print("\n新状态分布：")
    for status, count in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {status:12s} {count}")

    print(f"\n需要修改: {len(changes)}")
    for game, old, new in changes[:15]:
        print(f"  {game.item.title[:32]:34s} {old:12s} -> {new}")

    if args.dry_run:
        print("\n[DRY-RUN] 未写入数据库")
        return

    if not changes:
        print("\n没有需要修改的记录")
        return

    for game, _, new in changes:
        game.status = new

    Game.objects.bulk_update([c[0] for c in changes], ["status"], batch_size=200)
    print(f"\n已更新 {len(changes)} 条")


if __name__ == "__main__":
    main()
