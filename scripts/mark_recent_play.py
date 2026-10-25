"""把「最近两周玩过」的 Steam 游戏自动标成「进行中」。

数据来源：Steam 的 GetRecentlyPlayedGames 接口（只看 playtime_2weeks > 0）。
修改范围（用户拍板）：
  当前状态 == 已暂停 / 计划中  → 进行中
  当前状态 == 已完成          → 一律不动（全成就的游戏标成进行中是错的）
  库里没有的游戏              → 跳过，只记下来

用法（在 src 目录）：
    ../.venv/Scripts/python.exe ../scripts/mark_recent_play.py --dry-run
    ../.venv/Scripts/python.exe ../scripts/mark_recent_play.py

日志追加写入 yamtrack/.tools/logs/recent_play.log（计划任务跑的时候看不到输出）。
"""

import argparse
import os
import sys
import time
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

import requests  # noqa: E402
from django.conf import settings  # noqa: E402

from app.models import Game, MediaTypes, Sources, Status  # noqa: E402

API_URL = "https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/"
LOG_PATH = Path(__file__).resolve().parent.parent / ".tools" / "logs" / "recent_play.log"

# 只有这两个状态会被改，Completed 绝不动
SOURCE_STATUSES = {
    Status.PAUSED.value,
    Status.PLANNING.value,
}

# 日志文案，纯显示用，不参与任何判断逻辑
STATUS_CN = {
    Status.COMPLETED.value: "已通关",
    Status.IN_PROGRESS.value: "进行中",
    Status.PLANNING.value: "已暂停",  # 界面上「计划中」并进「已暂停」一档
    Status.PAUSED.value: "已暂停",
    Status.DROPPED.value: "已放弃",
}


class Tee:
    """把输出同时写到屏幕和日志文件。"""

    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, text):
        for stream in self.streams:
            try:
                stream.write(text)
                stream.flush()
            except (OSError, ValueError):
                pass

    def flush(self):
        for stream in self.streams:
            try:
                stream.flush()
            except (OSError, ValueError):
                pass


def cn_status(value):
    """Return the Chinese label of a status value."""
    return STATUS_CN.get(value, value)


def fetch_recent(appid_filter=None):
    """Return the list of recently played games from the Steam API."""
    if not settings.STEAM_API_KEY or not settings.STEAM_ID:
        raise RuntimeError("settings 里缺 STEAM_API_KEY 或 STEAM_ID")

    # trust_env=False：直连，不走系统/环境变量里的代理
    session = requests.Session()
    session.trust_env = False

    response = session.get(
        API_URL,
        params={
            "key": settings.STEAM_API_KEY,
            "steamid": settings.STEAM_ID,
            "count": 0,
        },
        timeout=30,
    )
    response.raise_for_status()
    body = response.json().get("response", {})
    games = body.get("games", []) or []

    recent = []
    for entry in games:
        minutes = int(entry.get("playtime_2weeks") or 0)
        if minutes <= 0:
            continue
        recent.append(
            {
                "appid": str(entry.get("appid")),
                "name": entry.get("name") or f"appid {entry.get('appid')}",
                "minutes": minutes,
            },
        )

    if appid_filter:
        recent = [g for g in recent if g["appid"] in appid_filter]
    return recent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印不写入")
    parser.add_argument("--user", default="admin", help="用户名")
    parser.add_argument(
        "--appid",
        action="append",
        default=None,
        help="只处理指定 appid（可重复，调试用）",
    )
    args = parser.parse_args()

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
    real_stdout = getattr(sys, "stdout", None)
    if real_stdout is not None and hasattr(real_stdout, "reconfigure"):
        try:
            real_stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    sys.stdout = Tee(real_stdout, log_file)

    print("\n" + "=" * 60)
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} mark_recent_play 开始")
    print(f"用户: {args.user}  模式: {'DRY-RUN（不写库）' if args.dry_run else '正式写入'}")

    try:
        recent = fetch_recent(set(args.appid) if args.appid else None)
    except Exception as exc:  # noqa: BLE001 - 计划任务只需要一个非零退出码
        print(f"[错误] 调用 Steam 接口失败: {exc}")
        log_file.close()
        return 1

    print(f"最近两周玩过: {len(recent)} 个")
    if not recent:
        print("接口没返回任何最近玩过的游戏，本次无需处理")
        log_file.close()
        return 0

    # appid -> 对应的 Game 记录（同一 appid 只应有一条）
    appids = [g["appid"] for g in recent]
    games = list(
        Game.objects.filter(
            user__username=args.user,
            item__media_id__in=appids,
            item__source=Sources.STEAM.value,
            item__media_type=MediaTypes.GAME.value,
        ).select_related("item"),
    )
    by_appid = {}
    for game in games:
        by_appid.setdefault(str(game.item.media_id), game)

    changes = []
    already = []
    completed = []
    missing = []

    for entry in recent:
        game = by_appid.get(entry["appid"])
        hours = entry["minutes"] / 60
        head = f"{entry['name']}（appid {entry['appid']}）两周 {entry['minutes']} 分钟 ≈ {hours:.1f} 小时"

        if game is None:
            missing.append(head)
            print(f"  [跳过] {head} —— 库里没有这个游戏")
            continue

        if game.status == Status.COMPLETED.value:
            completed.append(head)
            print(f"  [保留] {head} —— 已完成，不动")
            continue

        if game.status == Status.IN_PROGRESS.value:
            already.append(head)
            print(f"  [已是] {head} —— 已经是进行中")
            continue

        if game.status not in SOURCE_STATUSES:
            missing.append(head)
            print(f"  [跳过] {head} —— 当前状态是 {cn_status(game.status)}，不在自动范围内")
            continue

        changes.append((game, entry))
        print(
            f"  [改动] {head} —— {cn_status(game.status)} -> "
            f"{cn_status(Status.IN_PROGRESS.value)}",
        )

    print(
        f"\n汇总: 待改 {len(changes)} / 已是进行中 {len(already)} / "
        f"已完成不动 {len(completed)} / 跳过 {len(missing)}",
    )

    if args.dry_run:
        print("[DRY-RUN] 未写入数据库")
        log_file.close()
        return 0

    if not changes:
        print("没有需要修改的记录")
        log_file.close()
        return 0

    for game, _ in changes:
        game.status = Status.IN_PROGRESS.value

    Game.objects.bulk_update([c[0] for c in changes], ["status"], batch_size=200)
    print(f"已更新 {len(changes)} 条为进行中")

    log_file.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
