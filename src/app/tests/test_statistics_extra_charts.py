"""四张新图表（get_extra_charts）的回归测试。

只测新函数，不动任何已有测试。断言用中文标签，因为这几张图的标签是直接写死的
中文（项目约定：新增文案不加 {% trans %}）。
"""

import datetime
from decimal import Decimal

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from app import statistics
from app.templatetags import app_tags
from app.models import (
    Book,
    Game,
    Item,
    MediaTypes,
    Movie,
    Sources,
    Status,
)

User = get_user_model()


class ExtraChartsTests(TestCase):
    """验证 get_extra_charts 的四张图口径与空状态结构。"""

    def setUp(self):
        """建一个用户和四种媒体类型的样本数据。"""
        # 建 Completed 条目时 Media.process_status 会向 provider 要 max_progress，
        # 还会让 Item 去抓日历；这两处在本机都走网络，会超时。图的口径和它们无关，
        # 直接掐掉，测试只测数据库聚合。
        patcher_metadata = patch(
            "app.providers.services.get_media_metadata",
            return_value={"max_progress": None},
        )
        patcher_releases = patch.object(
            Item,
            "fetch_releases",
            lambda self, delay: None,
        )
        patcher_metadata.start()
        patcher_releases.start()
        self.addCleanup(patcher_metadata.stop)
        self.addCleanup(patcher_releases.stop)

        cache.clear()
        self.user = User.objects.create_user(
            username="chartuser",
            password="chartpass",
        )

        game_titles = {
            "long": ("100", 6000),
            "short": ("101", 600),
            "no_achievements": ("102", None),
        }
        self.games = {}
        for key, (media_id, minutes) in game_titles.items():
            item = Item.objects.create(
                media_id=media_id,
                source=Sources.STEAM.value,
                media_type=MediaTypes.GAME.value,
                title=f"Game {key}",
                release_date=datetime.date(2015, 5, 1),
            )
            game = Game.objects.create(
                item=item,
                user=self.user,
                status=Status.COMPLETED.value,
                progress=minutes or 0,
                end_date=timezone.make_aware(datetime.datetime(2024, 6, 1)),
            )
            self.games[key] = game

        # 全成就：解锁数 >= 总数
        self.games["long"].achievements_total = 10
        self.games["long"].achievements_unlocked = 10
        self.games["long"].save()

        # 部分完成
        self.games["short"].achievements_total = 20
        self.games["short"].achievements_unlocked = 7
        self.games["short"].save()

        # 无成就数据：achievements_total 保持 0

        # 电影：另一年完成，用来验证堆叠分组 + 发行年份跨媒体类型
        movie_item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="A Movie",
            release_date=datetime.date(1985, 3, 31),
        )
        Movie.objects.create(
            item=movie_item,
            user=self.user,
            status=Status.COMPLETED.value,
            end_date=timezone.make_aware(datetime.datetime(2023, 2, 1)),
        )

        # 进行中的游戏：不该出现在年度完成趋势里
        in_progress_item = Item.objects.create(
            media_id="900",
            source=Sources.STEAM.value,
            media_type=MediaTypes.GAME.value,
            title="Game in progress",
            release_date=datetime.date(2025, 1, 1),
        )
        Game.objects.create(
            item=in_progress_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=120,
            # 放在 2022：如果「进行中」也被算进趋势，2022 会多出一个 label
            end_date=timezone.make_aware(datetime.datetime(2022, 7, 1)),
        )

    def _get_charts(self):
        user_media, _ = statistics.get_user_media(self.user, None, None)
        return statistics.get_extra_charts(self.user, user_media, None, None)

    def test_yearly_completed_groups_by_year_and_media_type(self):
        """年度完成趋势：按 end_date 年份堆叠，只算已完成。"""
        chart = self._get_charts()["yearly_completed"]

        self.assertEqual(chart["labels"], ["2023", "2024"])
        movie_label = app_tags.media_type_readable(MediaTypes.MOVIE.value)
        game_label = app_tags.media_type_readable(MediaTypes.GAME.value)
        labels = [dataset["label"] for dataset in chart["datasets"]]
        self.assertIn(movie_label, labels)
        self.assertIn(game_label, labels)

        by_label = {
            dataset["label"]: dataset["data"] for dataset in chart["datasets"]
        }
        # 三条已完成的游戏都在 2024；2022 那条是进行中，不该出现
        self.assertEqual(by_label[game_label], [0, 3])
        self.assertEqual(by_label[movie_label], [1, 0])

    def test_playtime_top_is_minutes_to_hours_sorted(self):
        """游玩时长 Top 10：分钟换小时（1 位小数），降序，只取 10 条。"""
        chart = self._get_charts()["playtime_top"]

        self.assertEqual(chart["datasets"][0]["label"], "小时")
        data = chart["datasets"][0]["data"]
        self.assertEqual(data, sorted(data, reverse=True))
        # 6000 分钟 = 100.0 小时，600 分钟 = 10.0 小时
        self.assertEqual(data[:2], [100.0, 10.0])
        self.assertLessEqual(len(chart["labels"]), 10)

    def test_achievement_buckets_split_three_ways(self):
        """成就：全成就 / 部分完成 / 无成就数据 三桶计数。"""
        chart = self._get_charts()["achievement_buckets"]

        self.assertEqual(chart["labels"], ["全成就", "部分完成", "无成就数据"])
        self.assertEqual(chart["datasets"][0]["data"], [1, 1, 2])

    def test_release_years_merges_to_decades_when_span_over_30(self):
        """发行年份跨度超过 30 年时按年代合并。"""
        chart = self._get_charts()["release_years"]

        # 1985 到 2025 跨 40 年，超过 30 年阈值，按年代合并
        self.assertEqual(chart["labels"], ["1980s", "2010s", "2020s"])
        self.assertEqual(chart["datasets"][0]["data"], [1, 3, 1])

    def test_empty_library_returns_empty_structure(self):
        """没有数据时四个键都必须是 labels/datasets 的空结构。"""
        empty_user = User.objects.create_user(
            username="emptyuser",
            password="emptypass",
        )
        user_media, _ = statistics.get_user_media(empty_user, None, None)
        charts = statistics.get_extra_charts(empty_user, user_media, None, None)

        for key in (
            "yearly_completed",
            "playtime_top",
            "achievement_buckets",
            "release_years",
        ):
            self.assertEqual(charts[key], {"labels": [], "datasets": []})

    def test_result_is_cacheable_and_cached(self):
        """结果只含纯数据，能进缓存；第二次调用命中缓存。"""
        cache.clear()
        first = self._get_charts()
        key = statistics._extra_charts_cache_key(self.user, None, None)
        self.assertIsNotNone(cache.get(key))

        second = self._get_charts()
        self.assertEqual(first, second)

    def test_media_without_end_date_is_skipped(self):
        """TV / Season 没有 end_date 字段，不能因此把整页搞崩。"""
        tv_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="A Show",
        )
        from app.models import TV

        TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.COMPLETED.value,
        )

        chart = self._get_charts()["yearly_completed"]
        self.assertNotIn("剧集", [ds["label"] for ds in chart["datasets"]])


class ExtraChartsReleaseYearBucketTests(TestCase):
    """_release_year_buckets 的边界：跨度 30 年内保持单年。"""

    def test_keeps_single_years_within_30_years(self):
        """跨度不超过 30 年时用单一年份。"""
        labels, data = statistics._release_year_buckets({1990: 2, 2010: 3})
        self.assertEqual(labels, ["1990", "2010"])
        self.assertEqual(data, [2, 3])

    def test_merges_to_decades_beyond_30_years(self):
        """跨度超过 30 年时合并成年代，升序。"""
        labels, data = statistics._release_year_buckets({1985: 1, 2024: 5})
        self.assertEqual(labels, ["1980s", "2020s"])
        self.assertEqual(data, [1, 5])
