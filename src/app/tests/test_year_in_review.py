"""Tests for the year-in-review page.

年度游玩报告的测试。

两条线：聚合函数（app/year_in_review.py）算得对不对，以及页面在各种年份、
空数据、脏参数下都必须 200 且不炸。
"""

import datetime

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app import year_in_review as yir
from app.models import Game, Item, MediaTypes, Sources, Status


class YearInReviewAggregateTests(TestCase):
    """Test the aggregation helpers directly."""

    def setUp(self):
        """Create a user with a small, hand-checkable game library."""
        self.user = get_user_model().objects.create_user(
            username="yir",
            password="12345",  # noqa: S106 - test fixture
        )
        self.year = 2025

    def _game(  # noqa: PLR0913 - a test fixture with explicit knobs
        self,
        media_id,
        *,
        minutes=0,
        end_year=None,
        release_year=None,
        genres=None,
        status=Status.COMPLETED.value,
        synced_year=None,
        achievements_total=0,
        achievements_unlocked=0,
        all_achievements=False,
    ):
        item = Item.objects.create(
            media_id=media_id,
            source=Sources.STEAM.value,
            media_type=MediaTypes.GAME.value,
            title=media_id,
            image="https://media.steampowered.com/x.jpg",
            genres=genres or [],
            release_date=datetime.date(release_year, 1, 1) if release_year else None,
        )
        end_date = None
        if end_year:
            end_date = datetime.datetime(
                end_year, 6, 1, 12, 0,
                tzinfo=timezone.get_current_timezone(),
            )
        synced = None
        if synced_year:
            synced = datetime.datetime(
                synced_year, 7, 1, 12, 0,
                tzinfo=timezone.get_current_timezone(),
            )
        return Game.objects.create(
            user=self.user,
            item=item,
            progress=minutes,
            status=status,
            end_date=end_date,
            achievements_total=achievements_total,
            achievements_unlocked=achievements_unlocked,
            achievements_synced_at=synced,
            is_all_achievements=all_achievements,
        )

    def test_totals_only_count_the_selected_year(self):
        """Games from another year must not leak into the totals.

        注意：``total_minutes`` 是这批游戏的**累计**分钟数（库里没有按年
        切分的记录），这里只验证「别的年份的游戏不混进来」这一点。
        """
        self._game("a", minutes=600, end_year=2025)  # 10h
        self._game("b", minutes=120, end_year=2024)
        self._game("c", minutes=60, end_year=None)

        report = yir.build_year_in_review(self.user.id, 2025)

        self.assertTrue(report["has_data"])
        self.assertEqual(report["total_minutes"], 600)
        self.assertEqual(report["total_hours"], 10)
        self.assertEqual(report["game_count"], 1)

    def test_no_per_year_playtime_claims(self):
        """The payload must not pretend it knows the year's playtime.

        「相当于连续不睡 X 天」/「平均每天 X 分钟」都是把累计时长当成一年
        来摊，一年最多 8,760 小时，摊出来必然是假数，所以这两个键必须消失。
        """
        self._game("a", minutes=600, end_year=2025)

        report = yir.build_year_in_review(self.user.id, 2025)

        self.assertNotIn("sleepless_days", report)
        self.assertNotIn("minutes_per_day", report)

    def test_library_totals_are_the_cumulative_backdrop(self):
        """The library block is whole-library cumulative, used as a denominator."""
        self._game("a", minutes=600, end_year=2025)  # counted in the cohort
        self._game("b", minutes=400, end_year=2024)  # other year, library only
        self._game("c", minutes=0, end_year=None)

        report = yir.build_year_in_review(self.user.id, 2025)
        library = report["library"]

        self.assertEqual(library["minutes"], 1000)
        self.assertEqual(library["hours"], 16.7)
        self.assertEqual(library["games"], 3)
        # 600 / 1000，两个数同口径（都是累计），这个占比才有意义
        self.assertEqual(library["share"], 60)

    def test_library_share_is_zero_without_any_playtime(self):
        """An empty library must not divide by zero."""
        self._game("a", minutes=0, end_year=2025)

        report = yir.build_year_in_review(self.user.id, 2025)

        self.assertEqual(report["library"]["minutes"], 0)
        self.assertEqual(report["library"]["share"], 0)

    def test_top_games_sorted_and_share_computed(self):
        """Top games come back by playtime with a share of the year."""
        self._game("big", minutes=900, end_year=2025)
        self._game("mid", minutes=300, end_year=2025)

        report = yir.build_year_in_review(self.user.id, 2025)

        self.assertEqual([g["title"] for g in report["top_games"]], ["big", "mid"])
        self.assertEqual(report["top_games"][0]["hours"], 15)
        self.assertEqual(report["top_games"][0]["percent"], 75)
        self.assertEqual(report["top_games"][1]["percent"], 25)

    def test_top_games_capped_at_five(self):
        """More than five games are truncated, not all returned."""
        for index in range(8):
            self._game(f"g{index}", minutes=100 + index, end_year=2025)

        report = yir.build_year_in_review(self.user.id, 2025)

        self.assertEqual(len(report["top_games"]), yir.TOP_GAMES)

    def test_completed_requires_completed_status(self):
        """Only Completed rows with an end date count as finished that year."""
        self._game("done", minutes=60, end_year=2025)
        self._game(
            "dropped",
            minutes=60,
            end_year=2025,
            status=Status.DROPPED.value,
        )

        report = yir.build_year_in_review(self.user.id, 2025)

        self.assertEqual(report["completed"]["count"], 1)
        self.assertEqual(report["completed"]["covers"][0]["title"], "done")

    def test_completed_wall_truncates_and_reports_hidden(self):
        """The cover wall keeps 12 and says how many were left out."""
        for index in range(15):
            self._game(f"done{index}", minutes=60, end_year=2025)

        report = yir.build_year_in_review(self.user.id, 2025)

        self.assertEqual(report["completed"]["count"], 15)
        self.assertEqual(len(report["completed"]["covers"]), yir.COVER_WALL)
        self.assertEqual(report["completed"]["hidden"], 3)

    def test_genres_flatten_json_and_sum_playtime(self):
        """A game with two genres contributes its minutes to both.

        占比的分母是全年总时长（1000 分钟），不是各类型之和 1600，
        否则一款游戏带 4 个类型时占比会加出 400%。
        """
        self._game("g1", minutes=600, end_year=2025, genres=["独立", "冒险"])
        self._game("g2", minutes=400, end_year=2025, genres=["独立"])

        report = yir.build_year_in_review(self.user.id, 2025)
        by_name = {g["name"]: g for g in report["genres"]}

        self.assertEqual(by_name["独立"]["minutes"], 1000)
        self.assertEqual(by_name["独立"]["percent"], 100)
        self.assertEqual(by_name["冒险"]["minutes"], 600)
        self.assertEqual(by_name["冒险"]["percent"], 60)
        self.assertEqual(report["total_minutes"], 1000)
        self.assertEqual(report["favorite"]["name"], "独立")

    def test_genres_capped_at_six_and_colored(self):
        """At most six genres are returned, each with a palette color."""
        for index in range(9):
            self._game(
                f"g{index}",
                minutes=100 - index,
                end_year=2025,
                genres=[f"类型{index}"],
            )

        report = yir.build_year_in_review(self.user.id, 2025)

        self.assertEqual(len(report["genres"]), yir.TOP_GENRES)
        for genre in report["genres"]:
            self.assertTrue(genre["color"].startswith("#"))

    def test_achievements_aggregate(self):
        """Achievement totals and the all-achievements count are summed."""
        self._game(
            "ach",
            minutes=60,
            end_year=2025,
            achievements_total=10,
            achievements_unlocked=4,
            all_achievements=True,
        )
        self._game(
            "ach2",
            minutes=60,
            end_year=2025,
            achievements_total=10,
            achievements_unlocked=6,
        )

        report = yir.build_year_in_review(self.user.id, 2025)
        stats = report["achievements"]

        self.assertEqual(stats["unlocked"], 10)
        self.assertEqual(stats["available"], 20)
        self.assertEqual(stats["percent"], 50)
        self.assertEqual(stats["perfect"], 1)
        self.assertEqual(stats["games_with_data"], 2)

    def test_game_age_buckets(self):
        """Release year decides the bucket: new / 1-2 / 3-4 / 5+."""
        self._game("new", minutes=600, end_year=2025, release_year=2025)
        self._game("recent", minutes=300, end_year=2025, release_year=2024)
        self._game("mid", minutes=180, end_year=2025, release_year=2022)
        self._game("old", minutes=60, end_year=2025, release_year=2010)
        self._game("nodate", minutes=60, end_year=2025)

        report = yir.build_year_in_review(self.user.id, 2025)
        buckets = {b["key"]: b for b in report["age"]["buckets"]}

        self.assertEqual(buckets["new"]["minutes"], 600)
        self.assertEqual(buckets["recent"]["minutes"], 300)
        self.assertEqual(buckets["mid"]["minutes"], 180)
        self.assertEqual(buckets["old"]["minutes"], 60)
        # 没发售日期的那款不进任何一档，但要单独报出来
        self.assertEqual(report["age"]["missing_games"], 1)
        self.assertEqual(report["age"]["known_minutes"], 1140)

    def test_empty_year_returns_friendly_payload(self):
        """A year with nothing tracked must not raise."""
        report = yir.build_year_in_review(self.user.id, 2019)

        self.assertFalse(report["has_data"])
        self.assertEqual(report["total_minutes"], 0)
        self.assertEqual(report["top_games"], [])
        self.assertEqual(report["completed"]["count"], 0)
        self.assertEqual(report["genres"], [])
        self.assertIsNone(report["favorite"])

    def test_payload_is_cacheable(self):
        """Everything in the payload is plain data, nothing model-shaped."""
        self._game("a", minutes=60, end_year=2025)
        report = yir.build_year_in_review(self.user.id, 2025)

        def check(value):
            self.assertNotIsInstance(value, Game)
            self.assertNotIsInstance(value, Item)
            if isinstance(value, dict):
                for item in value.values():
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)
            else:
                self.assertTrue(
                    value is None
                    or isinstance(value, (str, int, float, bool)),
                    f"unexpected type {type(value)}",
                )

        check(report)

    def test_cache_round_trip(self):
        """The second read comes from cache and is identical."""
        self._game("a", minutes=60, end_year=2025)
        cache.delete(yir.cache_key(self.user.id, 2025))

        cold = yir.get_year_in_review(self.user.id, 2025)
        warm = yir.get_year_in_review(self.user.id, 2025)

        self.assertEqual(cold, warm)
        self.assertEqual(cold["total_minutes"], 60)


