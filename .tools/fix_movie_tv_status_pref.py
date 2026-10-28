#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把电影 / 剧集 / 季三个库里「存坏的状态偏好」清成 All（全部）。

为什么会有坏值
--------------
列表页的视图会把 URL 里的 ``?status=...`` **永久写进用户表**的
``movie_status`` / ``tv_status`` / ``season_status``。只要历史上有人（或某个
链接、某次误点）访问过一次 ``/admin/movie?status=Dropped``，这三个字段就会
一直停在 Dropped / In progress / Paused / Planning。之后从顶栏裸点「电影」
（链接不带任何参数）时，视图读到的是库里那个坏值 → 按「已放弃」去筛 →
476 部电影全被筛掉 → 用户看到**空白页**，地址栏还带着 ``status=Dropped``。

这三类媒体的数据全部来自豆瓣，豆瓣那边只有「已看过」一种标记，库里
Movie / TV / Season 每一行的 status 都是 Completed，所以「在看 / 已搁置 /
已放弃 / 待看」这几档永远筛不出任何东西，纯属雷。

脚本做什么
----------
把这三个字段里属于「雷」的值改成 ``All``（不筛），其余值（含 Completed）不动。
改完再进列表页就是全量，地址栏也不会再冒出 ``status=Dropped``。

用法（在 yamtrack 目录下）::

    # 先干跑，只看要改什么，不动库
    .venv\\Scripts\\python.exe .tools\\fix_movie_tv_status_pref.py --dry-run

    # 确认没问题再落库
    .venv\\Scripts\\python.exe .tools\\fix_movie_tv_status_pref.py

可重复执行：第二次跑会输出「没有需要修改的记录」。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = BASE_DIR / "src"
sys.path.insert(0, str(SRC_DIR))


def _bootstrap_django():
    """按项目自己的方式初始化 Django（必须在 src 目录里）。"""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    # settings 里可能有相对路径（DB 文件、模板目录），切到 src 再 setup。
    os.chdir(SRC_DIR)
    import django

    django.setup()


# 这三类媒体「不可能存在」的状态值 —— 见到就清成 All。
BAD_VALUES = ("Dropped", "In progress", "Paused", "Planning")
SAFE_VALUE = "All"
FIELDS = ("movie_status", "tv_status", "season_status")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="清理电影/剧集/季库里存坏的状态偏好（Dropped 等）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印要改什么，不写数据库",
    )
    parser.add_argument(
        "--username",
        default=None,
        help="只处理指定用户（默认处理全部用户）",
    )
    args = parser.parse_args(argv)

    _bootstrap_django()

    from django.contrib.auth import get_user_model

    user_model = get_user_model()
    users = user_model.objects.all().order_by("pk")
    if args.username:
        users = users.filter(username=args.username)

    print("=" * 68)
    print(f"数据库: {BASE_DIR / 'src' / 'db.sqlite3'}")
    print(f"模式  : {'DRY-RUN（不写库）' if args.dry_run else '实际执行'}")
    print(f"规则  : 把 {', '.join(FIELDS)} 里的 {BAD_VALUES} → {SAFE_VALUE!r}")
    print("=" * 68)

    total_changes = 0
    for user in users:
        changes = []
        for field in FIELDS:
            before = getattr(user, field, None)
            if before in BAD_VALUES:
                changes.append((field, before, SAFE_VALUE))

        if not changes:
            print(f"[跳过] pk={user.pk} {user.username}: 无需修改")
            continue

        print(f"[待改] pk={user.pk} {user.username}:")
        for field, before, after in changes:
            print(f"         {field}: {before!r} → {after!r}")

        total_changes += len(changes)
        if args.dry_run:
            continue

        for field, _before, after in changes:
            setattr(user, field, after)
        user.save(update_fields=[field for field, _, _ in changes])

        # 落库后立刻回读，证明真的写进去了（别只信内存里的对象）
        user.refresh_from_db()
        for field, before, after in changes:
            print(f"         ✅ {field}: {before!r} → {getattr(user, field)!r}")

    print("-" * 68)
    if args.dry_run:
        print(f"DRY-RUN 结束：共 {total_changes} 处待修改，数据库未改动。")
        print("确认无误后去掉 --dry-run 再跑一次即可落库。")
    elif total_changes:
        print(f"完成：共修改 {total_changes} 处。")
    else:
        print("完成：没有需要修改的记录（已经是干净的）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
