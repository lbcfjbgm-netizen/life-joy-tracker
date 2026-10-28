"""按月（或按天）给每个游戏存一条「累计游玩时长」快照。

背景：本站的 historical 记录是导入时的一次性批量快照，时间戳都是假的，
所以「过去每天玩了多久」数据库里根本没有，事后也补不出来。唯一的办法
是从现在开始，每月（每月版）或每天（--daily 版）把每个游戏的累计时长存一份，
下个月/过几天减掉上一次，就是这段时间玩了多久。

用法：
    python .tools/snapshot_playtime.py                    # 月度，预演（默认）
    python .tools/snapshot_playtime.py --apply            # 月度，真写
    python .tools/snapshot_playtime.py --apply --user admin
    python .tools/snapshot_playtime.py --apply --month 2026-10

    python .tools/snapshot_playtime.py --daily            # 日度，预演
    python .tools/snapshot_playtime.py --daily --apply    # 日度，真写

规则：
    1. 只统计 progress > 0 或 achievements_total > 0 的游戏（玩过的）。
    2. 月度：每个游戏每月只留一条，captured_on 固定当月 1 号。
       日度：每个游戏每天只留一条，captured_on 就是当天（timezone.localdate()）。
       两种都靠唯一约束 + update_or_create 保证重跑不重复（幂等）。
    3. 月度：时长和上一条快照相同就不写（省行数）；第一次跑时全部写，留基线。
       日度：**每天每个玩过的游戏都写一条**，哪怕时长没变 —— 首页要算
       「最近 7 天增量」，缺任何一天都会让那天的差值算不出来。
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = BASE_DIR / "src"
LOG_PATH = BASE_DIR / ".tools" / "logs" / "snapshot_playtime.log"

# Django 的配置从 src 目录起算，先把它放到 sys.path 最前面再初始化。
sys.path.insert(0, str(SRC_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import transaction  # noqa: E402
from django.db.models import OuterRef, Q, Subquery  # noqa: E402
from django.utils import timezone  # noqa: E402

from app.models import Game, PlaytimeDaily, PlaytimeSnapshot  # noqa: E402

logger = logging.getLogger("snapshot_playtime")


def setup_console() -> None:
    """Windows 控制台默认是 GBK 代码页，中文会乱码，这里切成 UTF-8 再输出。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - 输出编码不该拖垮采集
        logger.debug("切换控制台编码失败，输出可能乱码", exc_info=True)


def setup_logging() -> None:
    """日志追加写到 .tools/logs/snapshot_playtime.log，UTF-8。"""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    # 屏幕上只留警告和错误，正常摘要用 print 输出，避免两遍重复。
    stream_handler.setLevel(logging.WARNING)

    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False


def parse_month(value: str | None) -> datetime.date:
    """把 "2026-10" 解析成 2026-10-01；不传就用今天所在月的 1 号。"""
    if not value:
        return timezone.localdate().replace(day=1)
    try:
        parsed = datetime.datetime.strptime(value, "%Y-%m").date()
    except ValueError as exc:
        msg = f"--month 要写成 YYYY-MM，收到 {value!r}"
        raise SystemExit(msg) from exc
    return parsed.replace(day=1)


def collect_rows(user, captured_on: datetime.date) -> list[dict]:
    """算出这个月该写哪些快照，返回待写列表（含动作标记）。"""
    # 每个游戏在「本月之前」最新的那条快照，用子查询取，避免把整张表拉进内存。
    latest_before = PlaytimeSnapshot.objects.filter(
        user=user,
        game_id=OuterRef("pk"),
        captured_on__lt=captured_on,
    ).order_by("-captured_on")

    games = (
        Game.objects.filter(user=user)
        .filter(Q(progress__gt=0) | Q(achievements_total__gt=0))
        .annotate(
            prev_minutes=Subquery(latest_before.values("playtime_minutes")[:1]),
        )
        .values("id", "item__title", "progress", "prev_minutes")
        .order_by("id")
    )

    # 这个月已经写过的，用来保证同月重跑不产生重复记录。
    existing = dict(
        PlaytimeSnapshot.objects.filter(user=user, captured_on=captured_on).values_list(
            "game_id",
            "playtime_minutes",
        ),
    )

    plan = []
    for row in games:
        game_id = row["id"]
        minutes = int(row["progress"] or 0)
        if game_id in existing:
            # 本月已有记录：值一样就算 unchanged，不一样就 update（同月补采）。
            action = "unchanged" if existing[game_id] == minutes else "update"
        elif row["prev_minutes"] == minutes:
            action = "skip"  # 和上一条快照一样，不写，省行数
        else:
            action = "write"  # 首次（基线）或确实变了

        plan.append(
            {
                "game_id": game_id,
                "title": row["item__title"],
                "minutes": minutes,
                "action": action,
            },
        )
    return plan


