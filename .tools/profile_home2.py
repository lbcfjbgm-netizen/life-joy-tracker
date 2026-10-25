"""只读：cProfile 找首页/统计页的 Python 侧热点，并抓正确性基线数字。"""

from __future__ import annotations

import cProfile
import io
import os
import pstats
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
c = Client()
c.force_login(user)

# 预热
c.get("/")
c.get("/statistics")

for label, url in (("首页", "/"), ("统计页", "/statistics")):
    pr = cProfile.Profile()
    pr.enable()
    resp = c.get(url)
    pr.disable()
    print()
    print("=" * 78)
    print(f"{label} {url}  status={resp.status_code} html={len(resp.content)}B")
    st = pstats.Stats(pr)
    st.sort_stats("cumulative")
    buf = io.StringIO()
    st.stream = buf
    st.print_stats(30)
    lines = buf.getvalue().splitlines()
    keep = False
    for ln in lines:
        if "ncalls" in ln:
            keep = True
        if keep:
            print(ln)

# ---- 正确性基线：直接调内部函数拿结构数字 ----
print()
print("=" * 78)
print("正确性基线（内部函数，非 HTML 解析）")
from app import home as home_helpers  # noqa: E402
from app import statistics as stats  # noqa: E402
from app.models import Status  # noqa: E402


class FakeReq:
    def __init__(self, u):
        self.user = u
        self.headers = {}
        self.GET = {}
        self.POST = {}


req = FakeReq(user)
sort_by = user.home_sort
for section_key in (Status.IN_PROGRESS.value, Status.PLANNING.value):
    d = home_helpers.get_home_media_types(req, sort_by, section_key, 14)
    print(f"  [home/{section_key}] total = " + ", ".join(
        f"{k}={v['total']}" for k, v in d.items()))
    print(f"             displayed = " + ", ".join(
        f"{k}={len(v['items'])}" for k, v in d.items()))

um, mc = stats.get_user_media(user, None, None)
print(f"  media_count = {mc}")
sd = stats.get_status_distribution(um)
print("  status datasets:")
for ds in sd["datasets"]:
    print(f"    {ds['label']:10s} total={ds['total']}")
print(f"  total_completed = {sd['total_completed']}")
scd, top = stats.get_score_distribution(um)
print(f"  score avg={scd['average_score']} total_scored={scd['total_scored']} top={len(top)}")
cons = stats.get_consumption_stats(um, mc)
for x in cons:
    print(f"  consumption {x['media_type']:6s} = {x['value']}  ({x['descriptor']})")
print(f"  media type chart = {stats.get_media_type_distribution(mc)}")
