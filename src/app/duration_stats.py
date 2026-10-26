"""读书时长 / 观影时长聚合（只读，纯 ORM，不写库）。

这个模块只回答一个问题：**某个用户、某段时间里，读了多少小时书、看了多少小时影视**。
统计页后面从 :func:`get_reading_stats` 和 :func:`get_watch_stats` 取数字即可，
两个函数都只读不写，也不会产生任何 UPDATE / INSERT。

数据来源
--------
读书
    ``app_book.reading_seconds`` —— 微信读书导入的「实际阅读秒数」，整数，单位是**秒**。
    这是真实耗时，不是估算。只有 ``reading_seconds > 0`` 的行才有意义
    （微信读书没同步过的书、手填的书都停在 0）。
观影（电影）
    ``app_movie.runtime`` —— 片长，单位是**分钟**（不是秒）。实测取值范围 21~242。
    只有 ``status = Completed`` 且 ``runtime > 0`` 的电影才算「看过」。
观影（剧集）
    剧集时长是**估算**的，原因见下。

剧集为什么只能估
----------------
真正准确的时长字段链是：``app_tv`` / ``app_season`` / ``app_episode`` 三张表。
但 ``app_episode`` 只有 ``end_date``，**没有任何时长字段**；
``app_item`` 也没有 runtime（只有 total_pages / release_date / season_number /
episode_number 等）。所以「某一集多少分钟」这个事实在库里根本不存在，
无论怎么 join 都取不到。唯一能做的是：已看集数 × 每集估计分钟数
（默认 45 分钟）。因此 ``tv_hours`` 一定带误差，返回值里用
``tv_is_estimated = True`` 明确标出，避免统计页把估算值当实测值展示。

日期口径
--------
``app_book.end_date`` / ``app_movie.end_date`` / ``app_episode.end_date`` 在 Django
里都是 **DateTimeField**（``USE_TZ = True``，站点 ``TIME_ZONE = Asia/Shanghai``），
不是字符串。原始列在 SQLite 里存成 ``'2026-08-16 03:09:33'`` 这种文本，
但走 ORM 时它是带时区的 datetime，所以年份用 ``ExtractYear`` 取
（会自动按站点时区换算，不会出现跨年错位），日期区间用 ``end_date__date``
过滤 —— 这是在 SQLite 上也能正确工作的写法，**不要**用字符串切片去比。
"""

from __future__ import annotations

import logging

from django.db import models
from django.db.models.functions import ExtractYear
from django.utils import timezone

from app.models import Book, Episode, Movie, Status

logger = logging.getLogger(__name__)

SECONDS_PER_HOUR = 3600
MINUTES_PER_HOUR = 60

# 剧集没有真实单集时长，给一个可调的估计值（分钟）。
DEFAULT_TV_MINUTES_PER_EPISODE = 45


def _round_hours(total_minutes):
    """把分钟换算成小时，保留 1 位小数。"""
    return round(total_minutes / MINUTES_PER_HOUR, 1)


def _round_seconds_hours(total_seconds):
    """把秒换算成小时，保留 1 位小数。"""
    return round(total_seconds / SECONDS_PER_HOUR, 1)


def _year_key(year):
    """把 ExtractYear 的结果统一成字符串，None 也返回字符串。"""
    return str(year) if year is not None else ""


def _has_explicit_range(start_date, end_date):
    """判断调用方是不是显式给了日期区间。"""
    return start_date is not None or end_date is not None


def _apply_end_date_range(queryset, start_date, end_date):
    """按 ``end_date`` 过滤区间，只对非空 end_date 生效。

    ``end_date`` 是 DateTimeField，这里用 ``__date`` 查日期，SQLite 上也能跑；
    直接比 datetime 会漏掉当天的记录。
    """
    if start_date is not None:
        queryset = queryset.filter(end_date__date__gte=start_date)
    if end_date is not None:
        queryset = queryset.filter(end_date__date__lte=end_date)
    return queryset


