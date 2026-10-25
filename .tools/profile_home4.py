"""只读实验：比较"实例化 2086 行" vs "values() 轻量行 + 只实例化窗口" 的耗时。"""

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
from django.utils import timezone  # noqa: E402

from app.models import BasicMedia, MediaTypes, Status  # noqa: E402
from events.models import Event  # noqa: E402

user = get_user_model().objects.get(username="admin")
manager = BasicMedia.objects
now = timezone.now()

FIELDS = (
    "id", "progress", "progressed_at", "created_at", "status",
    "item_id", "item__title", "item__media_type", "item__image",
)


def best(fn, n=3):
    b = None
    out = None
    for _ in range(n):
        t0 = time.perf_counter()
        out = fn()
        dt = (time.perf_counter() - t0) * 1000
        b = dt if b is None else min(b, dt)
    return b, out


for media_type, status in (
    (MediaTypes.GAME.value, Status.PLANNING.value),
    (MediaTypes.BOOK.value, Status.IN_PROGRESS.value),
):
    print("=" * 78)
    print(f"{media_type}/{status}")
    qs = manager.get_media_list(
        user=user, media_type=media_type, status_filter=status, sort_filter=None)

    t_count, n = best(lambda: qs.count())
    print(f"  count()                        {t_count:8.1f}ms  n={n}")

    t_inst, lst = best(lambda: list(xs) if (xs := manager.get_media_list(
        user=user, media_type=media_type, status_filter=status,
        sort_filter=None)) else None)
    print(f"  list(qs) 实例化                 {t_inst:8.1f}ms  n={len(lst)}")

    t_vals, rows = best(lambda: list(manager.get_media_list(
        user=user, media_type=media_type, status_filter=status,
        sort_filter=None).values(*FIELDS)))
    print(f"  list(qs.values(...))           {t_vals:8.1f}ms  n={len(rows)}")

    ids = [r["item_id"] for r in rows]
    t_ev, ev = best(lambda: list(Event.objects.filter(
        item_id__in=ids, datetime__lte=now).values("item_id", "content_number")))
    print(f"  events(content_number) 批量      {t_ev:8.1f}ms  n={len(ev)}")

    t_fu, fu = best(lambda: list(Event.objects.filter(
        item_id__in=ids, datetime__gt=now).values("item_id", "datetime")))
    print(f"  events(future) 批量             {t_fu:8.1f}ms  n={len(fu)}")

    mp = {}
    for r in ev:
        c = r["content_number"]
        if c is not None:
            mp[r["item_id"]] = max(mp.get(r["item_id"], 0), c)

    def do_sort():
        return sorted(rows, key=lambda x: (
            mp.get(x["item_id"]) is None,
            -((x["progress"] / mp[x["item_id"]] * 100) if mp.get(x["item_id"]) else 0),
            -timezone.datetime.timestamp(x["progressed_at"] or x["created_at"]),
            x["item__title"].lower(),
        ))

    t_sort, srt = best(do_sort)
    print(f"  sorted(rows) Python 排序        {t_sort:8.1f}ms")

    top = srt[:14]
    t_h, inst = best(lambda: list(manager.get_media_list(
        user=user, media_type=media_type, status_filter=status, sort_filter=None,
    ).filter(pk__in=[r["id"] for r in top])))
    print(f"  只实例化 14 行                  {t_h:8.1f}ms  n={len(inst)}")
    print(f"  合计(轻量路径)                  {t_count + t_vals + t_ev + t_fu + t_sort + t_h:8.1f}ms")
    print(f"  合计(现状路径)                  {t_count + t_inst:8.1f}ms+注解+排序")
