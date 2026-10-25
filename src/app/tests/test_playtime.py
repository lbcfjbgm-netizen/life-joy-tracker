"""月度游玩时长快照与聚合的测试。

所有日期都相对「当前月」算，测试不会因为系统日期变化而失败。
统计窗口的上界是当前月（采集脚本也只写当月 1 号），所以用例里
「观察月」一律用当前月，基线用上一个月。
"""

import datetime

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase

from app.models import Game, Item, MediaTypes, PlaytimeSnapshot, Sources
from app.playtime import get_monthly_playtime, month_start, shift_month

User = get_user_model()

THIS_MONTH = month_start()
PREV_MONTH = shift_month(THIS_MONTH, -1)
PREV2_MONTH = shift_month(THIS_MONTH, -2)


def label(day):
    """把某月 1 号转成 "YYYY-MM"。"""
    return day.strftime("%Y-%m")


class MonthlyPlaytimeTests(TestCase):
    """get_monthly_playtime 的差值计算与「没有数据」标记。"""

    def setUp(self):
        """建一个用户和两个游戏。"""
        self.user = User.objects.create_user(username="snap", password="x")
        self.other = User.objects.create_user(username="other", password="x")
        self.item_a = Item.objects.create(
            media_id="10",
            source=Sources.STEAM.value,
            media_type=MediaTypes.GAME.value,
            title="Game A",
        )
        self.item_b = Item.objects.create(
            media_id="20",
            source=Sources.STEAM.value,
            media_type=MediaTypes.GAME.value,
            title="Game B",
        )
        self.game_a = Game.objects.create(
            user=self.user,
            item=self.item_a,
            progress=100,
            status="In progress",
        )
        self.game_b = Game.objects.create(
            user=self.user,
            item=self.item_b,
            progress=0,
            achievements_total=10,
            status="In progress",
        )

    def snapshot(self, game, when, minutes, user=None):
        """写一条快照。"""
        return PlaytimeSnapshot.objects.create(
            user=user or self.user,
            game=game,
            captured_on=when,
            playtime_minutes=minutes,
        )

    def test_only_baseline_has_no_data(self):
        """只有一个月基线时，全部为 0 并且 has_data=False。"""
        self.snapshot(self.game_a, THIS_MONTH, 600)

        result = get_monthly_playtime(self.user, months=3)

        self.assertEqual(
            result["labels"],
            [label(PREV2_MONTH), label(PREV_MONTH), label(THIS_MONTH)],
        )
        self.assertEqual(result["datasets"][0]["data"], [0.0, 0.0, 0.0])
        self.assertEqual(result["datasets"][1]["data"], [0, 0, 0])
        self.assertIs(result["has_data"], False)
        self.assertIsNone(result["data_start"])

    def test_month_delta_becomes_hours(self):
        """相邻两个月的差值就是当月时长，并换算成小时。"""
        self.snapshot(self.game_a, PREV_MONTH, 300)
        self.snapshot(self.game_a, THIS_MONTH, 600)

        result = get_monthly_playtime(self.user, months=2)

        self.assertEqual(result["labels"], [label(PREV_MONTH), label(THIS_MONTH)])
        # 上个月只有基线，差值算不出来；本月多出 300 分钟 = 5.0 小时。
        self.assertEqual(result["datasets"][0]["data"], [0.0, 5.0])
        self.assertEqual(result["datasets"][1]["data"], [0, 1])
        self.assertIs(result["has_data"], True)
        self.assertEqual(result["data_start"], label(THIS_MONTH))

    def test_unchanged_game_not_counted(self):
        """时长没变的游戏不计入「玩过的游戏数」。"""
        self.snapshot(self.game_a, PREV_MONTH, 300)
        self.snapshot(self.game_a, THIS_MONTH, 300)
        self.snapshot(self.game_b, PREV_MONTH, 0)
        self.snapshot(self.game_b, THIS_MONTH, 120)

        result = get_monthly_playtime(self.user, months=1)

        self.assertEqual(result["datasets"][1]["data"], [1])
        self.assertEqual(result["datasets"][0]["data"], [2.0])

    def test_gap_month_is_skipped_but_playtime_kept(self):
        """中间月份没留快照时，差值归到观察到变化的那个月，总量不丢。"""
        self.snapshot(self.game_a, PREV2_MONTH, 0)
        self.snapshot(self.game_a, THIS_MONTH, 180)

        result = get_monthly_playtime(self.user, months=3)

        self.assertEqual(
            result["labels"],
            [label(PREV2_MONTH), label(PREV_MONTH), label(THIS_MONTH)],
        )
        self.assertEqual(result["datasets"][0]["data"], [0.0, 0.0, 3.0])

    def test_decrease_is_not_negative(self):
        """时长倒退（Steam 重置等）不会拉低整个月的数。"""
        self.snapshot(self.game_a, PREV_MONTH, 600)
        self.snapshot(self.game_a, THIS_MONTH, 100)

        result = get_monthly_playtime(self.user, months=1)

        self.assertEqual(result["datasets"][0]["data"], [0.0])
        self.assertEqual(result["datasets"][1]["data"], [0])

    def test_other_user_isolation(self):
        """别的用户的快照不会串进来。"""
        self.snapshot(self.game_a, PREV_MONTH, 0, user=self.other)
        self.snapshot(self.game_a, THIS_MONTH, 600, user=self.other)

        result = get_monthly_playtime(self.user, months=2)

        self.assertEqual(result["datasets"][0]["data"], [0.0, 0.0])
        self.assertIs(result["has_data"], False)

    def test_months_length(self):
        """months 参数决定柱子数量。"""
        result = get_monthly_playtime(self.user, months=6)

        self.assertEqual(len(result["labels"]), 6)
        self.assertEqual(len(result["datasets"]), 2)
        self.assertEqual(len(result["datasets"][0]["data"]), 6)
        self.assertEqual(
            [ds["background_color"] for ds in result["datasets"]],
            ["#6366f1", "#22c55e"],
        )


