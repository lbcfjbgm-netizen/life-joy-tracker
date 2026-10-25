# -*- coding: utf-8 -*-
"""把「家庭库里有、本库没有、且过了质量门槛」的游戏导入进来。

复用 src/integrations/imports/steam.py 里现成的逻辑，不自己造建表规则：
    primary_cover_url()   —— 封面 URL（含 CDN 兜底与 appid>=300万 的新域名）
    Status.PLANNING       —— 没玩过的游戏状态

输入：.tools/raw/steam_family_reviews.json（fetch_family_reviews.py 产出）
      判定字段 verdict == "import" 的才导入。

规则（用户 2026-10-24 拍板）：
    * 玩过（rt_playtime>0）        -> 导入
    * 没玩过：评论数>=100 且 好评率>=60 -> 导入，status=Planning
    * 其余                        -> 不进库（在 steam_blocklist.json 里拉黑）

安全约束（硬性）：
    * 只用 get_or_create，**已存在的 appid 一律跳过**，绝不覆盖已有 2928 条。
    * 不写 score（保持 NULL）、不碰 users_user、不碰其他表。
    * 分批提交，可断点续跑（已存在的自然跳过）。

用法：
    ..\\.venv\\Scripts\\python.exe .tools\\import_family_games.py --dry-run
    ..\\.venv\\Scripts\\python.exe .tools\\import_family_games.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import transaction  # noqa: E402

from app.models import (  # noqa: E402
    Game,
    Item,
    MediaTypes,
    Sources,
    Status,
)
from integrations.imports.steam import primary_cover_url  # noqa: E402

REVIEWS = ROOT / ".tools" / "raw" / "steam_family_reviews.json"
BLOCK = ROOT / ".tools" / "raw" / "steam_blocklist.json"
MIN_REVIEWS = 100
MIN_PCT = 60


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--batch", type=int, default=200)
    args = ap.parse_args()

    data = json.loads(REVIEWS.read_text(encoding="utf-8"))

    # 读取拉黑清单；但若该 appid 现在有了游玩时长（用户真玩了），自动解除
    block_ids: set[int] = set()
    if BLOCK.exists():
        try:
            bl = json.loads(BLOCK.read_text(encoding="utf-8"))
            block_ids = {int(e["appid"]) for e in bl.get("appids", [])}
        except (OSError, ValueError, KeyError):
            block_ids = set()

    user = get_user_model().objects.filter(is_superuser=True).order_by("id").first()
    if user is None:
        print("找不到用户，退出。")
        return 1

    existing_ids = set(
        Item.objects.filter(
            media_type=MediaTypes.GAME.value, source=Sources.STEAM.value
        ).values_list("media_id", flat=True)
    )
    print(f"库中现有 Steam 游戏条目: {len(existing_ids)}")

    to_import: list[dict] = []
    unblocked: list[int] = []
    skipped_existing = skipped_quality = 0

    for key, rec in data.items():
        appid = str(rec.get("appid"))
        if appid in existing_ids:
            skipped_existing += 1
            continue
        played = int(rec.get("rt_playtime") or 0) > 0
        pct = rec.get("pct")
        cnt = int(rec.get("count") or 0)

        qualifies = played or (pct is not None and cnt >= MIN_REVIEWS and pct >= MIN_PCT)
        if not qualifies:
            skipped_quality += 1
            continue
        if int(rec.get("appid")) in block_ids:
            unblocked.append(int(rec.get("appid")))
        to_import.append(rec)

    print(f"已存在跳过 {skipped_existing}，质量不达标跳过 {skipped_quality}，"
          f"本次导入 {len(to_import)}（其中解除拉黑 {len(unblocked)}）")
    if not args.apply:
        for rec in to_import[:10]:
            print(f"   {rec['appid']:>8} {str(rec.get('name'))[:40]:40} "
                  f"{rec.get('pct')}% ({rec.get('count')} 条)")
        print("\n[dry-run] 未写库。加 --apply 执行。")
        return 0

    created_games = created_items = 0
    with transaction.atomic():
        for start in range(0, len(to_import), args.batch):
            chunk = to_import[start:start + args.batch]
            new_games: list[Game] = []
            for rec in chunk:
                appid = str(rec["appid"])
                name = rec.get("name") or f"Unknown Game {appid}"
                item, item_created = Item.objects.get_or_create(
                    media_id=appid,
                    source=Sources.STEAM.value,
                    media_type=MediaTypes.GAME.value,
                    defaults={"title": name, "image": primary_cover_url(appid)},
                )
                if item_created:
                    created_items += 1
                if Game.objects.filter(item=item, user=user).exists():
                    continue
                # 用 bulk_create 而不是 .create()：Game.save() 里的 process_status()
                # 会调 self.item.fetch_releases(delay=True) 投递 Celery 任务，
                # 命令行下没有 events.tasks 上下文会直接 AttributeError。
                # bulk_create 绕过 save()，与项目里导入器的做法一致。
                new_games.append(
                    Game(
                        item=item,
                        user=user,
                        status=Status.PLANNING.value,
                        score=None,
                        progress=int(rec.get("rt_playtime") or 0),
                        notes="Imported from Steam（家庭共享）",
                    )
                )
            if new_games:
                Game.objects.bulk_create(new_games, batch_size=200)
                created_games += len(new_games)
            print(f"  已导入 {created_games} 条…", flush=True)

    print(f"\n完成：新建条目 {created_items}，新建游戏记录 {created_games}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
