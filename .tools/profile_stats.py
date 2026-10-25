"""只读：统计页各函数分别计时，定位 235ms 花在哪。"""

from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT / "src")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection, reset_queries  # noqa: E402
from django.test import Client  # noqa: E402

from app import statistics as stats  # noqa: E402

user = get_user_model().objects.get(username="admin")


def best(fn, n=3):
    b = None
    out = None
    for _ in range(n):
        t0 = time.perf_counter()
        out = fn()
        dt = (time.perf_counter() - t0) * 1000
        b = dt if b is None else min(b, dt)
    return b, out


t_um, (um, mc) = best(lambda: stats.get_user_media(user, None, None))
print(f"get_user_media                {t_um:8.1f}ms")

reset_queries()
t_sd, sd = best(lambda: stats.get_status_distribution(um))
print(f"get_status_distribution       {t_sd:8.1f}ms  ({len(connection.queries)} sql 累计)")

reset_queries()
t_sc, (scd, top) = best(lambda: stats.get_score_distribution(um))
print(f"get_score_distribution        {t_sc:8.1f}ms")
print(f"   top rated = {[m.item.title[:24] for m in top]}")

reset_queries()
t_cs, cons = best(lambda: stats.get_consumption_stats(um, mc))
print(f"get_consumption_stats         {t_cs:8.1f}ms")

t_pie, _ = best(lambda: stats.get_status_pie_chart_data(sd))
t_mt, _ = best(lambda: stats.get_media_type_distribution(mc))
print(f"pie+type distribution         {t_pie + t_mt:8.1f}ms")

c = Client()
c.force_login(user)
c.get("/statistics")
t_tot, resp = best(lambda: c.get("/statistics"))
print(f"整页（test client）            {t_tot:8.1f}ms  html={len(resp.content)}B")

# 只跑视图前半段（不算渲染）粗算模板渲染耗时
from django.template.loader import render_to_string  # noqa: E402

t_home, resp2 = best(lambda: c.get("/"))
print(f"首页整页（test client）        {t_home:8.1f}ms  html={len(resp2.content)}B")

# 统计页 SQL 条数
reset_queries()
c.get("/statistics")
print(f"统计页 SQL 条数 = {len(connection.queries)}")
print(f"统计页 SQL 总耗时 = {sum(float(q['time']) for q in connection.queries)*1000:.1f}ms")
reset_queries()
c.get("/")
print(f"首页 SQL 条数 = {len(connection.queries)}")
print(f"首页 SQL 总耗时 = {sum(float(q['time']) for q in connection.queries)*1000:.1f}ms")
