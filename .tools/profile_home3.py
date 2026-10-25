"""只读：把首页一个分区拆成几个阶段分别计时，定位 500ms 到底花在哪。"""

from __future__ import annotations

import io
import os
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

from app.models import BasicMedia, MediaTypes, Status  # noqa: E402

user = get_user_model().objects.get(username="admin")
manager = BasicMedia.objects


def phase(name, fn, n=3):
    best = None
    out = None
    for _ in range(n):
        t0 = time.perf_counter()
        out = fn()
        dt = (time.perf_counter() - t0) * 1000
        best = dt if best is None else min(best, dt)
    print(f"    {name:42s} {best:8.1f}ms  (best of {n})")
    return out


for media_type, status in ((MediaTypes.GAME.value, Status.PLANNING.value),
                           (MediaTypes.BOOK.value, Status.PLANNING.value),
                           (MediaTypes.BOOK.value, Status.IN_PROGRESS.value)):
    print()
    print("=" * 78)
    print(f"media_type={media_type} status={status}")

    def build():
        return manager.get_media_list(
            user=user, media_type=media_type, status_filter=status, sort_filter=None)

    qs = build()
    cnt = phase("1) COUNT(*)", lambda: qs.count())
    print(f"       total rows = {cnt}")

    lst = phase("2) 全量取实例 list(qs)", lambda: list(build()))
    lst = list(build())
    print(f"       len={len(lst)}")

    phase("3) annotate_max_progress", lambda: manager.annotate_max_progress(list(build()), media_type))
    phase("4) _annotate_next_event", lambda: manager._annotate_next_event(list(build())))  # noqa: E501
    phase("5) _sort_home_media(completion)", lambda: manager._sort_home_media(list(build()), "completion"))

    def full_minus_fetch():
        m = manager
        l = list(build())
        m.annotate_max_progress(l, media_type)
        m._annotate_next_event(l)
        return m._sort_home_media(l, "completion")

    phase("6) 全链路(取+注解+排序)", full_minus_fetch)

print()
print("=" * 78)
print("SQL 计数复核：单次 get_home_status(game, planning) 发了哪些查询")
reset_queries()
manager.get_home_status(user=user, status=Status.PLANNING.value, sort_by="completion",
                        items_limit=None)
for q in connection.queries:
    print(f"  {float(q['time'])*1000:7.1f}ms  {q['sql'][:110]}")
