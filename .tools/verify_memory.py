"""验收「每个库各自记住浏览状态」。

用户确认的需求（2026-10-25 第三轮）：
    5 项都要记，搜索关键词不记：
      1. 卡片大小 density  1(巨幕)/4/8/12/16
      2. 状态筛选 status   All achievements/Completed/In progress/Paused
      3. 排序 sort         score/title/progress/start_date/end_date
      4. 版式 layout       grid/table
      5. 首卡特写 lead     true/false
    且**每个库各记各的**，改一个不能影响另一个。

用法（在项目根跑）：
    .venv\\Scripts\\python.exe -X utf8 .tools\\verify_memory.py

设计原则：
- 不改数据库业务数据；测完把偏好复位到测前的值（在 finally 里）
- 用 Django test Client 独立进程跑，不影响正在运行的站点
- 打印「期望 / 实测 / 判定」，最后给总结；有 FAIL 就退 1
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.test import Client  # noqa: E402

from users.models import User  # noqa: E402

PREF_FIELDS = ["game_layout", "game_sort", "game_status", "list_density"]

# 软导航请求头：侧栏点图标走这条路径
SOFT = {"hx-request": "true", "x-soft-navigation": "true"}

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}{'  — ' + detail if detail else ''}")


def _grab(html: str, key: str) -> str | None:
    m = re.search(rf"{key}: '([^']*)'", html)
    return m.group(1) if m else None


def alpine_status(html: str):
    return _grab(html, "status")


def alpine_density(html: str):
    return _grab(html, "density")


def alpine_sort(html: str):
    return _grab(html, "sort")


def alpine_layout(html: str):
    return _grab(html, "layout")


def _load_baseline() -> dict:
    """读 .tools/baseline.json；没有就返回空 dict（基线项跳过比对）。

    基线不能再写死在脚本里：用户会持续录新游戏，写死就会误报。
    """
    path = Path(__file__).resolve().parent / "baseline.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def alpine_lead(html: str, context_lead: bool | None = None) -> str | None:
    """读首卡特写的初值。

    ⚠ 不能用 re.search(r"lead:\\s*(true|false)")：那会抓到模板 x-data 里
    硬编码的占位字面量 `lead: false, leadIdx: 0`（media_list.html:40），
    它永远是 false，和后端传什么无关 —— 这样验等于验了个常量。

    真正的落点是 initialLead() 里的 `{% if lead_mode == True %}` 分支，
    渲染出来形如：
        return {% if lead_mode == True %}true{% elif ... %}false{% else %}this.readLead(){% endif %};
    所以要看 initialLead() 函数体里 return 出来的值。
    """
    m = re.search(r"initialLead\(\)\s*\{(.{0,400}?)\n\s*\},", html, re.S)
    if not m:
        return None
    returns = re.findall(r"return (true|false|this\.readLead\(\));", m.group(1))
    if not returns:
        return None
    last = returns[-1]
    if last == "this.readLead()":
        # 走了 localStorage 兜底 —— 说明后端没传 lead_mode（这种情况按 False 论）
        return "false"
    return last


def main() -> int:
    user = User.objects.get(username="admin")
    before = {f: getattr(user, f) for f in PREF_FIELDS}
    print(f"测前偏好：{before}\n")

    try:
        # ================================================ 1. 状态记忆（后端 bug）
        print("【1】状态下拉记忆（原本 100% 失效）")
        # 这一组会改 game_status，测完必须恢复原值，否则会污染用户的真实偏好。
        status_before = User.objects.get(pk=user.pk).game_status
        try:
            for st in ["Completed", "In progress", "Paused"]:
                user.refresh_from_db()
                user.game_status = st
                user.save(update_fields=["game_status"])
                html = Client().get("/admin/game").content.decode()
                got = alpine_status(html)
                check(f"库里 game_status={st} → 页面", got == st,
                      f"期望 {st}，实测 {got}")
        finally:
            user.refresh_from_db()
            user.game_status = status_before
            user.save(update_fields=["game_status"])

        # ================================================ 2. 分库记忆（核心）
        print("\n【2】分库记忆：5 项都要各记各的")
        c = Client()
        # 游戏库：巨幕 + 全成就 + 结束日期 + grid
        c.get(
            "/admin/game?layout=grid&density=1"
            "&status=All+achievements&sort=end_date&lead=1",
        )
        # 电影库：4 档 + 已观看 + 评分 + table
        c.get(
            "/admin/movie?layout=table&density=4"
            "&status=Completed&sort=score&lead=0",
        )

        # --- 回游戏库（软导航，跟点侧栏图标一致）
        r = c.get("/admin/game", headers=SOFT)
        h = r.content.decode()
        push = r.get("HX-Push-Url") or ""
        check("游戏库软导航 200（不能 302）", r.status_code == 200,
              f"状态码 {r.status_code}")
        check("游戏·卡片大小 = 1（巨幕）", alpine_density(h) == "1",
              f"实测 {alpine_density(h)}")
        check("游戏·状态 = All achievements",
              alpine_status(h) == "All achievements", f"实测 {alpine_status(h)}")
        check("游戏·排序 = end_date", alpine_sort(h) == "end_date",
              f"实测 {alpine_sort(h)}")
        check("游戏·版式 = grid", alpine_layout(h) == "grid",
              f"实测 {alpine_layout(h)}")
        check("游戏·首卡特写 = true", alpine_lead(h) == "true",
              f"实测 {alpine_lead(h)}")
        check("游戏软导航带 HX-Push-Url 含 density=1",
              "density=1" in push, push or "(没有)")

        # --- 回电影库
        r = c.get("/admin/movie", headers=SOFT)
        h = r.content.decode()
        push = r.get("HX-Push-Url") or ""
        check("电影库软导航 200", r.status_code == 200, f"状态码 {r.status_code}")
        check("电影·卡片大小 = 4（不能被巨幕污染）", alpine_density(h) == "4",
              f"实测 {alpine_density(h)}")
        check("电影·状态 = Completed", alpine_status(h) == "Completed",
              f"实测 {alpine_status(h)}")
        check("电影·排序 = score", alpine_sort(h) == "score",
              f"实测 {alpine_sort(h)}")
        check("电影·版式 = table", alpine_layout(h) == "table",
              f"实测 {alpine_layout(h)}")
        check("电影·首卡特写 = false", alpine_lead(h) == "false",
              f"实测 {alpine_lead(h)}")
        check("电影地址里不能出现 density=1", "density=1" not in push, push)

        # ================================================ 3. 302 补全 + 防循环
        print("\n【3】裸地址补全参数（302）+ 防循环")
        r = c.get("/admin/game")
        loc = r.get("Location") or ""
        check("裸访问 /admin/game 会 302", r.status_code == 302,
              f"状态码 {r.status_code}，Location={loc or '(空)'}")
        if loc:
            path = re.sub(r"^https?://[^/]+", "", loc)
            r2 = c.get(path)
            check("跟着跳转地址再请求 → 200（防循环）", r2.status_code == 200,
                  f"状态码 {r2.status_code}")

        # 全新会话（没存过）不该被补全
        r = Client().get("/admin/game")
        check("全新会话裸访问不 302（没存过就不跳）", r.status_code == 200,
              f"状态码 {r.status_code}")

        # ================================================ 4. 不该跳的情况
        print("\n【4】不该跳转的情况（必须 200）")
        for label, url in [
            ("带 page", "/admin/game?page=1"),
            ("带 search", "/admin/game?search=hades"),
            ("已带 density", "/admin/game?density=8"),
            ("已带 status", "/admin/game?status=Completed"),
            ("巨屏搜索", "/admin/game?density=1&search=hades"),
        ]:
            rr = Client().get(url)
            check(f"{label} 不 302", rr.status_code == 200, f"状态码 {rr.status_code}")

        # ================================================ 5. HTMX 局部请求
        print("\n【5】HTMX 局部请求（不能变 302）")
        rr = Client().get("/admin/game?layout=grid", headers={"hx-request": "true"})
        check("局部 HTMX 仍是 200 片段", rr.status_code == 200,
              f"状态码 {rr.status_code}")

        # ================================================ 6. 非游戏库不套巨幕
        print("\n【6】非游戏库不能套巨幕")
        html = Client().get("/admin/movie?density=1").content.decode()
        check("电影库 density 不是 1", alpine_density(html) != "1",
              f"实测 {alpine_density(html)}")

        # ================================================ 7. 搜索不记
        print("\n【7】搜索关键词不记")
        c_s = Client()
        c_s.get("/admin/game?search=hades")
        html = c_s.get("/admin/game", headers=SOFT).content.decode()
        # 只认 Alpine 的 search 初值（x-data 里那个 `search: ''`），
        # 不能用宽松正则，否则会抓到 placeholder="…搜索…" 里的字。
        m = re.search(r"search:\s*'([^']*)',\s*statusLabels", html)
        got_search = m.group(1) if m else None
        check("离开搜索后回来搜索框是空的", got_search in ("", None),
              f"实测 {got_search!r}")

        # ================================================ 8. 基线
        print("\n【8】基线 4 个数字")
        from app.models import Game, Item  # noqa: E402

        # 基线从 .tools/baseline.json 读，不要在这里写死：
        # 用户每录一款新游戏 app_game / app_item / score 就 +1，写死的话
        # 这个验收脚本会自己先失灵（已经踩过一次）。
        # 允许增长，只禁止减少（减少才是出事故）。
        baseline = _load_baseline()
        for label, actual, key, may_grow in [
            ("app_game", Game.objects.count(), "app_game", True),
            ("app_item", Item.objects.count(), "app_item", True),
            ("score 非空", Game.objects.exclude(score__isnull=True).count(),
             "score 非空", True),
            ("status=Dropped", Game.objects.filter(status="Dropped").count(),
             "status='Dropped'", False),
        ]:
            expect = baseline.get(key)
            if expect is None:
                check(f"{label}（基线缺失，跳过比对）", True, f"实测 {actual}")
            elif may_grow:
                check(f"{label} ≥ {expect}", actual >= expect,
                      f"实测 {actual}")
            else:
                check(f"{label} = {expect}", actual == expect, f"实测 {actual}")

    finally:
        # 复位测前偏好。如果用 Ctrl+C 或杀进程，这里不会执行，
        # 但开头已经把原值打印出来了，照着 .tools/reset_prefs.py 手工复位即可。
        user.refresh_from_db()
        for f, v in before.items():
            setattr(user, f, v)
        user.save(update_fields=list(before))
        print(f"\n已复位偏好：{before}")

    # ---------------------------------------------------------------- 总结
    print("\n" + "=" * 62)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"  通过 {passed}/{total}")
    failed = [(n, d) for n, ok, d in results if not ok]
    if failed:
        print("\n  失败项：")
        for n, d in failed:
            print(f"    ❌ {n}  —  {d}")
        print("\n  结论：❌ 没修好，别交付。")
        return 1
    print("\n  结论：✅ 5 项分库记忆全部生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