def _empty_reading_stats():
    """没数据时返回的空结构（字段和正常返回完全一致）。"""
    return {
        "total_seconds": 0,
        "total_hours": 0.0,
        "book_count": 0,
        "by_year": [],
        "this_year_hours": 0.0,
        "this_year_books": 0,
        "unknown_date_hours": 0.0,
        "has_data": False,
    }


def _empty_watch_stats():
    """没数据时返回的空结构（字段和正常返回完全一致）。"""
    return {
        "movie_minutes": 0,
        "movie_hours": 0.0,
        "movie_count": 0,
        "tv_episodes": 0,
        "tv_minutes": 0,
        "tv_hours": 0.0,
        "tv_is_estimated": True,
        "total_hours": 0.0,
        "by_year": [],
        "has_data": False,
    }


def get_reading_stats(user, start_date=None, end_date=None):
    """汇总一个用户的读书时长。

    :param user: Django 用户；传 None（匿名访问路径）时返回空结构。
    :param start_date: ``datetime.date`` 或 None，按 ``end_date`` 的日期下限过滤。
    :param end_date: ``datetime.date`` 或 None，按 ``end_date`` 的日期上限过滤。
    :return: dict，字段见模块说明；``total_seconds`` 是真实秒数，
        ``total_hours`` 保留 1 位小数。

    口径说明：

    * 只统计 ``reading_seconds > 0`` 的书 —— 0 表示微信读书没同步过，不是「读了 0 秒」。
    * ``total_seconds`` / ``total_hours`` / ``book_count`` 是**全量**：不传区间时，
      没读完的书（``end_date`` 为空）也算在里面，所以 609.0h = 读完 507.4h +
      在读 101.5h，243 本 = 128 本 + 115 本。传了区间时只算区间内读完的，
      没读完的既不进总量也不进 ``unknown_date_hours``（那时它返回 0）。
    * ``by_year`` 只按 ``end_date`` 年份分组，所以各年相加等于「读完的部分」
      （507.4h），不等于 ``total_hours``；在读的那 101.5h 单独放在
      ``unknown_date_hours`` 里，这样统计页能分开展示。
    """
    if user is None:
        return _empty_reading_stats()

    base = Book.objects.filter(user=user, reading_seconds__gt=0)
    ranged = _has_explicit_range(start_date, end_date)

    # 读完的书：有 end_date，按区间过滤。
    dated = base.filter(end_date__isnull=False)
    dated = _apply_end_date_range(dated, start_date, end_date)

    # 总计的口径：没传区间 = 所有有时长记录的书（含还没读完的），
    # 这样 total_hours 才等于「Completed 507.4h + In progress 101.5h」。
    # 传了区间 = 只算区间内读完的，没读完的不进总量。
    total_qs = dated if ranged else base
    totals = total_qs.aggregate(
        total_seconds=models.Sum("reading_seconds"),
        book_count=models.Count("id"),
    )
    total_seconds = totals["total_seconds"] or 0
    book_count = totals["book_count"] or 0

    by_year = [
        {
            "year": _year_key(row["year"]),
            "hours": _round_seconds_hours(row["seconds"] or 0),
            "book_count": row["book_count"] or 0,
        }
        for row in (
            dated.values(year=ExtractYear("end_date"))
            .annotate(
                seconds=models.Sum("reading_seconds"),
                book_count=models.Count("id"),
            )
            .order_by("-year")
        )
    ]

    # 还在读的书：end_date 为空。显式给了区间就不算它们。
    unknown_seconds = 0
    if not ranged:
        unknown = base.filter(end_date__isnull=True).aggregate(
            seconds=models.Sum("reading_seconds"),
        )
        unknown_seconds = unknown["seconds"] or 0

    this_year = str(timezone.localdate().year)
    this_year_row = next(
        (row for row in by_year if row["year"] == this_year),
        None,
    )
    this_year_hours = this_year_row["hours"] if this_year_row else 0.0
    this_year_books = this_year_row["book_count"] if this_year_row else 0

    return {
        "total_seconds": total_seconds,
        "total_hours": _round_seconds_hours(total_seconds),
        "book_count": book_count,
        "by_year": by_year,
        "this_year_hours": this_year_hours,
        "this_year_books": this_year_books,
        "unknown_date_hours": _round_seconds_hours(unknown_seconds),
        "has_data": total_seconds > 0,
    }