def run(user, captured_on: datetime.date, *, apply: bool) -> dict:
    """执行采集，返回统计结果。"""
    plan = collect_rows(user, captured_on)

    to_write = [row for row in plan if row["action"] in {"write", "update"}]
    skipped = [row for row in plan if row["action"] == "skip"]
    unchanged = [row for row in plan if row["action"] == "unchanged"]

    written = 0
    if apply and to_write:
        with transaction.atomic():
            for row in to_write:
                PlaytimeSnapshot.objects.update_or_create(
                    user=user,
                    game_id=row["game_id"],
                    captured_on=captured_on,
                    defaults={"playtime_minutes": row["minutes"]},
                )
                written += 1

    mode = "APPLY" if apply else "DRY-RUN"
    logger.info(
        "[%s] user=%s month=%s 总游戏数=%d 待写=%d 跳过(无变化)=%d 已存在同值=%d 实写=%d",
        mode,
        user.username,
        captured_on.strftime("%Y-%m"),
        len(plan),
        len(to_write),
        len(skipped),
        len(unchanged),
        written,
    )

    return {
        "mode": mode,
        "month": captured_on.strftime("%Y-%m"),
        "total_games": len(plan),
        "to_write": len(to_write),
        "skipped": len(skipped),
        "unchanged": len(unchanged),
        "written": written,
    }


def collect_daily_rows(user) -> list[dict]:
    """算出今天该写哪些日快照，返回待写列表。

    和月度版的差别：日度**不过滤「和上次一样」**。首页算的是「今天 - 7 天前」
    这条长跨度的差，中间任何一天缺一行都不影响差值本身，但缺数据会让
    「哪天有基线」变得不确定，所以每天每个玩过的游戏都写一条，最省心。
    """
    games = (
        Game.objects.filter(user=user, progress__gt=0)
        .values("id", "item__title", "progress")
        .order_by("id")
    )
    return [
        {
            "game_id": row["id"],
            "title": row["item__title"],
            "minutes": int(row["progress"] or 0),
        }
        for row in games
    ]


def run_daily(user, captured_on: datetime.date, *, apply: bool) -> dict:
    """执行日度采集（幂等：同一天跑多少次都只有一条）。"""
    plan = collect_daily_rows(user)

    written = 0
    if apply and plan:
        with transaction.atomic():
            for row in plan:
                PlaytimeDaily.objects.update_or_create(
                    user=user,
                    game_id=row["game_id"],
                    captured_on=captured_on,
                    defaults={"playtime_minutes": row["minutes"]},
                )
                written += 1

    mode = "APPLY" if apply else "DRY-RUN"
    logger.info(
        "[%s] user=%s day=%s 有待写游戏=%d 实写=%d",
        mode,
        user.username,
        captured_on.isoformat(),
        len(plan),
        written,
    )

    return {
        "mode": mode,
        "day": captured_on.isoformat(),
        "total_games": len(plan),
        "written": written,
    }


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description="存游戏累计游玩时长快照（默认按月，加 --daily 按天）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写库；不加这个参数就是预演（默认）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预演，不写库（默认行为，显式写出来更清楚）",
    )
    parser.add_argument(
        "--daily",
        action="store_true",
        help="写日快照 PlaytimeDaily（默认写月度 PlaytimeSnapshot）",
    )
    parser.add_argument("--user", default=None, help="只处理这个用户名，默认全部用户")
    parser.add_argument(
        "--month",
        default=None,
        help="月度快照的月份 YYYY-MM，默认当前月（--daily 时忽略）",
    )
    args = parser.parse_args(argv)

    setup_logging()
    setup_console()

    user_model = get_user_model()
    if args.user:
        users = list(user_model.objects.filter(username=args.user))
        if not users:
            logger.error("找不到用户 %s", args.user)
            return 1
    else:
        users = list(user_model.objects.all())

    apply = args.apply and not args.dry_run
    if args.apply and args.dry_run:
        logger.warning("--apply 和 --dry-run 同时给了，按 dry-run 处理")

    if args.daily:
        return _main_daily(users, apply=apply)

    captured_on = parse_month(args.month)

    total_written = 0
    for user in users:
        result = run(user, captured_on, apply=apply)
        total_written += result["written"]
        print(
            f"月份 {result['month']} / 总游戏数 {result['total_games']} / "
            f"本月新写 {result['to_write']} 条 / 跳过(无变化) {result['skipped']} 条 / "
            f"已存在同值 {result['unchanged']} 条 / 实写 {result['written']} 条",
        )

    if not apply:
        print("（预演模式，没有写库。要真写请加 --apply）")
    else:
        print(f"本次共写入 {total_written} 条快照。")
    return 0


def _main_daily(users, *, apply: bool) -> int:
    """--daily 分支：写今天的日快照。"""
    captured_on = timezone.localdate()
    total_written = 0
    for user in users:
        result = run_daily(user, captured_on, apply=apply)
        total_written += result["written"]
        print(
            f"日期 {result['day']} / 有时长的游戏 {result['total_games']} 个 / "
            f"实写 {result['written']} 条",
        )

    if not apply:
        print("（预演模式，没有写库。要真写请加 --apply）")
    else:
        print(f"本次共写入 {total_written} 条日快照。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
