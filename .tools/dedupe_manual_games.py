"""把 4 条「手写 + Steam 版都有」的重复游戏合并成一条，保留 Steam 那条。

规则（用户拍板）：
- 时长 / 状态 / 成就 / 创建时间 一律以 Steam 那条为准，绝不覆盖。
- 只在 Steam 那条为空（score 为 NULL 或 0）时，才把手动那条的打分搬过去。
- notes 用拼接方式合并，不覆盖 Steam 那条已有的内容。
- 标题保持 Steam 原名（不动 app_item.title）。
- 删掉手动那条：先删 history（simple_history），再删 game，最后删 item。

默认 --dry-run 只打印计划；加 --apply 才真删。
删之前会把 4 条 item + game + 历史行全字段导出成 JSON 备份，方便回滚。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from app.models import (  # noqa: E402
    Game,
    HistoricalGame,
    Item,
    PlaytimeDaily,
    PlaytimeSnapshot,
)

# 纯「导入来源」标记，算不上用户自己写的备注，拼过去只会让 Steam 那条看着像来自两个地方。
# 实测这 4 条手写记录的备注全是这一种，所以都跳过不搬。
IMPORT_MARKERS = {"Notion 导入", "Imported from Steam"}

# 手动条目 id -> (Steam 那条的 game id, 说明)
DUPLICATE_GROUPS = [
    (10406, 1928, "点击僵尸 / Zombidle: REMONSTERED"),
    (10407, 2745, "女神异闻录3重制版 / 女神异闻录３ Reload"),
    (10409, 2928, "精神之城：洛菲 / Spirit City: Lofi Sessions"),
    (10411, 2657, "捞女游戏 / 情感反诈模拟器"),
]

BACKUP_DIR = PROJECT_ROOT / ".tools" / "logs"


def _row_snapshot(model_obj) -> dict:
    """把一个模型实例的所有字段拍成可 JSON 化的 dict。"""
    data = {}
    for field in model_obj._meta.concrete_fields:
        value = getattr(model_obj, field.attname)
        if isinstance(value, Decimal):
            value = str(value)
        elif isinstance(value, datetime):
            value = value.isoformat()
        elif hasattr(value, "isoformat"):
            value = value.isoformat()
        elif isinstance(value, (bytes, bytearray)):
            value = value.hex()
        data[field.attname] = value
    return data


def _iter_refs(manual_game: Game, manual_item: Item):
    """列出所有指向这条手动记录的关联行（用于确认删干净、不误伤）。"""
    yield "PlaytimeDaily.game", PlaytimeDaily.objects.filter(game=manual_game).count()
    yield "PlaytimeSnapshot.game", PlaytimeSnapshot.objects.filter(game=manual_game).count()
    history_qs = HistoricalGame.objects.filter(id=manual_game.id)
    yield "HistoricalGame.id", history_qs.count()


def build_plan(verbose: bool = True) -> list[dict]:
    """算出每个重复组的合并计划，返回结构化列表。"""
    plan = []
    for manual_item_id, steam_game_id, label in DUPLICATE_GROUPS:
        entry = {
            "label": label,
            "manual_item_id": manual_item_id,
            "steam_game_id": steam_game_id,
            "ok": False,
            "reasons": [],
            "score_move": None,
            "notes_append": None,
            "refs": [],
        }

        manual_item = Item.objects.filter(id=manual_item_id).first()
        if manual_item is None:
            entry["reasons"].append("手动条目已不存在，跳过（可能之前已删）")
            plan.append(entry)
            continue

        manual_games = list(Game.objects.filter(item=manual_item))
        if not manual_games:
            entry["reasons"].append("手动条目没有 game 行，跳过")
            plan.append(entry)
            continue
        if len(manual_games) > 1:
            entry["reasons"].append(f"手动条目有 {len(manual_games)} 条 game 行，超出预期，跳过")
            plan.append(entry)
            continue
        manual_game = manual_games[0]

        steam_game = Game.objects.filter(id=steam_game_id).select_related("item").first()
        if steam_game is None:
            entry["reasons"].append(f"Steam 那条 game {steam_game_id} 不存在，跳过")
            plan.append(entry)
            continue
        if steam_game.item.source != "steam":
            entry["reasons"].append(f"game {steam_game_id} 不是 steam 来源，跳过")
            plan.append(entry)
            continue

        entry["ok"] = True
        entry["manual_item"] = _row_snapshot(manual_item)
        entry["manual_game"] = _row_snapshot(manual_game)
        entry["steam_game"] = _row_snapshot(steam_game)

        # 打分：只在 Steam 那条为空时搬
        manual_score = manual_game.score
        steam_score = steam_game.score
        if manual_score is not None and (steam_score is None or float(steam_score) == 0.0):
            entry["score_move"] = {"from": float(manual_score), "to_previous": steam_score}
        elif manual_score is not None and steam_score is not None and float(manual_score) != float(steam_score):
            entry["reasons"].append(
                f"两边分数不同（手动 {manual_score} / Steam {steam_score}），按规则保留 Steam 那条",
            )

        # notes：拼接，不覆盖；纯导入标记不搬
        manual_notes = (manual_game.notes or "").strip()
        steam_notes = (steam_game.notes or "").strip()
        if manual_notes in IMPORT_MARKERS:
            entry["notes_skipped"] = manual_notes
        elif manual_notes and manual_notes not in steam_notes:
            entry["notes_append"] = {"append": manual_notes, "steam_current": steam_notes}

        entry["refs"] = list(_iter_refs(manual_game, manual_item))
        entry["steam_refs"] = [
            ("PlaytimeDaily.game", PlaytimeDaily.objects.filter(game=steam_game).count()),
            ("PlaytimeSnapshot.game", PlaytimeSnapshot.objects.filter(game=steam_game).count()),
        ]
        plan.append(entry)
    return plan


def print_plan(plan: list[dict]) -> None:
    for entry in plan:
        print("=" * 72)
        print(f"[合并] {entry['label']}  手动 item {entry['manual_item_id']} -> Steam game {entry['steam_game_id']}")
        if not entry["ok"]:
            print("  状态：跳过")
            for reason in entry["reasons"]:
                print(f"    - {reason}")
            continue

        manual_game = entry["manual_game"]
        steam_game = entry["steam_game"]
        print("  状态：待执行")
        print(
            f"  时长：手动 {manual_game['progress']} 分钟 -> Steam {steam_game['progress']} 分钟"
            f"（保留 Steam）",
        )
        print(
            f"  状态字段：手动 {manual_game['status']} -> Steam {steam_game['status']}（保留 Steam）",
        )
        print(
            f"  成就：手动 {manual_game['achievements_unlocked']}/{manual_game['achievements_total']}"
            f" -> Steam {steam_game['achievements_unlocked']}/{steam_game['achievements_total']}（保留 Steam）",
        )
        if entry["score_move"]:
            print(f"  打分：搬 {entry['score_move']['from']} -> Steam 那条（原来是 {entry['score_move']['to_previous']}）")
        else:
            print(f"  打分：不搬（手动 {manual_game['score']} / Steam {steam_game['score']}）")
        if entry["notes_append"]:
            print(f"  备注：追加「{entry['notes_append']['append']}」到 Steam 那条")
        elif entry.get("notes_skipped"):
            print(f"  备注：不搬（「{entry['notes_skipped']}」只是导入来源标记）")
        else:
            print("  备注：无需追加")
        print("  将要删除的行：")
        for name, count in entry["refs"]:
            flag = "（ORM 会自动连带删除）" if name != "HistoricalGame.id" else "（需先删，simple_history 不自动清）"
            print(f"    {name}: {count} 行 {flag}")
        print("    app_Game: 1 行 / app_Item: 1 行")


def write_backup(plan: list[dict]) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = BACKUP_DIR / f"dedupe_manual_games_backup_{stamp}.json"
    payload = {"created_at": stamp, "groups": []}
    for entry in plan:
        if not entry["ok"]:
            continue
        manual_item_id = entry["manual_item_id"]
        manual_game_id = entry["manual_game"]["id"]
        payload["groups"].append(
            {
                "label": entry["label"],
                "manual_item_id": manual_item_id,
                "manual_game_id": manual_game_id,
                "steam_game_id": entry["steam_game_id"],
                "item": entry["manual_item"],
                "game": entry["manual_game"],
                "history": [_row_snapshot(row) for row in HistoricalGame.objects.filter(id=manual_game_id)],
                "playtime_daily": [
                    _row_snapshot(row) for row in PlaytimeDaily.objects.filter(game_id=manual_game_id)
                ],
                "playtime_snapshot": [
                    _row_snapshot(row) for row in PlaytimeSnapshot.objects.filter(game_id=manual_game_id)
                ],
            },
        )
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def apply_plan(plan: list[dict]) -> None:
    for entry in plan:
        if not entry["ok"]:
            print(f"- 跳过 {entry['label']}：{entry['reasons']}")
            continue

        manual_item_id = entry["manual_item_id"]
        manual_item = Item.objects.filter(id=manual_item_id).first()
        if manual_item is None:
            print(f"- 跳过 {entry['label']}：条目已不存在")
            continue
        manual_game = Game.objects.filter(item=manual_item).first()
        if manual_game is None:
            print(f"- 跳过 {entry['label']}：game 行已不存在")
            continue

        steam_game = Game.objects.get(id=entry["steam_game_id"])

        # 1) 先把能合并的字段合并到 Steam 那条
        update_fields = []
        if entry["score_move"]:
            steam_game.score = entry["score_move"]["from"]
            update_fields.append("score")
        if entry["notes_append"]:
            joiner = "；" if (steam_game.notes or "").strip() else ""
            steam_game.notes = f"{steam_game.notes or ''}{joiner}{entry['notes_append']['append']}"
            update_fields.append("notes")
        if update_fields:
            steam_game.save(update_fields=update_fields)
            print(f"- 已合并到 Steam game {steam_game.id}：{', '.join(update_fields)}")
        else:
            print(f"- Steam game {steam_game.id}：无可合并字段")

        # 2) 再删手动那条：history -> game(含 playtime 连带) -> item
        hist = HistoricalGame.objects.filter(id=manual_game.id)
        hist_count = hist.count()
        # 用 query 删除历史，避免触发 simple_history 再次写历史
        for row in hist:
            row.delete()
        print(f"  删除历史行 {hist_count} 条")

        daily_count = PlaytimeDaily.objects.filter(game=manual_game).count()
        snap_count = PlaytimeSnapshot.objects.filter(game=manual_game).count()
        game_pk = manual_game.pk
        manual_game.delete()
        print(f"  删除 game {game_pk}（连带 PlaytimeDaily {daily_count} 条、PlaytimeSnapshot {snap_count} 条）")

        item_pk = manual_item.pk
        manual_item.delete()
        print(f"  删除 item {item_pk}")


def main() -> int:
    parser = argparse.ArgumentParser(description="合并重复的手动游戏条目")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="真的执行删除")
    group.add_argument("--dry-run", action="store_true", help="只打印计划（默认）")
    args = parser.parse_args()

    plan = build_plan()
    print_plan(plan)

    todo = [entry for entry in plan if entry["ok"]]
    print("=" * 72)
    print(f"计划删除 {len(todo)} 组，跳过 {len(plan) - len(todo)} 组")

    if not args.apply:
        print("（这是 dry-run，什么都没改。确认无误后加 --apply 执行）")
        return 0

    if not todo:
        print("没有可执行的组，退出。")
        return 0

    backup_path = write_backup(plan)
    print(f"备份已写入：{backup_path}")
    apply_plan(plan)

    print("=" * 72)
    remaining = Item.objects.filter(source="manual", media_type="game").order_by("id")
    print(f"剩余手动游戏条目：{remaining.count()} 条")
    for item in remaining:
        print(f"  {item.id} | {item.title} | media_id={item.media_id} | image={item.image[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
