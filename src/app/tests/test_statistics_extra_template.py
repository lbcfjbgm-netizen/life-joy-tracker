"""渲染统计页模板，确认新增卡片和 json_script 不会让页面报错。

不启动站点、不改视图：直接用 Django 模板渲染 + 一个假的 context，
把 ``extra_charts`` 这段接线（views.py 由主对话负责）单独验一遍。
"""

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase

from app import statistics

User = get_user_model()


class StatisticsTemplateRenderTests(TestCase):
    """模板必须能在「有数据」和「没数据」两种情况下都渲染出来。"""

    def setUp(self):
        """base.html 要用 request.user（侧栏），所以造一个登录态请求。"""
        self.user = User.objects.create_user(
            username="templateuser",
            password="templatepass",
        )
        self.request = RequestFactory().get("/statistics")
        self.request.user = self.user

    def _minimal_context(self, extra_charts):
        return {
            "media_count": {"total": 0},
            "media_type_distribution": {"labels": [], "datasets": []},
            "status_distribution": {"labels": [], "datasets": [], "total_completed": 0},
            "status_pie_chart_data": {"labels": [], "datasets": []},
            "score_distribution": {
                "labels": [],
                "datasets": [],
                "average_score": None,
                "total_scored": 0,
            },
            "consumption_stats": [],
            "in_progress_count": 0,
            "rated_percent": None,
            "top_rated": [],
            "date_format_values": ["%Y-%m-%d"],
            "extra_charts": extra_charts,
        }

    def test_renders_with_data(self):
        """有数据时四张图的 canvas 和 json_script 都要出现。"""
        extra_charts = {
            "yearly_completed": statistics._empty_extra_chart(),
            "playtime_top": {
                "labels": ["A Game"],
                "datasets": [
                    {"label": "小时", "data": [12.5], "background_color": "#eab308"},
                ],
            },
            "achievement_buckets": {
                "labels": ["全成就", "部分完成", "无成就数据"],
                "datasets": [
                    {"label": "游戏数", "data": [1, 2, 3], "background_color": "#6366f1"},
                ],
            },
            "release_years": statistics._empty_extra_chart(),
        }
        html = render_to_string(
            "app/statistics.html",
            self._minimal_context(extra_charts),
            request=self.request,
        )

        self.assertIn('id="playtimeChart"', html)
        self.assertIn('id="achievementChart"', html)
        self.assertIn('id="extra_charts"', html)
        # 没数据的两张走空状态，不该有 canvas
        self.assertNotIn('id="yearlyCompletedChart"', html)
        self.assertNotIn('id="releaseYearChart"', html)
        self.assertIn("还没有完成记录", html)

    def test_renders_when_extra_charts_missing(self):
        """视图还没接线（context 里没有 extra_charts）时也不能炸。"""
        html = render_to_string(
            "app/statistics.html",
            self._minimal_context(None),
            request=self.request,
        )
        self.assertIn('id="extra_charts"', html)
        # default_if_none 兜住 None，json_script 仍然输出合法 JSON（空串是一个
        # 合法的 JSON 字符串；前端 parse 后不是对象就不初始化图表）
        start = html.index('id="extra_charts"')
        body = html[start:].split(">", 1)[1].split("</script>", 1)[0].strip()
        self.assertIn(body, ("", '""'))
        self.assertNotIn("None", body)

    def test_renders_with_empty_chart_dict(self):
        """四张图全空时四块空状态都出现。"""
        empty = statistics._empty_extra_chart()
        extra_charts = {
            "yearly_completed": dict(empty),
            "playtime_top": dict(empty),
            "achievement_buckets": dict(empty),
            "release_years": dict(empty),
        }
        html = render_to_string(
            "app/statistics.html",
            self._minimal_context(extra_charts),
            request=self.request,
        )
        for title in ("还没有完成记录", "还没有游玩时长", "还没有成就数据", "还没有发行日期"):
            self.assertIn(title, html)