class PlaytimeSnapshotModelTests(TestCase):
    """快照表的唯一约束和幂等写入。"""

    def setUp(self):
        """建用户和游戏。"""
        self.user = User.objects.create_user(username="snap2", password="x")
        item = Item.objects.create(
            media_id="30",
            source=Sources.STEAM.value,
            media_type=MediaTypes.GAME.value,
            title="Game C",
        )
        self.game = Game.objects.create(
            user=self.user,
            item=item,
            progress=60,
            status="In progress",
        )

    def test_unique_per_user_game_month(self):
        """同一用户同一游戏同一个月只能有一条。"""
        PlaytimeSnapshot.objects.create(
            user=self.user,
            game=self.game,
            captured_on=THIS_MONTH,
            playtime_minutes=60,
        )

        with self.assertRaises(IntegrityError):
            PlaytimeSnapshot.objects.create(
                user=self.user,
                game=self.game,
                captured_on=THIS_MONTH,
                playtime_minutes=90,
            )

    def test_update_or_create_is_idempotent(self):
        """update_or_create 重跑不产生重复行。"""
        for minutes in (60, 60, 90):
            PlaytimeSnapshot.objects.update_or_create(
                user=self.user,
                game=self.game,
                captured_on=THIS_MONTH,
                defaults={"playtime_minutes": minutes},
            )

        self.assertEqual(
            PlaytimeSnapshot.objects.filter(user=self.user, game=self.game).count(),
            1,
        )
        row = PlaytimeSnapshot.objects.get(user=self.user, game=self.game)
        self.assertEqual(row.playtime_minutes, 90)


class MonthHelperTests(TestCase):
    """月份工具的边界行为。"""

    def test_shift_month_across_year(self):
        """跨年移动月份。"""
        self.assertEqual(
            shift_month(datetime.date(2026, 1, 1), -1),
            datetime.date(2025, 12, 1),
        )
        self.assertEqual(
            shift_month(datetime.date(2026, 12, 1), 1),
            datetime.date(2027, 1, 1),
        )

    def test_month_start(self):
        """任意日期都归到当月 1 号。"""
        self.assertEqual(month_start(datetime.date(2026, 3, 17)), datetime.date(2026, 3, 1))
