"""Backfill the games played on this machine that the Steam API never reports.

The Steam web API knows the games bought with the account and the ones lent by
a relative, and nothing else. A few titles belong to neither set yet were
played here, and the client's own files are the only place they appear; see
``src/integrations/imports/steam_local.py`` for how those files are read.

This script takes that local list, asks the Store for the official cover and
the Chinese title of each game, and writes the missing items and games into the
database through the importer's own bulk path. That path is used on purpose:
``Game.objects.create()`` runs ``process_status()`` and ``fetch_releases()``
(``src/app/models.py``), which talk to the network and fail in a standalone
script, while ``helpers.bulk_create_media`` writes the same rows directly.

Usage (from the repository root)::

    .venv\\Scripts\\python.exe .tools\\backfill_local_games.py --dry-run
    .venv\\Scripts\\python.exe .tools\\backfill_local_games.py --limit 20
    .venv\\Scripts\\python.exe .tools\\backfill_local_games.py --limit 0
    .venv\\Scripts\\python.exe .tools\\backfill_local_games.py --appids 499170,3812600

``--dry-run`` writes nothing at all: it reports what a real run would do.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# The console of this machine is a GBK code page, so a title holding a
# trademark sign or an emoji would crash the whole run on its first print.
# Reporting must never be the reason a backfill stops.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402

from app.models import Item, MediaTypes, Sources, Status  # noqa: E402
from integrations.imports import (  # noqa: E402
    helpers,
    steam,
    steam_local,
    steam_store,
)

# The two weeks counter the API reports has no counterpart in the local files,
# so a backfilled game is never marked as in progress from this source alone.
# Everything played at least once is paused, which is what the importer derives
# from a played game that was not launched recently.
LOCAL_PLAYED_STATUS = Status.PAUSED.value

NOTES = "Imported from Steam（本地记录）"

# The default run stays small on purpose: the first pass is meant to prove the
# write path on a handful of games before the whole batch goes in.
DEFAULT_LIMIT = 20


def parse_args(argv=None):
    """Return the parsed command line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Backfill the Steam games played on this machine that neither "
            "Steam API endpoint reports."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the games that would be added, write nothing",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"how many games to backfill, longest played first (default {DEFAULT_LIMIT}, 0 for all)",
    )
    parser.add_argument(
        "--appids",
        default="",
        help="comma separated App IDs to backfill, ignoring --limit",
    )
    parser.add_argument(
        "--user",
        default="admin",
        help="username the games are added for (default admin)",
    )
    parser.add_argument(
        "--no-cover-check",
        action="store_true",
        help="skip the cover resolution, store the guessed path only",
    )
    return parser.parse_args(argv)


def _appid(game):
    """Return the App ID of a game dictionary, None when it holds no number."""
    try:
        return int(game["appid"])
    except (KeyError, TypeError, ValueError):
        return None


def exclusion_lists():
    """Return the two sets of App IDs a backfill must never touch.

    The games marked private or hidden in the Steam client are read from the
    client's own file, and the games the operator removed by hand from the
    JSON list. They are read here, next to the selection, so a future caller
    can't forget one of them.
    """
    return steam_local.read_private_appids(), steam.read_excluded()


def select_games(games, appids, limit):
    """Return the games a run must handle, in the order they are handled.

    The explicit App IDs win over the limit: asking for two specific games must
    never silently pick a different two because of a cap.

    The two exclusion lists are applied first, so neither the cap nor an
    explicit ``--appids`` can pull a game back that has to stay out.

    Returns:
        tuple: ``(picked, missing, skipped)``, where ``skipped`` counts the
        games dropped by each list
    """
    private, excluded = exclusion_lists()

    skipped = {"private": 0, "excluded": 0}
    kept = []
    for game in games:
        appid = _appid(game)
        if appid in private:
            skipped["private"] += 1
        elif appid in excluded:
            skipped["excluded"] += 1
        else:
            kept.append(game)

    if appids:
        wanted = {appid.strip() for appid in appids.split(",") if appid.strip()}
        picked = [game for game in kept if str(game["appid"]) in wanted]
        missing = wanted - {str(game["appid"]) for game in picked}
        return picked, missing, skipped

    if limit and limit > 0:
        return kept[:limit], set(), skipped
    return kept, set(), skipped


def describe(appid, title, playtime, image, ok, reason=""):
    """Return the one line report of a game."""
    minutes = f"{playtime} 分钟"
    cover = image[:60] if image else "(无封面)"
    verdict = "成功" if ok else f"失败 {reason}".strip()
    return f"{appid:<9} {title[:34]:<34} {minutes:<12} {cover:<62} {verdict}"


def existing_item(appid):
    """Return the Steam item of an App ID, None when the library has none."""
    return Item.objects.filter(
        media_id=str(appid),
        source=Sources.STEAM.value,
        media_type=MediaTypes.GAME.value,
    ).first()


def placeholder_title(appid):
    """Return the readable stand-in name of a game nothing can name any more."""
    return f"Steam 游戏 {appid}"


