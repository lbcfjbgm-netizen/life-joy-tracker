"""验证列表页状态下拉只剩四档、各自命中数正确。

直接用 Django 测试客户端渲染页面，不碰 8001 线上进程。
用法（从 yamtrack/ 根目录）：

    .venv\\Scripts\\python.exe scripts\\check_status_filters.py
"""

import io
import os
import re
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import django  # noqa: E402

django.setup()
from django.test import Client  # noqa: E402

from app.models import Book, Game, MediaTypes  # noqa: E402
from users.models import User  # noqa: E402

USERNAME = "admin"
MEDIA_TYPE = "game"

client = Client()
user = User.objects.get(username=USERNAME)
client.force_login(user)

base = f"/{USERNAME}/{MEDIA_TYPE}"
html = client.get(base).content.decode("utf-8", "replace")

# 下拉区域：从 "Filter by Status" 到 "Sort by" 之间
start = html.find("Filter by Status")
end = html.find("Sort by", start)
block = html[start:end] if start != -1 and end != -1 else ""
options = re.findall(r"@click=\"status = '([^']+)'", block)
labels = re.findall(r"@click=\"status = '[^']+'; open = false\"[^>]*>([^<]+)<", block)

print("== 下拉选项 ==")
print("数量:", len(options))
for value, label in zip(options, labels):
    print(f"  {value!r} -> {label}")
print("含「计划中」:", "计划中" in block)
print("含 Planning 值:", any(o == "Planning" for o in options))
print("含「全部」/All:", any(o == "All" for o in options))

print("\n== 各档命中数（直接查库）==")
expect = {
    "全成就": Game.objects.filter(
        status="Completed",
        is_all_achievements=True,
    ).count(),
    "已通关": Game.objects.filter(
        status="Completed",
        is_all_achievements=False,
    ).count(),
    "进行中": Game.objects.filter(status="In progress").count(),
    "已暂停": Game.objects.filter(status__in=["Paused", "Planning"]).count(),
}
for k, v in expect.items():
    print(f"  {k}: {v}")

print("\n== 页面实际抓取（分页总数）==")
from django.core.paginator import Paginator  # noqa: E402

from app.models import BasicMedia  # noqa: E402

for value in ("All achievements", "Completed", "In progress", "Paused"):
    qs = BasicMedia.objects.get_media_list(
        user=user,
        media_type=MEDIA_TYPE,
        status_filter=value,
        sort_filter=None,
    )
    print(f"  {value!r}: {qs.count()}")

print("\n== 页码响应码 ==")
for value in ("All achievements", "Completed", "In progress", "Paused", "All"):
    resp = client.get(f"{base}?status={value}")
    print(f"  status={value!r}: HTTP {resp.status_code}")

book_qs = BasicMedia.objects.get_media_list(
    user=user,
    media_type=MediaTypes.BOOK.value,
    status_filter="All achievements",
    sort_filter=None,
)
print("\nBook / 全成就（Book 无成就字段，应退化为整块 Completed）:", book_qs.count())
print("Book 总数:", Book.objects.count())

print("\n== 回归 ==")
for url in ("/", "/journal", "/statistics", "/panel/app/game/"):
    print(f"  {url}: HTTP {client.get(url).status_code}")
