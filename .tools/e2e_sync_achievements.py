"""验证「刷新成就」按钮的四条路径（不碰 8001 活站，纯 test client）。

跑法：cd src && ..\.venv\Scripts\python.exe ..\.tools\e2e_sync_achievements.py
"""

import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + r"\src")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from unittest import mock  # noqa: E402

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client, override_settings  # noqa: E402
from django.urls import reverse  # noqa: E402

from app.models import Game, Item, MediaTypes, Sources  # noqa: E402

User = get_user_model()
user = User.objects.get(username="admin")


def header(text):
    print("\n" + "=" * 70)
    print(text)
    print("=" * 70)


def card_of(html):
    """从整页 HTML 里抠出成就卡那一段。"""
    start = html.find('id="achievement-card"')
    if start < 0:
        return "<没找到卡片>"
    start = html.rfind("<div", 0, start)
    end = html.find("</div>\n", html.find("m-sync-note", start) if "m-sync-note" in html[start:] else start)
    return html[start : start + 2000]


def show(html, label):
    has_button = "刷新成就" in html
    has_url = "sync-achievements" in html
    print(f"  {label}: 有按钮={has_button} 有同步URL={has_url}")

    for key in ["无成就清单", "已解锁", "m-sync-note"]:
        if key in html:
            idx = html.find(key)
            snippet = html[max(0, idx - 80) : idx + 120].replace("\n", " ")
            print(f"  {label}: 命中 {key!r} -> ...{snippet}...")


# ---------------------------------------------------------------- 路径 a
header("a. Steam 游戏 appid 4267800 点按钮 → 成功 + 卡片数字正确")

steam_game = (
    Game.objects.filter(user=user, item__source=Sources.STEAM.value, item__media_id="4267800")
    .select_related("item")
    .first()
)
print(f"  库里这条：game={steam_game.id} item={steam_game.item.id} title={steam_game.item.title}")
print(f"  同步前：total={steam_game.achievements_total} unlocked={steam_game.achievements_unlocked}")

url = reverse(
    "sync_game_achievements",
    kwargs={
        "source": steam_game.item.source,
        "media_type": steam_game.item.media_type,
        "media_id": steam_game.item.media_id,
    },
)
print(f"  路由：{url}")

client = Client()
client.force_login(user)

# 只 mock 掉真正打 Steam 那一步，验证视图→同步函数→回显的整条链
with mock.patch(
    "integrations.imports.steam_achievements._fetch_achievements",
    return_value=[{"achieved": 1}] * 59 + [{"achieved": 0}] * 3,
) as fetch:
    resp = client.post(url)
    print(f"  HTTP {resp.status_code}，_fetch_achievements 调用次数={fetch.call_count}")
    html = resp.content.decode("utf-8")
    show(html, "a")

steam_game.refresh_from_db()
print(f"  同步后：total={steam_game.achievements_total} unlocked={steam_game.achievements_unlocked}")
print(f"  卡片里应有 '59' 和 '/ 62 已解锁'：", ("59" in html) and ("/ 62 已解锁" in html))

# ---------------------------------------------------------------- 路径 b
header("b. 手写条目（media_id 是 uuid）→ 路由被拒 + 详情页无按钮")

manual_uuid = (
    Item.objects.filter(source=Sources.MANUAL.value, media_type=MediaTypes.GAME.value)
    .exclude(media_id__regex=r"^\d+$")
    .first()
)
print(f"  挑中的手写条目：item={manual_uuid.id} title={manual_uuid.title} media_id={manual_uuid.media_id}")

# 它没有 Game 行就取不到 → 这里给一个必然 404 的组合
url_b = reverse(
    "sync_game_achievements",
    kwargs={"source": "manual", "media_type": "game", "media_id": manual_uuid.media_id},
)
resp = client.post(url_b)
print(f"  路由：{url_b} → HTTP {resp.status_code}（期望 404）")

# 详情页渲染：用 media_details 的模板上下文直接看按钮
from app import achievements as ach  # noqa: E402

print(f"  steam_appid_of(uuid item) = {ach.steam_appid_of(manual_uuid)!r}（期望 None）")

# ---------------------------------------------------------------- 路径 c
header("c. 未登录访问 → 被拒（不是 500）")

# .env 里配了 YAMTRACK_AUTO_LOGIN_USERNAME，中间件是在 settings 里静态插的，
# 所以要临时把这条中间件从列表里摘掉才测得出真正的匿名请求。
mw_without_autologin = [
    m for m in settings.MIDDLEWARE if m != "app.middleware.AutoLoginMiddleware"
]
with override_settings(MIDDLEWARE=mw_without_autologin):
    anon = Client()
    resp = anon.post(url)
    print(f"  匿名 POST {url} → HTTP {resp.status_code}（期望 302 或 403，不能是 500）")
    print(f"  Location: {resp.headers.get('Location')}")

# ---------------------------------------------------------------- 路径 d
header("d. 30 秒内连点两次 → 第二次被节流且没有再请求 Steam")

from django.core.cache import cache  # noqa: E402

cache.delete(f"achievements_sync_{user.id}_4267800")

with mock.patch(
    "integrations.imports.steam_achievements._fetch_achievements",
    return_value=[{"achieved": 1}] * 59 + [{"achieved": 0}] * 3,
) as fetch:
    r1 = client.post(url)
    r2 = client.post(url)
    print(f"  第 1 次 HTTP {r1.status_code}，第 2 次 HTTP {r2.status_code}")
    print(f"  _fetch_achievements 总调用次数={fetch.call_count}（期望 1，第二次被节流）")

html2 = r2.content.decode("utf-8")
if "m-sync-note" in html2:
    idx = html2.find("m-sync-note")
    print("  第二次卡片里的提示：", html2[idx : idx + 120].replace("\n", " "))
