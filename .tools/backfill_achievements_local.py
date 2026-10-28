r"""一次性补数：用 Steam 客户端本地缓存补上「未同步」游戏的成就进度.

背景（已实测，不需要再查）：

* ``ISteamUserStats/GetPlayerAchievements`` 对很多游戏直接返回
  ``HTTP 403 {"error": "Profile is not public"}``，这些游戏在库里就一直
  是 ``achievements_total = 0`` / ``achievements_synced_at = NULL``，
  详情页只能显示「未同步」。
* 但 Steam 客户端本地有这份数据：
  ``<Steam>\\userdata\\<账号 id>\\config\\librarycache\\<appid>.json``，
  里面 ``achievements`` 那一条带 ``nTotal`` / ``nAchieved`` 两个整数。
  已用客户端截图核对过（``3812600`` 25/50、``2967950`` 18/60），完全吻合。

这个脚本只做一件事：把库里 ``achievements_total = 0`` 的 Steam 游戏翻一遍，
本地缓存读得到就补上三个字段，读不到就跳过。

三条硬规矩：

1. 只补 ``achievements_total = 0`` 的。库里有总数的游戏一律不碰：本地缓存的
   总数有时比官方少（实测 ``2218750`` 本地 500 / 官方 666，``1829470``
   12 / 14），拿它覆盖会把数据改小。
2. 写库走 ``Game.objects.bulk_update``，绝不 ``game.save()`` —— ``Game.save()``
   会顺带触发 ``process_status()`` 和 ``fetch_releases()``，两个都会发网络
   请求，在独立脚本里必崩。
3. 只动成就三件套（total / unlocked / synced_at），不动 ``status``：状态联动
   归站点里的同步任务管，这里掺一脚会把「已放弃」之类的状态搅乱。

在项目根目录（``yamtrack``）下用 venv 的 python 跑::

    .venv\\Scripts\\python.exe -X utf8 .tools\\backfill_achievements_local.py --dry-run
    .venv\\Scripts\\python.exe -X utf8 .tools\\backfill_achievements_local.py --limit 50
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.utils import timezone  # noqa: E402

from app.models import Game, MediaTypes, Sources  # noqa: E402
from integrations.imports import steam_achievements_local  # noqa: E402

# 明细最多打这么多条，再多就得翻日志了
DETAIL_LIMIT = 20

UPDATED_FIELDS = [
    "achievements_total",
    "achievements_unlocked",
    "achievements_synced_at",
]


def parse_args(argv=None):
    """Return the command line options of this script."""
    parser = argparse.ArgumentParser(
        description="用 Steam 客户端本地缓存补上未同步游戏的成就进度",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只报告，不写库",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="最多扫描多少个待补游戏，0 = 不限（默认）",
    )
    parser.add_argument(
        "--user",
        default="",
        help="用户名或用户 id，默认取第一个超级用户",
    )
    return parser.parse_args(argv)


def resolve_user(value):
    """Return the user the games belong to, None when it cannot be found."""
    model = get_user_model()

    if value:
        queryset = model.objects.filter(pk=int(value)) if value.isdigit() else None
        if queryset is None:
            queryset = model.objects.filter(username=value)
        return queryset.first()

    return model.objects.filter(is_superuser=True).order_by("id").first()


def pending_games(user, limit=0):
    """Return the Steam games of a user that have no achievement counters yet."""
    games = (
        Game.objects.filter(
            user=user,
            item__source=Sources.STEAM.value,
            item__media_type=MediaTypes.GAME.value,
            achievements_total=0,
        )
        .select_related("item")
        .order_by("item__media_id")
    )

    if limit > 0:
        games = games[:limit]

    return list(games)


def main(argv=None):
    """Run the backfill and print a summary, without ever saving one by one."""
    args = parse_args(argv)

    user = resolve_user(args.user)
    if user is None:
        print(f"找不到用户 {args.user!r}，什么都没做")
        return 1

    cache_dir = steam_achievements_local.librarycache_dir()
    print(f"用户: {user.username} (id={user.pk})")
    print(f"本地缓存目录: {cache_dir or '未找到'}")

    games = pending_games(user, args.limit)
    print(f"待补游戏（achievements_total=0）: {len(games)} 个")

    if not games:
        print("没有待补的游戏，结束")
        return 0

    if cache_dir is None:
        print("本机没找到 Steam 本地缓存，什么都没做")
        return 0

    now = timezone.now()
    to_update = []
    details = []
    scanned = 0
    with_data = 0
    skipped = 0

    for game in games:
        scanned += 1
        appid = game.item.media_id

        counts = steam_achievements_local.read_achievements(appid)
        if counts is None:
            skipped += 1
            continue

        total, unlocked = counts
        with_data += 1

        details.append(
            (
                appid,
                game.item.title,
                game.achievements_total,
                game.achievements_unlocked,
                total,
                unlocked,
            ),
        )

        game.achievements_total = total
        game.achievements_unlocked = unlocked
        game.achievements_synced_at = now
        to_update.append(game)

    if to_update and not args.dry_run:
        Game.objects.bulk_update(to_update, UPDATED_FIELDS)

    print()
    print(f"扫描: {scanned} 个")
    print(f"本地有数据: {with_data} 个")
    print(f"本地也没数据（跳过）: {skipped} 个")
    if args.dry_run:
        print(f"可更新: {len(to_update)} 个（--dry-run，未写库）")
    else:
        print(f"已更新: {len(to_update)} 个")

    if details:
        print()
        print(
            f"前 {min(len(details), DETAIL_LIMIT)} 条明细 (appid / 游戏名 / 旧 → 新):"
        )
        for appid, title, old_total, old_unlocked, total, unlocked in details[
            :DETAIL_LIMIT
        ]:
            print(
                f"  {appid} / {title} / "
                f"{old_total}/{old_unlocked} → {total}/{unlocked}",
            )

    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        # 游戏名有中文，控制台默认编码可能是 GBK
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
