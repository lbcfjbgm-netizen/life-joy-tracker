"""用 django.test.Client 在新进程里渲染 5 个详情页，检查新字段是否出现在 HTML 里。

判断方式：取字段里的可见文字（去掉换行），找页面是否包含该片段。
不碰线上 8001 进程，只走 Django 自己的测试客户端。
"""

import io
import os
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import django  # noqa: E402

django.setup()
from django.test import Client  # noqa: E402

from app.models import Item  # noqa: E402
from users.models import User  # noqa: E402

APPIDS = ["1091500", "292030", "1012880", "10", "220"]


def visible(text, n=14):
    """取一段可见文字做指纹；按空白切成词，用第一个足够长的词判断。

    模板会把换行渲染成 <br>，所以不能拿整段带空格的串直接比。
    """
    words = [w for w in (text or "").split() if len(w) >= 4]
    return words[:2] if words else []


client = Client()
client.force_login(User.objects.get(username="admin"))

summary = []
for appid in APPIDS:
    item = Item.objects.get(media_id=appid, media_type="game")
    url = f"/details/steam/game/{appid}/{item.title}"
    resp = client.get(url)
    html = resp.content.decode("utf-8", "replace") if resp.status_code == 200 else ""
    print(f"== appid {appid}  {item.title!r}  -> HTTP {resp.status_code}")

    rows = {}
    rows["synopsis"] = bool(item.synopsis) and bool(visible(item.synopsis)) and all(
        w in html for w in visible(item.synopsis)
    )
    rows["genres"] = bool(item.genres) and all(g in html for g in item.genres)
    rows["release_date"] = item.release_date is not None and any(
        fmt in html
        for fmt in (
            item.release_date.isoformat(),
            f"{item.release_date.year} 年 {item.release_date.month} 月 {item.release_date.day} 日",
            item.release_date.strftime("%Y-%m-%d"),
        )
    )
    rows["title_zh"] = bool(item.title_zh) and item.title_zh in html
    rows["title_en"] = bool(item.title_en) and item.title_en in html

    for key, ok in rows.items():
        mark = "显示" if ok else ("无数据" if not getattr(item, key if key != "release_date" else "release_date") else "数据在库但未显示")
        print(f"   {key:<13} {'✓' if ok else '✗'}  ({mark})")
    summary.append((appid, item.title, rows))

print("\n=== 汇总：字段是否在详情页展示 ===")
for key in ("synopsis", "genres", "release_date", "title_zh", "title_en"):
    hits = sum(1 for _, _, r in summary if r[key])
    print(f"{key:<13} 命中 {hits}/5")
