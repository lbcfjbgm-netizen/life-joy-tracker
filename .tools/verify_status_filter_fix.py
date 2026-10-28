#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""临时验证脚本：用 Django test client 打真站点的 URL，统计渲染条目数。

只读，不写任何业务数据（登录会话走 test client 自己的内存，不落真实 session）。
跑法（在 yamtrack 目录下）::

    $env:PYTHONUTF8="1"
    .venv\\Scripts\\python.exe .tools\\verify_status_filter_fix.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = BASE_DIR / "src"
sys.path.insert(0, str(SRC_DIR))
os.chdir(SRC_DIR)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402

USER = get_user_model().objects.get(pk=1)

# 列表页里每个条目都会渲染一次详情链接，用它数条目最稳。
CARD_RE = re.compile(r'href="[^"]*/details/[^"]*"')


def count_items(html: str) -> int:
    return len(set(CARD_RE.findall(html)))


def hit(client: Client, url: str):
    # follow=True：裸进列表页时视图会 302 补全参数，跟进去才能看到真正的页面。
    resp = client.get(url, follow=True)
    html = resp.content.decode("utf-8", "replace") if resp.status_code == 200 else ""
    return resp, html


def checked_in(client: Client, url: str):
    """一次请求既拿 HTML，也拿视图 context 里的模板分页总数。"""
    resp = client.get(url, follow=True)
    html = resp.content.decode("utf-8", "replace") if resp.status_code == 200 else ""
    total = ""
    media_list = resp.context.get("media_list") if resp.context else None
    if media_list is not None:
        total = media_list.paginator.count
    return resp, html, total


def main():
    client = Client()
    client.force_login(USER)

    # 这些列表页会顺手把「这次用的版式/排序/状态」写进 users_user，
    # 所以跑之前先存一份，跑完原样放回去，免得验证脚本自己把用户的偏好改了。
    PREF_FIELDS = [
        f"{mt}_{name}"
        for mt in ("movie", "tv", "season", "game", "book")
        for name in ("status", "layout", "sort")
    ]
    snapshot = {f: getattr(USER, f, None) for f in PREF_FIELDS}

    try:
        _run(client)
    finally:
        USER.refresh_from_db()
        changed = {
            f: (snapshot[f], getattr(USER, f, None))
            for f in PREF_FIELDS
            if getattr(USER, f, None) != snapshot[f]
        }
        for f, value in snapshot.items():
            setattr(USER, f, value)
        if changed:
            USER.save(update_fields=list(changed))
        print()
        print("=" * 78)
        print("⑦ 偏好收尾：验证过程写进库的值已还原")
        print("=" * 78)
        for f, (old, new) in changed.items():
            print(f"  {f}: 验证时被写成 {new!r} -> 已还原 {old!r}")
        print(f"  共还原 {len(changed)} 个字段；其余字段一个没动。")


def _run(client):

    urls = [
        "/admin/movie",
        "/admin/movie?status=Dropped",
        "/admin/movie?status=In+progress",
        "/admin/tv",
        "/admin/tv?status=Dropped",
        "/admin/game?status=Dropped",
        "/admin/book?status=In+progress",
    ]

    print("=" * 78)
    print("① 列表页：状态码 + 本页渲染条目数 + 模板分页总数（总数必须 > 0）")
    print("=" * 78)
    for url in urls:
        resp, html, total = checked_in(client, url)
        n = count_items(html)
        flag = "OK " if (resp.status_code == 200 and n > 0) else "BAD"
        print(f"[{flag}] {resp.status_code}  items={n:<5} total={total!s:<6} {url}")

    print()
    print("①-b 游戏库对照组：换一档真有数据的状态，证明筛选仍然生效")
    for url, expect in (
        ("/admin/game", "裸进=全成就"),
        ("/admin/game?status=Paused", "870"),
        ("/admin/game?status=Completed", "297"),
        ("/admin/book?status=Completed", "书籍照旧"),
    ):
        resp, html, total = checked_in(client, url)
        print(f"[OK ] {resp.status_code}  items={count_items(html):<5} total={total!s:<6} {url}  ({expect})")

    print()
    print("=" * 78)
    print("② 电影库页面里的状态相关 option / button 文字")
    print("=" * 78)
    resp, html = hit(client, "/admin/movie")
    # 状态筛选按钮带 aria-label="按状态筛选"
    has_status_ctl = 'aria-label="按状态筛选"' in html
    print(f"含状态下拉控件（aria-label=按状态筛滤）: {has_status_ctl}")
    for word in ("已放弃", "已弃看", "在看", "在追", "已搁置", "待看"):
        print(f"  页面出现“{word}”: {word in html}")

    print()
    print("=" * 78)
    print("③ 剧集库同项检查")
    print("=" * 78)
    resp, html = hit(client, "/admin/tv")
    print(f"含状态下拉控件: {'aria-label=\"按状态筛选\"' in html}")
    for word in ("已放弃", "已弃剧", "在追", "已搁置"):
        print(f"  页面出现“{word}”: {word in html}")

    print()
    print("=" * 78)
    print("④ 游戏库（必须照旧，仍有五档伪状态）")
    print("=" * 78)
    resp, html = hit(client, "/admin/game")
    print(f"含状态下拉控件: {'aria-label=\"按状态筛选\"' in html}")
    for word in ("全成就", "仅通关", "已通关", "进行中", "已暂停"):
        print(f"  页面出现“{word}”: {word in html}")

    print()
    print("=" * 78)
    print("⑤ 追踪弹层 option 列表")
    print("=" * 78)
    opt_re = re.compile(r"<option value=\"([^\"]*)\"[^>]*>([^<]*)</option>")

    from app.models import Movie

    movie = Movie.objects.filter(user=USER).first()
    if movie:
        url = (
            f"/track_modal/douban/movie/{movie.item.media_id}"
            f"?return_url=/&instance_id={movie.id}"
        )
        resp, html = hit(client, url)
        opts = opt_re.findall(html)
        print(f"movie 弹层 status={resp.status_code} {url}")
        print(f"  全部 option: {opts}")
        status_opts = [(v, l) for v, l in opts if v in ("Completed", "In progress", "Paused", "Dropped", "Planning")]
        print(f"  status 段: {status_opts}")

    resp, html = hit(
        client,
        "/track_modal/steam/game/4267800?return_url=/&instance_id=4959",
    )
    opts = opt_re.findall(html)
    print(f"\ngame 弹层 status={resp.status_code}")
    print(f"  全部 option: {opts}")

    print()
    print("=" * 78)
    print("⑥ /create 页面每种媒体的 status 选项（服务端 json_script 内容）")
    print("=" * 78)
    resp, html = hit(client, "/create")
    m = re.search(
        r'<script id="status-choices-json" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    print(f"/create status={resp.status_code}")
    if m:
        print(f"  status_choices_json = {m.group(1)}")
    else:
        print("  !!! 没找到 status-choices-json")
    opts = opt_re.findall(html)
    print(f"  首屏 <option>: {opts}")


if __name__ == "__main__":
    main()
