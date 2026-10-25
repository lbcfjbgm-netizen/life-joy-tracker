# -*- coding: utf-8 -*-
"""3.5 放行前的 20 个抽样验证：按年份分层，用新进程 django.test.Client 逐页渲染。

核对四项：状态码 200 / 页面显示完成时间 / 推算标记正确 / 中文简介是否已补上。
用法（在 src 目录跑）：
    ..\\.venv\\Scripts\\python.exe ..\\scripts\\sample_check_20.py --report ..\\..\\_抽样20报告.md
"""

import argparse
import html
import os
import re
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402
from django.utils import timezone  # noqa: E402

from app.models import Game, MediaTypes, Sources  # noqa: E402

# 每年抽几个：2025/2026 全取，2023/2024 各取 5，合计 20，跨 4 个年份
PER_YEAR = {"2023": 5, "2024": 5, "2025": 4, "2026": 6}


def pick_games():
    """Stratified sample: spread over years, and cover every game with a synopsis.

    Only a handful of the completed games have a cached Steam description, so
    those are pulled in first: they are the only ones that can prove the new
    synopsis actually reaches the page. The remaining slots stay stratified by
    year with a real/estimated mix.
    """
    rows = list(
        Game.objects.select_related("item", "user")
        .filter(status="Completed", end_date__isnull=False)
        .order_by("end_date", "id"),
    )
    by_year = {}
    for game in rows:
        year = timezone.localtime(game.end_date).strftime("%Y")
        by_year.setdefault(year, []).append(game)

    picked = [g for g in rows if (g.item.synopsis or "").strip()]
    picked_ids = {g.id for g in picked}

    real = [g for g in rows if not g.completion_estimated and (g.item.synopsis or "").strip()]
    estimated = [g for g in rows if g.completion_estimated and (g.item.synopsis or "").strip()]
    print(
        f"[抽样] 54 条里有中文简介的 {len(picked)} 条"
        f"（真实 {len(real)} / 推算 {len(estimated)}），全部纳入样本",
    )

    for year, want in sorted(PER_YEAR.items()):
        pool = [g for g in by_year.get(year, []) if g.id not in picked_ids]
        if not pool:
            continue
        have = sum(
            1 for g in picked if timezone.localtime(g.end_date).strftime("%Y") == year
        )
        want = max(want - have, 0)
        if not want:
            continue
        real = [g for g in pool if not g.completion_estimated]
        estimated = [g for g in pool if g.completion_estimated]
        want = min(want, len(pool))
        take_real = min(len(real), max(want // 2 + (want % 2), want - len(estimated)))
        take_est = min(len(estimated), want - take_real)
        take_real = min(len(real), want - take_est)

        def spread(group, count):
            if count <= 0 or not group:
                return []
            if count >= len(group):
                return list(group)
            step = len(group) / count
            return [group[int(i * step)] for i in range(count)]

        picked.extend(spread(real, take_real))
        picked.extend(spread(estimated, take_est))

    picked.sort(key=lambda g: g.end_date)
    return picked[:20] if len(picked) > 20 else picked


def first_line(text, limit=24):
    """Return a short, HTML-safe fragment that must appear on the page."""
    line = next((ln.strip() for ln in text.split("\n") if ln.strip()), "")
    return line[:limit]


def check_one(client, game):
    item = game.item
    url = reverse(
        "media_details",
        kwargs={
            "source": Sources.STEAM.value,
            "media_type": MediaTypes.GAME.value,
            "media_id": item.media_id,
            "title": "x",
        },
    )
    response = client.get(url)
    body = response.content.decode("utf-8", "replace")

    local_end = timezone.localtime(game.end_date) if game.end_date else None
    year = local_end.strftime("%Y") if local_end else ""
    exact = local_end.strftime("%Y-%m-%d %H:%M") if local_end else ""

    shows_year = f"{year} 年" in body
    has_marker = "（推算）" in body
    if game.completion_estimated:
        date_ok = shows_year and has_marker
    else:
        date_ok = exact in body and not has_marker

    synopsis_len = len((item.synopsis or "").strip())
    if synopsis_len:
        probe = first_line(item.synopsis)
        synopsis_ok = html.escape(probe) in body
    else:
        # 没有简介的条目，页面上也不许出现占位符，空着才是对的
        probe = ""
        synopsis_ok = "No synopsis available." not in body

    return {
        "status": response.status_code,
        "url": url,
        "year": year,
        "exact": exact,
        "estimated": bool(game.completion_estimated),
        "shows_year": shows_year,
        "has_marker": has_marker,
        "date_ok": date_ok,
        "synopsis_len": synopsis_len,
        "synopsis_ok": synopsis_ok,
        "synopsis_shown": synopsis_len > 0 and synopsis_ok,
        "probe": probe,
    }


def main():  # noqa: PLR0915
    parser = argparse.ArgumentParser(description="20 个跨年份抽样验证（默认只读）")
    parser.add_argument("--report", help="写 Markdown 报告到这个路径")
    args = parser.parse_args()

    games = pick_games()
    admin = get_user_model().objects.filter(is_superuser=True).first()
    if admin is None:
        raise SystemExit("没有 superuser，无法登录渲染")

    # 新进程 + 空的模板缓存，专门用来抓「写库了但页面不显示」这类问题
    client = Client(SERVER_NAME="127.0.0.1")
    client.force_login(admin)

    results = []
    for game in games:
        results.append((game, check_one(client, game)))

    ok200 = sum(1 for _, r in results if r["status"] == 200)
    okdate = sum(1 for _, r in results if r["date_ok"])
    okframe = sum(1 for _, r in results if r["synopsis_ok"])
    shown = sum(1 for _, r in results if r["synopsis_shown"])
    with_syn = sum(1 for _, r in results if r["synopsis_len"] > 0)
    empty = len(results) - with_syn
    est_ok = sum(1 for g, r in results if g.completion_estimated and r["date_ok"])
    est_n = sum(1 for g, _ in results if g.completion_estimated)
    real_ok = sum(1 for g, r in results if not g.completion_estimated and r["date_ok"])
    real_n = sum(1 for g, _ in results if not g.completion_estimated)

    print(f"抽样 {len(results)} 个（跨 {len({r['year'] for _, r in results})} 个年份）")
    print(f"  200        : {ok200}/{len(results)}")
    print(f"  日期+标记对 : {okdate}/{len(results)}")
    print(f"    推算的 {est_ok}/{est_n} 有「（推算）」；真实的 {real_ok}/{real_n} 无标记")
    print(f"  简介有内容上页面 : {shown}/{with_syn}")
    print(f"  简介为空且页面无占位符: {okframe - shown}/{empty}")
    print()
    for game, r in results:
        flag = "OK " if (r["status"] == 200 and r["date_ok"] and r["synopsis_ok"]) else "BAD"
        print(
            f"{flag} {r['year']} {game.item.media_id:>8} "
            f"{'推算' if r['estimated'] else '真实'} "
            f"{'有标记' if r['has_marker'] else '无标记'} "
            f"简介{r['synopsis_len']:>5}字 "
            f"{'上页面' if r['synopsis_shown'] else ('无内容' if not r['synopsis_len'] else '上页面失败')} "
            f"{game.item.title[:22]}",
        )

    if args.report:
        lines = [
            "# 3.5 前的 20 个抽样验证",
            "",
            f"- 方式：新进程 `django.test.Client`（admin 登录、进程内无模板缓存）逐页渲染详情页",
            f"- 样本：从「Completed 且 end_date 非空」的 54 条里按年份分层抽 {len(results)} 个"
            f"（2023/2024/2025/2026）",
            f"- 结论：**{ok200}/{len(results)} 页面 200；{okdate}/{len(results)} 完成时间与推算标记正确"
            f"（推算 {est_ok}/{est_n} 有标记、真实 {real_ok}/{real_n} 无标记）；"
            f"有中文简介的 {shown}/{with_syn} 条已上页面**",
            "",
            "| # | 年份 | 游戏 | appid | 完成时间(库,本地) | 类型 | 页面日期判定 | 简介字数 | 简介上页面 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for index, (game, r) in enumerate(results, 1):
            lines.append(
                f"| {index} | {r['year']} | {game.item.title[:26]} | {game.item.media_id} | "
                f"{r['exact']} | {'推算' if r['estimated'] else '真实'} | "
                f"{'✓' if r['date_ok'] else '✗'} "
                f"{'（推算）在' if r['has_marker'] else '无标记'} | "
                f"{r['synopsis_len']} | "
                f"{'✓ 显示' if r['synopsis_shown'] else ('— 无简介(页面无占位符)' if not r['synopsis_len'] else '✗')} |",
            )
        lines += [
            "",
            "## 判定口径",
            "",
            "- 推算值：页面必须出现「YYYY 年」和「（推算）」，且不露出具体月日；",
            "- 真实值：页面必须出现 `YYYY-MM-DD HH:MM` 且**不出现**「（推算）」；",
            "- 简介：库里的第一行前 24 字必须出现在渲染后的 HTML 里（转义后比对）；",
            "- 库里简介为空的条目，页面必须**不出现** `No synopsis available.` 占位符。",
            "",
        ]
        Path(args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n报告已写：{args.report}")

    bad = [g for g, r in results if r["status"] != 200 or not r["date_ok"] or not r["synopsis_ok"]]
    if bad:
        print(f"\n有 {len(bad)} 个不合格，先别放全量。")
        sys.exit(1)


if __name__ == "__main__":
    main()