def build_item(appid, local_name, info, dry_run):
    """Return the item to attach the game to, created only in a real run.

    The title is the Chinese one when the Store has it, otherwise the local
    English name from the install manifest, otherwise the original name the
    achievement schema still publishes. A game with a name in none of those is
    never written: ``main`` skips it before this is reached, because a row that
    says nothing about a game is worse than no row at all.
    """
    item = existing_item(appid)
    if item is not None:
        return item, False

    title = info["title_zh"] or info["title"] or local_name or placeholder_title(appid)
    title_zh = info["title_zh"] or ""
    title_en = info["title"] or local_name or ""
    image = info["image"] or ""

    if dry_run:
        return None, True

    item, created = Item.objects.get_or_create(
        media_id=str(appid),
        source=Sources.STEAM.value,
        media_type=MediaTypes.GAME.value,
        defaults={
            "title": title,
            "title_zh": title_zh,
            "title_en": title_en,
            "image": image,
            # A plain list is what the JSONField expects; the raw SQL form
            # (a JSON string) is only needed outside the ORM.
            "genres": [],
        },
    )

    if not created:
        # An item the library already had keeps its own data, only the fields
        # it was missing are filled
        changed = []
        for field, value in (
            ("title_zh", title_zh),
            ("title_en", title_en),
            ("image", image),
        ):
            if value and not getattr(item, field):
                setattr(item, field, value)
                changed.append(field)
        if changed:
            item.save(update_fields=changed)

    return item, created


def build_game(user, item, appid, playtime):
    """Return the unsaved game row of a backfilled App ID."""
    from app.models import Game  # noqa: PLC0415 - matches the importer

    return Game(
        item=item,
        user=user,
        status=LOCAL_PLAYED_STATUS,
        score=None,
        progress=playtime,
        notes=NOTES,
        start_date=None,
        end_date=None,
    )


def main(argv=None):
    """Run the backfill, return the process exit code."""
    args = parse_args(argv)

    user_model = get_user_model()
    try:
        user = user_model.objects.get(username=args.user)
    except user_model.DoesNotExist:
        print(f"用户 {args.user} 不存在")
        return 1

    known = set(
        Item.objects.filter(source=Sources.STEAM.value).values_list(
            "media_id",
            flat=True,
        ),
    )
    print(f"库里已有 Steam 条目 {len(known)} 个，用户 {user.username} (id={user.pk})")

    local_games = steam_local.local_only_games(known)
    print(f"本地有游玩记录、库里没有的游戏：{len(local_games)} 个")

    blocklist = steam.read_blocklist()
    local_games = [game for game in local_games if game["appid"] not in blocklist]

    selected, missing, skipped = select_games(local_games, args.appids, args.limit)
    # 私密/隐藏清单和排除表都在 select_games 里过滤, 这里把两边的数量报出来:
    # dry-run 时看到的是「本来会被补录, 但被清单挡住」的个数
    print(
        f"去掉黑名单后剩 {len(local_games)} 个"
        f" (其中私密/隐藏跳过 {skipped['private']} 个, "
        f"排除表跳过 {skipped['excluded']} 个)",
    )
    if missing:
        print(f"以下 appid 不在待补清单里（可能已在库中或没有游玩记录）：{sorted(missing)}")

    print(
        f"本次处理 {len(selected)} 个"
        f"{'（dry-run，不写数据库）' if args.dry_run else ''}",
    )
    print("-" * 150)

    if not selected:
        print("没有要补的游戏")
        return 0

    created_games = []
    skipped = []
    no_cover = []

    for game in selected:
        appid = game["appid"]
        playtime = game["playtime_minutes"]

        if args.no_cover_check:
            info = {
                "title": game["name"],
                "title_zh": "",
                "image": steam_store.guess_cover_url(appid),
                "achievements": 0,
            }
        else:
            info = steam_store.store_info(appid)

        title = info["title_zh"] or info["title"] or game["name"]
        if not title:
            # Removed from the Store for good: no endpoint names it, so there is
            # nothing honest to write about it. Reported, never stored.
            skipped.append(appid)
            print(
                describe(appid, placeholder_title(appid), playtime, "", False,
                         "（跳过：商店已下架且无名字）"),
            )
            continue

        if not info["image"]:
            no_cover.append(appid)

        item, _created = build_item(appid, game["name"], info, args.dry_run)

        if args.dry_run:
            print(describe(appid, title, playtime, info["image"], True, "（将新增）"))
            continue

        if item is None:
            print(describe(appid, title, playtime, info["image"], False, "条目创建失败"))
            continue

        # The library may already hold this game for another user, or the
        # previous run may have been interrupted after the item was written
        from app.models import Game  # noqa: PLC0415 - matches the importer

        if Game.objects.filter(item=item, user=user).exists():
            print(describe(appid, item.title, playtime, info["image"], True, "（已存在，跳过）"))
            continue

        created_games.append(build_game(user, item, appid, playtime))
        print(describe(appid, item.title, playtime, info["image"], True, "（待写入）"))

    print("-" * 150)
    print(f"能补 {len(selected) - len(skipped)} 个，跳过 {len(skipped)} 个（商店已下架且无名字）")
    if skipped:
        print(f"  跳过的 appid: {skipped}")
    if no_cover:
        print(f"有名字但没有封面 {len(no_cover)} 个（照补，封面留空）: {no_cover}")

    if args.dry_run:
        print(f"dry-run 完成：将补录 {len(selected) - len(skipped)} 个游戏，数据库未改动")
        return 0

    if not created_games:
        print("没有需要写入的游戏")
        return 0

    # The importer's own bulk path, which writes the rows without calling
    # save() and so never triggers the network hooks a plain create() runs
    helpers.bulk_create_media(
        {MediaTypes.GAME.value: created_games},
        user,
    )

    print("-" * 150)
    print(f"已写入 {len(created_games)} 个游戏")
    return 0


if __name__ == "__main__":
    sys.exit(main())
