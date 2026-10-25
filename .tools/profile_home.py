"""只读性能剖析：首页 / 统计页的 SQL 条数、最慢 SQL、返回行数。

用 Django test client 走真实视图（不经过 socket），
临时打开 connection.queries 力抓 SQL（不改 settings.py，只在进程内临时置位）。
"""

from __future__ import annotations

import io
import os
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
os.chdir(SRC)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection, reset_queries  # noqa: E402
from django.test import Client  # noqa: E402

user = get_user_model().objects.get(username="admin")

# Force query logging without touching settings.py on disk.
from django.conf import settings  # noqa: E402

settings.DEBUG = True
connection.force_debug_cursor = True
reset_queries()


def timed(client, url, runs=5):
    times = []
    last = None
    for _ in range(runs):
        t0 = time.perf_counter()
        resp = client.get(url)
        times.append((time.perf_counter() - t0) * 1000)
        last = resp
    return statistics.median(times), last, times


def parse_rows(sql):
    """Best-effort row count from sqlite EXPLAIN-ish counting: run the query."""
    return None


def profile_page(client, url, label):
    reset_queries()
    t0 = time.perf_counter()
    resp = client.get(url)
    total_ms = (time.perf_counter() - t0) * 1000
    queries = list(connection.queries)
    print()
    print("=" * 78)
    print(f"{label}  {url}")
    print(f"  status={resp.status_code}  html={len(resp.content)}B  wall={total_ms:.1f}ms")
    print(f"  SQL 条数 = {len(queries)}")

    entries = []
    for q in queries:
        sql = q["sql"]
        try:
            ms = float(q["time"]) * 1000
        except (TypeError, ValueError):
            ms = 0.0
        # count rows by re-running the same SELECT (read-only, no writes here)
        rows = -1
        if sql.lstrip().upper().startswith("SELECT"):
            try:
                with connection.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM (" + sql + ")")
                    rows = cur.fetchone()[0]
            except Exception:
                rows = -2
        entries.append((ms, rows, sql))

    print("  --- 最慢 10 条 ---")
    for ms, rows, sql in sorted(entries, key=lambda x: -x[0])[:10]:
        flat = re.sub(r"\s+", " ", sql).strip()
        print(f"  {ms:8.1f}ms {rows:6d}行  {flat[:200]}")

    print("  --- 按耗时归类（同一 SQL 前缀合并）---")
    buckets = {}
    for ms, rows, sql in entries:
        flat = re.sub(r"\s+", " ", sql).strip()
        key = flat[:80]
        b = buckets.setdefault(key, [0, 0.0, rows])
        b[0] += 1
        b[1] += ms
    for key, (n, ms, rows) in sorted(buckets.items(), key=lambda x: -x[1][1])[:8]:
        print(f"  n={n:4d}  {ms:8.1f}ms  rows={rows:6d}  {key}")

    return resp, total_ms, queries


c = Client()
c.force_login(user)

for label, url in (
    ("首页", "/"),
    ("统计页", "/statistics"),
    ("游戏列表(对照)", "/admin/game"),
    ("列表(对照)", "/list"),
):
    try:
        med, resp, times = timed(c, url, runs=5)
        print(f"[TIMING] {label:14s} 5次中位 {med:8.1f}ms  全部 {[round(t,1) for t in times]}  html={len(resp.content)}B")
    except Exception as exc:  # noqa: BLE001
        print(f"[TIMING] {label} 失败 {exc}")

profile_page(c, "/", "首页 SQL 剖析")
profile_page(c, "/statistics", "统计页 SQL 剖析")

# ---- 首页各分区 total（正确性基线） ----
print()
print("=" * 78)
print("首页各分区 total（正确性基线）")
resp = c.get("/")
html = resp.content.decode("utf-8", "replace")
for m in re.finditer(r'<section class="space-y-5">', html):
    pass
# section 标题 + count + 各媒体类型卡数量
secs = html.split('<section class="space-y-5">')[1:]
for i, sec in enumerate(secs):
    h2 = re.search(r'<h2[^>]*>([^<]+)</h2>', sec)
    count = re.search(r'([\d,]+)\s*<!--?\s*-->?\s*item|([\d,]+)\s*item', sec)
    badge = re.search(r'text-sm text-gray-300">(.*?)</span>', sec, re.S)
    badge_txt = re.sub(r"<[^>]+>", "", badge.group(1)).strip() if badge else "?"
    ids = re.findall(r'id="media-grid-([a-z_]+)-([a-z]+)"', sec)
    cards = re.findall(r'id="home-media-([a-z]+)-(\d+)"', sec)
    loadall = re.findall(r'Load all \((\d+)\)|加载全部 \((\d+)\)|\((\d+)\)</button>', sec)
    types = {}
    for mt, _mid in cards:
        types[mt] = types.get(mt, 0) + 1
    print(f"  section[{i}] {h2.group(1) if h2 else '?'}  徽标={badge_txt!r}  卡片数={len(cards)}  按类型={types}")
    print(f"            media-grid={ids}")

# 前 14 个标题
titles = re.findall(r'title="([^"]{1,120})">', html)
print()
print("首页前 14 个 title= 属性（用于改前后对比）:")
for t in titles[:14]:
    print("   ", t)

# ---- 统计页数字（正确性基线） ----
print()
print("=" * 78)
print("统计页数字（正确性基线）")
resp = c.get("/statistics")
shtml = resp.content.decode("utf-8", "replace")
Path(ROOT / ".tools" / "_w_stats_before.html").write_text(shtml, encoding="utf-8")
Path(ROOT / ".tools" / "_w_home_before.html").write_text(html, encoding="utf-8")
for pat in (
    r'([\d,]+)\s*(?:个)?游戏',
    r'([\d,]+)\s*h',
):
    print(f"  pattern {pat}: {re.findall(pat, shtml)[:20]}")
print("  已保存 _w_home_before.html / _w_stats_before.html")
