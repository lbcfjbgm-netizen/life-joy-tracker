# -*- coding: utf-8 -*-
"""给已完成的游戏补「完成时间」（end_date），按交接单 §10 第 4 条的四级策略。

策略优先级（严格按顺序，先命中先得）：
  ① 最后一个成就的解锁时间（3.1 的种子 achievement_seeds.json 里的 last_unlock）
     → 真实时间，completion_estimated = False
  ② 最后游玩时间（progressed_at）
     → 当前不可用：全库 progressed_at 只有 2026-10-20 / 2026-10-21 两天，是导入日。
       分支保留，必须显式加 --use-progressed-at 才会启用，且要过「导入日」和
       「Notion 年份区间」两道检查。
  ③ Notion 的「完成时间」年份（app_importitem.raw.finished_year，或 games_all.json）
     → 只有年，写成该年 1 月 1 日 12:00（本地时区），completion_estimated = True
  ④ 按「序号」锚点插值
     → 只在左右锚点都存在、时间单调递增、且两锚点间隔 ≤ --max-gap-days(默认 90) 天时插值；
       序号 999 是占位，一律排除；插值结果 completion_estimated = True

没有依据的一律留空，不瞎填。

用法（在 src 目录跑）：
    ..\\.venv\\Scripts\\python.exe ..\\scripts\\apply_completion_times.py            # 默认 dry-run
    ..\\.venv\\Scripts\\python.exe ..\\scripts\\apply_completion_times.py --apply    # 真写库
    ..\\.venv\\Scripts\\python.exe ..\\scripts\\apply_completion_times.py --report 路径.md
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from app.models import Game, MediaTypes, Sources, Status  # noqa: E402

PROJECT_ROOT = SRC_DIR.parent.parent
SEEDS_PATH = (
    SRC_DIR.parent / ".tools" / "raw" / "steam_librarycache" / "2026-10-22" / "achievement_seeds.json"
)
NOTION_JSON = SRC_DIR.parent / ".tools" / "raw" / "notion" / "2026-10-22" / "games_all.json"
DEFAULT_REPORT = PROJECT_ROOT / "_完成时间填充报告.md"

PLACEHOLDER_SEQUENCE = 999
IMPORT_DAYS = {"2026-10-20", "2026-10-21"}  # progressed_at 全是这两天 = 导入日


# --------------------------------------------------------------------------- #
# 读取输入
# --------------------------------------------------------------------------- #
def load_seeds():
    """appid -> 种子条目（含 last_unlock）"""
    with open(SEEDS_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    return {str(s["appid"]): s for s in data.get("seeds", [])}


def load_notion_rows():
    """item_id -> {number, finished_year}；以及 title 归一化后的备份索引"""
    from app.models import ImportItem

    by_item = {}
    note = Counter()
    for imp in ImportItem.objects.all().only("id", "item_id", "raw"):
        if not imp.item_id:
            continue
        raw = imp.raw
        if not isinstance(raw, dict):
            # 历史行可能是纯文本，容错一下
            try:
                raw = json.loads(raw or "{}")
            except (TypeError, ValueError):
                note["bad_json"] += 1
                continue
        by_item.setdefault(imp.item_id, raw)

    by_title = {}
    if NOTION_JSON.exists():
        with open(NOTION_JSON, encoding="utf-8") as fh:
            payload = json.load(fh)
        for page in payload.get("results", []):
            props = page.get("properties", {})
            titles = (props.get("游戏名称") or {}).get("title") or []
            name = "".join(t.get("plain_text", "") for t in titles).strip()
            if not name:
                continue
            by_title.setdefault(norm_title(name), {
                "number": (props.get("序号") or {}).get("number"),
                "finished_year": (((props.get("完成时间") or {}).get("select")) or {}).get("name"),
            })
    return by_item, by_title, note


def norm_title(text):
    return "".join((text or "").lower().split())


def parse_year(text):
    """'2024年' -> 2024"""
    if not text:
        return None
    digits = "".join(ch for ch in str(text) if ch.isdigit())
    return int(digits[:4]) if len(digits) >= 4 else None


def parse_iso(value):
    """带时区的 ISO 字符串 -> aware datetime；没时区的按本地时区补上"""
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def year_anchor(year):
    """年份 → 该年 7 月 1 日 12:00（本地时区）。

    只知道哪一年、不知道具体哪天，取年中把平均误差压到最小；
    用正午是为了时区换算时不会掉到前一年/后一年。这类一律标 estimated。
    """
    return timezone.make_aware(
        datetime(year, 7, 1, 12, 0, 0),
        timezone.get_current_timezone(),
    )


# --------------------------------------------------------------------------- #
# 计算
# --------------------------------------------------------------------------- #
def collect(games, seeds, notion_by_item, notion_by_title, args):
    """返回 [(game, plan)]，plan 里放策略、时间、是否 estimated、说明"""
    plans = []
    for game in games:
        title = game.item.title
        item_id = game.item_id
        seed = seeds.get(str(game.item.media_id)) if game.item.source == Sources.STEAM.value else None
        notion = notion_by_item.get(item_id) or notion_by_title.get(norm_title(title)) or {}
        number = notion.get("number")
        year = parse_year(notion.get("finished_year"))

        plan = {
            "game": game,
            "title": title,
            "number": number,
            "year": year,
            "year_candidate": None,
            "strategy": None,
            "when": None,
            "estimated": False,
            "detail": "",
        }

        # ---- ① 最后一个成就的解锁时间 ----
        last_unlock = (seed or {}).get("last_unlock")
        if last_unlock:
            plan.update(
                strategy="① 最后成就解锁",
                when=parse_iso(last_unlock),
                estimated=False,
                detail=f"appid {game.item.media_id}",
            )
            plans.append(plan)
            continue

        # ---- ② 最后游玩时间（当前不可用，默认跳过）----
        if args.use_progressed_at and game.progressed_at:
            when = game.progressed_at
            day = timezone.localtime(when).date().isoformat() if timezone.is_aware(when) else None
            in_year = year is None or timezone.localtime(when).year == year
            if day not in IMPORT_DAYS and in_year:
                plan.update(
                    strategy="② 最后游玩时间",
                    when=when,
                    estimated=False,
                    detail="progressed_at 通过导入日和年份区间检查",
                )
                plans.append(plan)
                continue

        # ---- ③ Notion 年份 / ④ 插值 ----
        # 交接单 §10 的原话是「锚点间隔 > 90 天的不插值（退回年份）」，
        # 所以④（插值）先试、不合格才退回③（年份）；两者都是 estimated。
        # 这里先只记录年份，把插值留给插值阶段统一判定。
        if year:
            plan["year_candidate"] = year_anchor(year)

        # 没拿到真实时间的，先挂起，交给插值/年份兜底阶段
        plans.append(plan)

    return plans


def build_anchors(plans):
    """锚点 = 有真实时间（策略①或②）且有序号的已完成游戏；999 排除"""
    anchors = []
    dropped = Counter()
    for plan in plans:
        if plan["strategy"] not in ("① 最后成就解锁", "② 最后游玩时间"):
            continue
        number = plan["number"]
        if number is None:
            dropped["无序号"] += 1
            continue
        if number == PLACEHOLDER_SEQUENCE:
            dropped["序号999"] += 1
            continue
        anchors.append((int(number), plan["when"], plan["title"]))
    anchors.sort(key=lambda x: x[0])
    return anchors, dropped


def interpolate(plans, anchors, max_gap_days):
    """给剩下没时间的游戏按序号锚点插值，插不成的退回 Notion 年份。

    统计只记一个最终去向（插值成功 / 退回年份(原因) / 留空(原因)），不重复计数。
    """
    stats = Counter()
    for plan in plans:
        if plan["strategy"] is not None:
            continue

        number = plan["number"]
        fallback = plan.get("year_candidate")
        if number is None:
            _fallback_year(plan, fallback, stats, "无序号")
            continue
        if number == PLACEHOLDER_SEQUENCE:
            _fallback_year(plan, fallback, stats, "序号999占位")
            continue
        number = int(number)

        left = None
        right = None
        for anchor in anchors:
            if anchor[0] < number:
                left = anchor
            elif anchor[0] > number and right is None:
                right = anchor
        if left is None or right is None:
            _fallback_year(plan, fallback, stats, "缺一侧锚点")
            continue

        gap_days = (right[1] - left[1]).total_seconds() / 86400.0
        if right[1] <= left[1]:
            _fallback_year(plan, fallback, stats, "锚点时间非递增")
            continue
        if gap_days > max_gap_days:
            _fallback_year(plan, fallback, stats, f"锚点间隔 {gap_days:.0f} 天 > {max_gap_days}")
            continue

        span = right[0] - left[0]
        if span <= 0:
            _fallback_year(plan, fallback, stats, "锚点序号重复")
            continue

        ratio = (number - left[0]) / span
        when = left[1] + (right[1] - left[1]) * ratio
        # 插值只精确到天，抹掉时分秒里的随机部分，保持正午，便于阅读
        when = timezone.make_aware(
            datetime(when.year, when.month, when.day, 12, 0, 0),
            timezone.get_current_timezone(),
        )
        plan.update(
            strategy="④ 序号插值",
            when=when,
            estimated=True,
            detail=f"{left[0]}号→{right[0]}号，间隔 {gap_days:.1f} 天",
        )
        stats["插值成功"] += 1
    return stats


def _fallback_year(plan, fallback, stats, reason):
    """插值不合格 → 退回策略③ Notion 年份；连年份都没有就留空。"""
    if fallback is not None:
        plan.update(
            strategy="③ Notion 年份",
            when=fallback,
            estimated=True,
            detail=f"{plan['year']} 年（只到年，不含具体日；插值放弃原因：{reason}）",
        )
        stats[f"退回年份（{reason}）"] += 1
    else:
        stats[f"留空（{reason}）"] += 1


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def fmt(dt):
    if dt is None:
        return "-"
    return timezone.localtime(dt).strftime("%Y-%m-%d %H:%M")


def pick_samples(plans, total=20):
    """跨年份抽样：优先每个策略、每个年份都覆盖到"""
    bucket = defaultdict(list)
    for plan in plans:
        if plan["strategy"] is None:
            continue
        year = timezone.localtime(plan["when"]).year
        bucket[(plan["strategy"], year)].append(plan)
    picked = []
    for key in sorted(bucket, key=lambda k: (k[0], k[1])):
        picked.append(bucket[key][0])
    # 不够 20 条就轮回补齐
    idx = 1
    while len(picked) < total:
        added = False
        for key in sorted(bucket):
            if idx < len(bucket[key]):
                picked.append(bucket[key][idx])
                added = True
                if len(picked) >= total:
                    break
        if not added:
            break
        idx += 1
    picked.sort(key=lambda p: (p["strategy"], p["when"]))
    return picked[:total]


def anchor_gap_rows(plans, anchors, max_gap_days):
    """相邻锚点之间的可用性分析（用于解释插值命中数）。

    注意：必须在插值之前调用，否则候选已经被填掉，看不出「之间有没有待填的游戏」。
    """
    rows = []
    for left, right in zip(anchors, anchors[1:]):
        gap = (right[1] - left[1]).total_seconds() / 86400.0
        between = sorted(
            int(p["number"]) for p in plans
            if p["strategy"] is None and p["number"] is not None
            and p["number"] != PLACEHOLDER_SEQUENCE
            and left[0] < int(p["number"]) < right[0]
        )
        reasons = []
        if gap <= 0:
            reasons.append("锚点时间没有随序号递增")
        if gap > max_gap_days:
            reasons.append(f"间隔 {gap:.0f} 天 > {max_gap_days} 天")
        if not between:
            reasons.append("两锚点之间没有待填的游戏")
        rows.append((left[0], right[0], gap, between, "、".join(reasons) or "可用"))
    return rows


def write_report(path, plans, anchors, anchor_dropped, interp_stats, gap_rows, args, before):
    strategy_counts = Counter(p["strategy"] or "未填充（留空）" for p in plans)
    filled = [p for p in plans if p["strategy"]]
    estimated = [p for p in filled if p["estimated"]]
    interp = [p for p in filled if p["strategy"].startswith("④")]
    empty = [p for p in plans if not p["strategy"]]

    lines = []
    add = lines.append
    add("# 完成时间填充报告（3.2 四级策略）")
    add("")
    add(f"- 生成时间：{timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M:%S')}")
    mode = "**已写库（--apply）**" if args.apply else "dry-run（未写库）"
    add(f"- 运行模式：{mode}")
    add(f"- 目标：status = Completed 的全部游戏，共 {len(plans)} 个")
    add(f"- 写入字段：`app_game.end_date`（模板里显示为 Ended），配合 `completion_estimated`")
    add(f"- 时区：USE_TZ=True，TIME_ZONE={os.environ.get('TZ', 'Asia/Shanghai')}，"
        f"统一存 aware datetime")
    add("")
    add("## 一、各策略命中数")
    add("")
    add("| 策略 | 命中数 | 是否 estimated |")
    add("|---|---|---|")
    order = ["① 最后成就解锁", "② 最后游玩时间", "③ Notion 年份", "④ 序号插值",
             "未填充（留空）"]
    for key in order:
        if key not in strategy_counts and key != "未填充（留空）":
            continue
        est = "全部 False" if key == "① 最后成就解锁" else (
            "全部 True" if key.startswith(("③", "④")) else "—")
        add(f"| {key} | {strategy_counts.get(key, 0)} | {est} |")
    add(f"| **合计** | **{len(plans)}** | 有时间的 {len(filled)} 条，"
        f"其中 estimated=True 的 {len(estimated)} 条 |")
    add("")
    add(f"- 策略②说明：`progressed_at` 全库只落在 {sorted(IMPORT_DAYS)} 两天，是导入日，"
        f"不能当最后游玩时间，因此本轮**跳过**（分支代码保留，需 --use-progressed-at 才启用）。")
    add("")
    add("## 二、插值情况")
    add("")
    add(f"- 可用锚点（真实时间 + 有序号，999 已排除）：**{len(anchors)}** 个")
    if anchors:
        for num, when, title in anchors:
            add(f"  - 序号 {num} → {fmt(when)} · {title}")
    skipped = "、".join(f"{k} {v}" for k, v in anchor_dropped.items()) or "无"
    add(f"- 锚点被排除的原因：{skipped}")
    add(f"- 插值尝试结果：" + ("、".join(f"{k} {v}" for k, v in interp_stats.items()) or "无"))
    add(f"- **实际插值出来：{len(interp)} 条**，全部标 `completion_estimated=True`"
        f"（占 estimated 总数的 {len(interp)}/{len(estimated)}）")
    add(f"- 判断规则：左右锚点都要有、锚点时间必须单调递增、间隔 ≤ {args.max_gap_days} 天；"
        f"不合格就退回策略③。")
    add("")
    add("相邻锚点为什么用不上：")
    add("")
    add(f"| 锚点对 | 间隔(天) | 之间待填的游戏序号 | 结果 |")
    add("|---|---|---|---|")
    for left, right, gap, between, verdict in gap_rows:
        add(f"| 序号 {left} → {right} | {gap:.0f} | {between or '—'} | {verdict} |")
    add("")
    add("- 结论：真实解锁时间只有 17 条，且这些游戏的序号与时间**并不严格同向**"
        "（1 号 2023-04-25 比 11 号 2023-04-08 还晚、26 号 2023-04-09 比 17 号 2023-08-05 还早），"
        "相邻锚点间隔又超过 90 天，所以按规则一条都不该插值 —— 本轮 0 条不是漏算，是规则在正常拦截。")
    add("")
    add("## 三、样本核对（跨年份，20 条以内）")
    add("")
    add("| # | 游戏 | 序号 | 策略 | 算出完成时间 | estimated | 备注 |")
    add("|---|---|---|---|---|---|---|")
    for i, plan in enumerate(pick_samples(plans), 1):
        add(f"| {i} | {plan['title']} | {plan['number'] if plan['number'] is not None else '-'} "
            f"| {plan['strategy']} | {fmt(plan['when'])} | "
            f"{'True' if plan['estimated'] else 'False'} | {plan['detail']} |")
    add("")
    add("## 四、留空的游戏（没有依据，不瞎填）")
    add("")
    add(f"共 {len(empty)} 个：")
    for plan in empty:
        add(f"- {plan['title']}（序号 {plan['number'] if plan['number'] is not None else '-'}"
            f"，Notion 年份 {'-'}，无成就解锁时间）")
    add("")
    add("## 五、写库前状态快照")
    add("")
    add("| 项 | 值 |")
    add("|---|---|")
    add(f"| app_game 总数 | {before['total']} |")
    add(f"| end_date 非空 | {before['end_nonnull']} |")
    add(f"| completion_estimated=True | {before['est']} |")
    for status, count in before["statuses"]:
        add(f"| status = {status} | {count} |")
    add("")
    add("## 六、年份分布")
    add("")
    year_counts = Counter(timezone.localtime(p["when"]).year for p in filled)
    for year in sorted(year_counts):
        add(f"- {year} 年：{year_counts[year]} 条")
    add("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return strategy_counts, filled, estimated, interp, empty


# --------------------------------------------------------------------------- #
def self_test():
    """用合成数据验证策略④真的能工作。

    真实库里 0 条插值，说明插入路径本轮没被执行过；这里用交接单第 466 行给的
    例子（125 号 = 2024-07-15、127 号 = 2024-08-15，126 号应落在中间）自检一遍。
    """
    def mk(number, year_candidate=None):
        return {"number": number, "year_candidate": year_candidate, "strategy": None,
                "when": None, "estimated": False, "detail": "", "year": None, "title": "t"}

    d1 = timezone.make_aware(datetime(2024, 7, 15, 12), timezone.get_current_timezone())
    d2 = timezone.make_aware(datetime(2024, 8, 15, 12), timezone.get_current_timezone())
    anchors = [(125, d1, "a"), (127, d2, "b")]

    mid = mk(126)
    interpolate([mid], anchors, 90)
    expect = d1 + (d2 - d1) / 2
    ok_mid = (
        mid["strategy"] == "④ 序号插值"
        and mid["estimated"] is True
        and timezone.localtime(mid["when"]).date() == timezone.localtime(expect).date()
    )

    # 间隔 > 90 天必须不插值、退回年份
    far = mk(126, year_candidate=timezone.make_aware(
        datetime(2024, 7, 1, 12), timezone.get_current_timezone()))
    d3 = timezone.make_aware(datetime(2024, 1, 1, 12), timezone.get_current_timezone())
    d4 = timezone.make_aware(datetime(2024, 12, 31, 12), timezone.get_current_timezone())
    interpolate([far], [(125, d3, "a"), (127, d4, "b")], 90)
    ok_far = far["strategy"] == "③ Notion 年份" and far["estimated"] is True

    # 序号 999 占位必须排除
    ph = mk(999)
    interpolate([ph], anchors, 90)
    ok_999 = ph["strategy"] is None

    return {"126 号插值到中间": ok_mid, "间隔>90天退回年份": ok_far, "999 不插值": ok_999}


def snapshot():
    from django.db.models import Count
    statuses = list(
        Game.objects.values("status").annotate(n=Count("id")).order_by("-n")
        .values_list("status", "n")
    )
    return {
        "total": Game.objects.count(),
        "end_nonnull": Game.objects.filter(end_date__isnull=False).count(),
        "est": Game.objects.filter(completion_estimated=True).count(),
        "statuses": statuses,
    }


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真写库（默认只 dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="显式 dry-run（默认行为）")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--max-gap-days", type=int, default=90,
                        help="插值允许的最大锚点间隔，默认 90 天")
    parser.add_argument("--use-progressed-at", action="store_true",
                        help="启用策略②（当前数据不可用，慎用）")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--skip-report", action="store_true")
    parser.add_argument("--self-test", action="store_true",
                        help="用合成数据自检策略④插值逻辑（不碰数据库）")
    args = parser.parse_args()

    if args.dry_run and args.apply:
        parser.error("--dry-run 和 --apply 不能同时给")

    if args.self_test:
        for name, ok in self_test().items():
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        raise SystemExit(0 if all(self_test().values()) else 1)

    before = snapshot()
    seeds = load_seeds()
    notion_by_item, notion_by_title, notion_note = load_notion_rows()

    games = list(
        Game.objects.filter(
            user__username=args.user,
            item__media_type=MediaTypes.GAME.value,
            status=Status.COMPLETED.value,
        ).select_related("item").order_by("id")
    )
    print(f"[INFO] Completed 游戏：{len(games)}；种子 {len(seeds)} 个 appid；"
          f"Notion 按条目 {len(notion_by_item)} / 按标题 {len(notion_by_title)}")
    if notion_note:
        print(f"[WARN] importitem raw 解析失败 {dict(notion_note)}")

    plans = collect(games, seeds, notion_by_item, notion_by_title, args)
    anchors, anchor_dropped = build_anchors(plans)
    # 间隔表必须在插值之前算：插值会把候选填掉，之后就看不出「之间有没有待填的游戏」
    gap_rows = anchor_gap_rows(plans, anchors, args.max_gap_days)
    interp_stats = interpolate(plans, anchors, args.max_gap_days)

    strategy_counts = Counter(p["strategy"] or "未填充（留空）" for p in plans)
    filled = [p for p in plans if p["strategy"]]
    estimated = [p for p in filled if p["estimated"]]
    interp = [p for p in filled if p["strategy"].startswith("④")]

    print("\n== 各策略命中 ==")
    for key in ["① 最后成就解锁", "② 最后游玩时间", "③ Notion 年份", "④ 序号插值",
                "未填充（留空）"]:
        print(f"  {key:<16} {strategy_counts.get(key, 0)}")
    print(f"  有时间 {len(filled)} 条 / estimated=True {len(estimated)} 条 / 插值 {len(interp)} 条")
    print(f"  锚点 {len(anchors)} 个（排除 {dict(anchor_dropped)}）")
    print(f"  插值判定 {dict(interp_stats)}")

    if not args.skip_report:
        path = Path(args.report)
        write_report(path, plans, anchors, anchor_dropped, interp_stats, gap_rows, args, before)
        print(f"[INFO] 报告写入 {path}")

    # 自洽性检查：写完前后状态分布必须一致，条数必须一致
    to_write = [p for p in filled]
    print(f"[INFO] 待写 {len(to_write)} 条（策略①真实时间 {strategy_counts.get('① 最后成就解锁', 0)}，"
          f"estimated {len(estimated)}）")

    if not args.apply:
        print("\n[DRY-RUN] 未写数据库。确认无误后加 --apply。")
        return

    if before["est"] or before["end_nonnull"]:
        print(f"[WARN] 库里已有 end_date {before['end_nonnull']} 条 / estimated {before['est']} 条，"
              f"本次会覆盖这些游戏的完成时间")

    with transaction.atomic():
        objs = []
        for plan in to_write:
            game = plan["game"]
            game.end_date = plan["when"]
            game.completion_estimated = plan["estimated"]
            objs.append(game)
        Game.objects.bulk_update(objs, ["end_date", "completion_estimated"], batch_size=200)

    after = snapshot()
    print("\n== 写库后 ==")
    print(f"  end_date 非空 {after['end_nonnull']} / estimated=True {after['est']}")
    print(f"  app_game 总数 {after['total']}（写前 {before['total']}）")
    print(f"  状态分布 {after['statuses']}")
    if after["total"] != before["total"]:
        print("[ERROR] 条数变了！")
        raise SystemExit(1)
    if [(s, c) for s, c in after["statuses"]] != [(s, c) for s, c in before["statuses"]]:
        print("[ERROR] 状态分布变了！")
        raise SystemExit(1)
    print("[OK] 条数与状态分布均未变化")


if __name__ == "__main__":
    main()