class ResolveYearTests(TestCase):
    """Test the ?year= parser."""

    def setUp(self):
        """Remember the current year so the assertions stay stable."""
        self.current = timezone.localdate().year

    def test_missing_year_falls_back_to_current(self):
        """No parameter means this year."""
        self.assertEqual(yir.resolve_year(None), self.current)

    def test_valid_year_is_kept(self):
        """A real year passes through."""
        self.assertEqual(yir.resolve_year("2025"), 2025)

    def test_dirty_input_falls_back(self):
        """Non-numeric and out-of-range years never reach the query."""
        for raw in ("abc", "", "   ", -5, "1e9"):
            with self.subTest(raw=raw):
                self.assertEqual(yir.resolve_year(raw), self.current)

    def test_too_early_year_falls_back(self):
        """Years before 1970 are rejected."""
        self.assertEqual(yir.resolve_year("1899"), self.current)


class YearInReviewViewTests(TestCase):
    """Test the rendered page."""

    def setUp(self):
        """Log in and seed one year of data."""
        self.user = get_user_model().objects.create_user(
            username="yirview",
            password="12345",  # noqa: S106 - test fixture
        )
        self.client.force_login(self.user)
        item = Item.objects.create(
            media_id="1",
            source=Sources.STEAM.value,
            media_type=MediaTypes.GAME.value,
            title="测试游戏",
            image="https://media.steampowered.com/x.jpg",
            genres=["独立"],
            release_date=datetime.date(2025, 3, 1),
        )
        Game.objects.create(
            user=self.user,
            item=item,
            progress=120,
            status=Status.COMPLETED.value,
            end_date=datetime.datetime(
                2025, 5, 1, 12, 0,
                tzinfo=timezone.get_current_timezone(),
            ),
            achievements_total=8,
            achievements_unlocked=8,
            is_all_achievements=True,
        )
        cache.delete(yir.cache_key(self.user.id, 2025))

    def test_page_renders_for_year_with_data(self):
        """The report page returns 200 and shows the headline blocks."""
        response = self.client.get(reverse("year_in_review") + "?year=2025")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("年度游玩报告", body)
        self.assertIn("测试游戏", body)
        self.assertIn("你累计投入最多的类型是", body)

    def test_page_labels_playtime_as_cumulative(self):
        """The headline number must be labelled 累计, never 「今年玩了」.

        回归测试：2026 年那页曾显示 10,497 小时（超过一年 8,760 小时），
        因为把累计 progress 当成了年度时长。页面必须明写这是累计值。
        """
        response = self.client.get(reverse("year_in_review") + "?year=2025")
        body = response.content.decode()

        self.assertIn("游戏库累计时长", body)
        self.assertIn("从入库到现在的累计时长", body)
        self.assertIn("不是 2025 这一年玩的", body)
        self.assertNotIn("相当于连续不睡", body)
        self.assertNotIn("平均每天", body)
        self.assertNotIn("占全年时长", body)

    def test_page_states_why_playtime_is_cumulative(self):
        """The disclaimer must spell out the data limitation on the page."""
        response = self.client.get(reverse("year_in_review") + "?year=2025")

        self.assertIn(
            "数据库中只保存累计游玩时长，没有按年切分的历史记录，"
            "因此本页的时长是累计值，不代表这一年的游玩量。",
            response.content.decode(),
        )

    def test_page_renders_for_empty_year(self):
        """A year with no data gets the friendly empty state, not a 500."""
        response = self.client.get(reverse("year_in_review") + "?year=2019")

        self.assertEqual(response.status_code, 200)
        self.assertIn("一片空白", response.content.decode())

    def test_default_is_current_year(self):
        """Bare URL resolves to this year."""
        response = self.client.get(reverse("year_in_review"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["year"], timezone.localdate().year)

    def test_dirty_query_still_returns_200(self):
        """Garbage in ?year= must not explode."""
        for raw in ("abc", "99999999", "-1", ""):
            response = self.client.get(f"{reverse('year_in_review')}?year={raw}")
            self.assertEqual(response.status_code, 200, raw)

    def test_unknown_url_is_not_this_view(self):
        """Sanity: the route string is exactly /year-in-review."""
        self.assertEqual(reverse("year_in_review"), "/year-in-review")