def get_watch_stats(
    user,
    start_date=None,
    end_date=None,
    tv_minutes_per_episode=DEFAULT_TV_MINUTES_PER_EPISODE,
):
    """汇总一个用户的观影时长（电影实测 + 剧集估算）。

    :param user: Django 用户；传 None 时返回空结构。
    :param start_date: ``datetime.date`` 或 None，按 ``end_date`` 的日期下限过滤。
    :param end_date: ``datetime.date`` 或 None，按 ``end_date`` 的日期上限过滤。
    :param tv_minutes_per_episode: 每集估计分钟数，默认 45。
    :return: dict，字段见模块说明。

    口径说明：

    * 电影：``status = Completed`` 且 ``runtime > 0``，``runtime`` 单位是**分钟**。
    * 剧集：``app_episode.end_date`` 非空就算看过一集，再乘 ``tv_minutes_per_episode``。
      剧集没有时长字段，所以 ``tv_is_estimated`` 恒为 True。
    * 区间过滤对电影和剧集都按各自的 ``end_date`` 日期生效；两者对不上同一个区间时，
      各算各的，互不影响。
    """
    if user is None:
        return _empty_watch_stats()

    try:
        per_episode = int(tv_minutes_per_episode)
    except (TypeError, ValueError):
        per_episode = DEFAULT_TV_MINUTES_PER_EPISODE
    if per_episode < 0:
        per_episode = DEFAULT_TV_MINUTES_PER_EPISODE

    # ---- 电影：实测片长，单位分钟 ----
    movie_base = Movie.objects.filter(
        user=user,
        status=Status.COMPLETED.value,
        runtime__gt=0,
    )
    movie_dated = _apply_end_date_range(
        movie_base.filter(end_date__isnull=False),
        start_date,
        end_date,
    )
    movie_totals = movie_dated.aggregate(
        minutes=models.Sum("runtime"),
        count=models.Count("id"),
    )
    movie_minutes = movie_totals["minutes"] or 0
    movie_count = movie_totals["count"] or 0

    # ---- 剧集：没有时长字段，只能数集数再估 ----
    episode_base = Episode.objects.filter(
        related_season__related_tv__user=user,
        end_date__isnull=False,
    )
    episode_dated = _apply_end_date_range(episode_base, start_date, end_date)
    tv_episodes = episode_dated.count()
    tv_minutes = tv_episodes * per_episode

    # ---- 按年份分组 ----
    movie_by_year = {}
    for row in (
        movie_dated.values(year=ExtractYear("end_date"))
        .annotate(minutes=models.Sum("runtime"))
        .order_by("-year")
    ):
        movie_by_year[_year_key(row["year"])] = row["minutes"] or 0

    tv_by_year = {}
    for row in (
        episode_dated.values(year=ExtractYear("end_date"))
        .annotate(count=models.Count("id"))
        .order_by("-year")
    ):
        tv_by_year[_year_key(row["year"])] = (row["count"] or 0) * per_episode

    years = sorted(
        set(movie_by_year) | set(tv_by_year),
        key=lambda value: value or "",
        reverse=True,
    )
    by_year = []
    for year in years:
        movie_year_minutes = movie_by_year.get(year, 0)
        tv_year_minutes = tv_by_year.get(year, 0)
        by_year.append(
            {
                "year": year,
                "movie_hours": _round_hours(movie_year_minutes),
                "tv_hours": _round_hours(tv_year_minutes),
                "total_hours": _round_hours(movie_year_minutes + tv_year_minutes),
            },
        )

    return {
        "movie_minutes": movie_minutes,
        "movie_hours": _round_hours(movie_minutes),
        "movie_count": movie_count,
        "tv_episodes": tv_episodes,
        "tv_minutes": tv_minutes,
        "tv_hours": _round_hours(tv_minutes),
        "tv_is_estimated": True,
        "total_hours": _round_hours(movie_minutes + tv_minutes),
        "by_year": by_year,
        "has_data": (movie_minutes + tv_minutes) > 0,
    }
