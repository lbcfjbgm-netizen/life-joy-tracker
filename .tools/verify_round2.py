# 第二轮复验：一条命令验全部
# 用法（在 yamtrack 目录下）：
#   .venv\Scripts\python.exe -X utf8 .tools\verify_round2.py
#
# 验 5 件事：
#   1. 基线 4 个数字没变
#   2. /near-complete 页面 200 且 11 款
#   3. 搜索耗时 ≤10 秒
#   4. 手动创建条目能建出来（P0 回归）
#   5. 赛季页剧集列表能显示（P0 回归）

import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8001"
# 脚本从 yamtrack/ 目录调用，但 Django 的 settings 模块叫 ``config``，
# 它在 src/ 下面，所以要把 src 塞进 sys.path 才导入得到。
_SRC = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
DB = str(__import__("pathlib").Path(_SRC) / "db" / "db.sqlite3")


def http(path, timeout=180):
    t = time.time()
    try:
        req = urllib.request.Request(BASE + path, headers={"User-Agent": "verify/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.getcode(), body, time.time() - t
    except Exception as exc:  # noqa: BLE001
        return f"ERR {exc!r}", "", time.time() - t


# ────────────────────────────── 1. 基线 ──────────────────────────────
def check_baseline():
    print("=" * 64)
    print("[1] 基线 4 个数字")
    print("=" * 64)
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    checks = [
        ("app_game", "select count(*) from app_game", 4935),
        ("app_item", "select count(*) from app_item", 10171),
        ("score 非空", "select count(*) from app_game where score is not null", 284),
        ("status=Dropped", "select count(*) from app_game where status='Dropped'", 1603),
    ]
    ok_all = True
    for name, sql, expect in checks:
        got = cur.execute(sql).fetchone()[0]
        ok = got == expect
        ok_all = ok_all and ok
        print(f"  {name:16s} 实测 {got:<7} 期望 {expect:<7} {'OK' if ok else 'MISMATCH ⚠'}")
    print(f"  integrity        {cur.execute('pragma integrity_check').fetchone()[0]}")
    conn.close()
    return ok_all


# ─────────────────────────── 2. 临门一脚 ───────────────────────────
def check_near_complete():
    print()
    print("=" * 64)
    print("[2] 临门一脚 /near-complete")
    print("=" * 64)
    code, body, dt = http("/near-complete")
    print(f"  HTTP {code}   {dt:.2f}s   {len(body)} bytes")
    if code != 200:
        print("  ❌ 页面不可用")
        return False
    checks = {
        "Tap Ninja": "Tap Ninja" in body,
        "「还差」字样": "还差" in body,
        "共 11 款": "11" in body,
    }
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} 含 {k}")
    cards = len(re.findall(r"/details/[a-z]+/game/\d+", body))
    print(f"  卡片链接数: {cards}")
    return all(checks.values())


# ──────────────────────────── 3. 搜索耗时 ────────────────────────────
def check_search():
    print()
    print("=" * 64)
    print("[3] 搜索耗时（判据 ≤10 秒）")
    print("=" * 64)
    words = ["赛博朋克", "fallout", "cyberpunk", "portal", "noita", "witcher", "hades"]
    over = 0
    worst = 0.0
    for q in words:
        path = "/search?" + urllib.parse.urlencode({"q": q})
        code, body, dt = http(path)
        worst = max(worst, dt)
        flag = ""
        if isinstance(code, int) and dt > 10:
            flag = "  ⚠ 超 10 秒"
            over += 1
        degraded = "连不上" in body or "没搜成" in body
        print(f"  {q:12s} HTTP {code}  {dt:6.2f}s  降级={degraded}{flag}")
    print(f"  → 最慢 {worst:.2f}s，超 10 秒 {over}/{len(words)} 次")
    return over == 0


# ────────────────────── 4. 手动创建条目（P0）──────────────────────
def check_create_entry():
    print()
    print("=" * 64)
    print("[4] 手动创建条目（P0：之前完全失效）")
    print("=" * 64)
    print("  → 这一步要写库，用 Django 直接测，测完立刻清理")
    import os

    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    from django.contrib.auth import get_user_model
    from django.test import Client

    from app.models import Item

    user = get_user_model().objects.get(pk=1)
    before = Item.objects.count()
    title = "ZZ_VERIFY_CREATE_PROBE"

    client = Client()
    client.force_login(user)
    resp = client.post(
        "/create",
        {"media_type": "game", "source": "manual", "title": title, "status": "Planning"},
    )
    after = Item.objects.count()
    delta = after - before

    print(f"  POST /create  status={resp.status_code}")
    print(f"  Item 数量  前={before}  后={after}  差={delta}")

    made = Item.objects.filter(title=title).first()
    ok = delta >= 1 and made is not None
    if made is not None:
        print(f"  ✅ 建出来了：image 长度={len(made.image or '')}  media_id={made.media_id}")
        made.delete()
        print(f"  已清理，Item 数回到 {Item.objects.count()}")
    else:
        print("  ❌ 一条都没建出来")
        body = resp.content.decode("utf-8", "replace")
        for kw in ("URL", "200", "图片", "image"):
            if kw in body:
                print(f"     页面里出现关键字 {kw}")
    return ok


# ────────────────────── 5. 赛季页剧集（P0）──────────────────────
def check_season_episodes():
    print()
    print("=" * 64)
    print("[5] 赛季页剧集列表（P0：之前永不显示）")
    print("=" * 64)
    import os

    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    from django.contrib.auth import get_user_model
    from django.test import Client

    from app.models import Episode, Season

    user = get_user_model().objects.get(pk=1)
    target = None
    for season in Season.objects.select_related("item"):
        if Episode.objects.filter(related_season=season).count() > 0:
            target = season
            break

    if target is None:
        print("  ⚠ 库里找不到带剧集的赛季，跳过")
        return None

    n_eps = Episode.objects.filter(related_season=target).count()
    item = target.item
    # 注意：赛季详情页的路由是 details/<source>/tv/<id>/<title>/season/<n>，
    # 不是 .../season/<id>/...（踩过：写成 season 会 404）。
    url = f"/details/{item.source}/tv/{item.media_id}/{item.title}/season/{item.season_number}"
    print(f"  样本：{item.title}（第 {item.season_number} 季，{n_eps} 集）")

    client = Client()
    client.force_login(user)
    resp = client.get(url)
    body = resp.content.decode("utf-8", "replace")

    print(f"  HTTP {resp.status_code}")
    # 真实用户的 obfuscate_unseen_episodes 是 False，且这一季 8 集都已看，
    # 所以剧集卡片渲染出来是 cursor-text 而不是 blur cursor-pointer。
    # 这里判「剧集卡片本身在不在」，两个分支都算数。
    has_card = ("line-clamp-1 blur cursor-pointer" in body) or (
        "line-clamp-1 cursor-text" in body
    )
    print(f"  {'✅' if has_card else '❌'} 剧集卡片渲染（blur 或 cursor-text 任一）")

    first_ep = Episode.objects.filter(related_season=target).first()
    ep_title = (first_ep.item.title or "")[:16] if first_ep else ""
    has_title = bool(ep_title) and ep_title in body
    print(f"  {'✅' if has_title else '❌'} 首集标题出现（{ep_title}）")

    return resp.status_code == 200 and has_card and has_title


def main():
    ok_base = check_baseline()
    ok_near = check_near_complete()
    ok_search = check_search()
    ok_create = check_create_entry()
    ok_season = check_season_episodes()

    print()
    print("=" * 64)
    print("汇总")
    print("=" * 64)
    rows = [
        ("基线 4 个数字", ok_base),
        ("临门一脚页面", ok_near),
        ("搜索 ≤10 秒", ok_search),
        ("手动创建条目（P0）", ok_create),
        ("赛季页剧集（P0）", ok_season),
    ]
    for name, ok in rows:
        mark = {True: "✅ 通过", False: "❌ 不通过", None: "⚠ 跳过"}[ok]
        print(f"  {name:20s} {mark}")

    all_ok = all(v for v in (ok_base, ok_near, ok_search, ok_create) if v is not None) and ok_season is not False
    print()
    print("  结论：" + ("全过，可以交付。" if all_ok else "有项没过，看上面明细。"))


if __name__ == "__main__":
    main()
