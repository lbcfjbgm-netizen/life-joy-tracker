"""补测：详情页上按钮出现/不出现的条件。

跑法：cd src && ..\.venv\Scripts\python.exe ..\.tools\e2e_sync_ach_detail.py
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

from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402

from app.models import Game, Item, MediaTypes, Sources  # noqa: E402

User = get_user_model()
user = User.objects.get(username="admin")
client = Client()
client.force_login(user)

FAKE_META = {
    "title": "占位",
    "image": "",
    "media_id": "x",
    "media_type": "game",
    "source": "steam",
    "synopsis": "",
    "genres": [],
}


def debug_page(item, label, expect_button):
    url = reverse(
        "media_details",
        kwargs={
            "source": item.source,
            "media_type": item.media_type,
            "media_id": item.media_id,
            "title": "x",
        },
    )
    with mock.patch("app.providers.services.get_media_metadata", return_value=dict(FAKE_META)):
        resp = client.get(url)
    html = resp.content.decode("utf-8")
    has = "刷新成就" in html
    ok = "OK" if has == expect_button else "!!! 不符 !!!"
    print(f"  {label}: item={item.id} media_id={item.media_id} -> HTTP {resp.status_code} 有按钮={has} 期望={expect_button} {ok}")
    return html


print("\n== 详情页按钮显示条件 ==")

steam_item = Item.objects.filter(
    source=Sources.STEAM.value, media_type=MediaTypes.GAME.value, media_id="4267800"
).first()
debug_page(steam_item, "Steam 游戏(纯数字)", True)

uuid_item = (
    Item.objects.filter(source=Sources.MANUAL.value, media_type=MediaTypes.GAME.value)
    .exclude(media_id__regex=r"^\d+$")
    .first()
)
debug_page(uuid_item, "手写条目(uuid)", False)

# 临时造一个 manual + 纯数字的条目来验证第三条显示条件
tmp_item = None
try:
    tmp_item, _ = Item.objects.get_or_create(
        media_id="220",
        source=Sources.MANUAL.value,
        media_type=MediaTypes.GAME.value,
        season_number=None,
        episode_number=None,
        defaults={"title": "临时测试条目（手工+数字appid）", "image": ""},
    )
    print(f"  已建临时条目 item={tmp_item.id}")
    # 详情页模板第 81 行会对 current_instance.status 调 status_button_color，
    # 没追踪的记录 status 是空串会抛 KeyError，所以先补一条 Game 行（真实用户
    # 也是先「开始追踪」才会看到卡片和按钮）
    Game.objects.get_or_create(
        user=user,
        item=tmp_item,
        defaults={"status": "In progress"},
    )
    html = debug_page(tmp_item, "手写条目(纯数字220)", True)

    print("\n== 临时条目点同步按钮 ==")
    url = reverse(
        "sync_game_achievements",
        kwargs={"source": "manual", "media_type": "game", "media_id": "220"},
    )
    import copy
    from unittest import mock as _mock

    from django.core.cache import cache as _cache

    # 上一次跑留下的节流键会挡住这次，先清掉
    _cache.delete(f"achievements_sync_{user.id}_220")

    fake = [{"achieved": 1}] * 3 + [{"achieved": 0}] * 7
    with _mock.patch(
        "integrations.imports.steam_achievements._fetch_achievements",
        return_value=copy.deepcopy(fake),
    ) as fetch:
        resp = client.post(url)
    print(f"  HTTP {resp.status_code}，_fetch_achievements 调用次数={fetch.call_count}")
    body = resp.content.decode("utf-8")
    print("  卡片里含 '/ 10 已解锁'：", "/ 10 已解锁" in body)
    g = Game.objects.filter(user=user, item=tmp_item).first()
    print(f"  同步后 total={g.achievements_total} unlocked={g.achievements_unlocked}")
finally:
    print("\n== 清理 ==")
    if tmp_item is not None:
        Game.objects.filter(item=tmp_item).delete()
        tmp_item.delete()
        exists = Item.objects.filter(media_id="220", source=Sources.MANUAL.value).exists()
        print(f"  临时条目已删除，还在吗：{exists}")
