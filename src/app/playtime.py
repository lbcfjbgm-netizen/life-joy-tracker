"""按月统计游玩时长。

数据只有「每个游戏每月一条累计时长快照」这一种来源（见 models.PlaytimeSnapshot）。

当月游玩时长 = 这个游戏本月快照的累计值 - 它上一条快照的累计值。
采集脚本为了省行数会跳过「时长没变」的月份，所以「上一条」可能是几个月前那条，
差值归到「观察到变化」的那个月，总量不会丢。每个游戏的第一条快照是基线，
没有上一条可减，当月计 0。
"""

from __future__ import annotations

import datetime
import logging

from django.core.cache import cache
from django.db.models import Count, F, Max, OuterRef, Q, Subquery, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from app.models import PlaytimeSnapshot

logger = logging.getLogger(__name__)

# 图表配色，前端直接用。
HOURS_COLOR = "#6366f1"
GAMES_COLOR = "#22c55e"

# 月度聚合的缓存时长（秒）。一次聚合要扫全部快照、还要为每条配一条相关子查询，
# 在本机 2900 条快照上约 8ms；统计页每次访问都要它，所以缓存起来。
# 10 分钟是个折中：采集脚本一天只写一次，页面不会看到明显过期的数。
MONTHLY_CACHE_TTL = 600


def month_start(day: datetime.date | None = None) -> datetime.date:
    """返回某天所在月份的 1 号；不传就用今天。"""
    day = day or timezone.localdate()
    return day.replace(day=1)


def shift_month(start: datetime.date, delta: int) -> datetime.date:
    """在「某月 1 号」上前后移动 delta 个月，返回那个月的 1 号。"""
    index = start.year * 12 + (start.month - 1) + delta
    return datetime.date(index // 12, index % 12 + 1, 1)


def month_labels(months: int, end: datetime.date | None = None) -> list[str]:
    """返回最近 months 个月的 "YYYY-MM" 标签，末位是 end 所在月。"""
    end = month_start(end)
    return [
        shift_month(end, -offset).strftime("%Y-%m")
        for offset in range(months - 1, -1, -1)
    ]


def get_monthly_playtime(user, months: int = 12) -> dict:
    """返回最近 N 个月的游玩时长和游戏数，用于画月度柱状图。

    数据来自 PlaytimeSnapshot 相邻两条的差值。这套数据只能向前积累：
    站点在第一次跑采集脚本之前的月份，数据库里根本没有记录，所以那些月份
    一律返回 0，同时用 has_data / data_start / message 告诉前端
    「数据从哪个月开始才有」，避免把一排 0 误读成「那个月没玩游戏」。

    结果会缓存 MONTHLY_CACHE_TTL 秒。缓存键里带一个「数据指纹」（快照条数 +
    最新写入时间 + 当前月份），任何一条快照被写入、或者跨月了，键就变了，
    不会拿到过期的一排 0；也因此测试里改完快照再调用立刻能读到新结果。
    缓存只是加速：读写失败一律降级成现算，不拖垮页面。

    Args:
        user: 用户实例。
        months: 要看最近多少个月，默认 12。

    Returns:
        dict: labels、datasets、has_data、data_start、message。
    """
    months = max(1, int(months))
    this_month = month_start()

    cache_key = _monthly_cache_key(user, months, this_month)
    if cache_key:
        try:
            cached = cache.get(cache_key)
        except Exception:  # noqa: BLE001 - 缓存坏了不能拖垮统计页
            logger.warning("%s - monthly playtime cache read failed", user)
            cached = None
        if cached is not None:
            return cached

    result = _compute_monthly_playtime(user, months, this_month)

    if cache_key:
        try:
            cache.set(cache_key, result, MONTHLY_CACHE_TTL)
        except Exception:  # noqa: BLE001 - 同上，写失败不影响返回
            logger.warning("%s - monthly playtime cache write failed", user)

    return result


def _monthly_cache_key(user, months: int, this_month: datetime.date) -> str | None:
    """拼缓存键；指纹查不出来时返回 None（调用方就跳过缓存）。"""
    try:
        fingerprint = PlaytimeSnapshot.objects.filter(user=user).aggregate(
            rows=Count("id"),
            newest=Max("updated_at"),
            latest_day=Max("captured_on"),
        )
    except Exception:  # noqa: BLE001 - 只是拼键，失败就别用缓存
        logger.warning("%s - monthly playtime fingerprint failed", user)
        return None

    newest = fingerprint["newest"]
    # 用微秒：缓存不随事务回滚（测试里用户主键会被复用），只精确到秒的话，
    # 两个用例在同一秒内写数据就可能撞出同一个键，读到上一条用例的结果。
    stamp = f"{newest.timestamp():.6f}" if newest else "0"
    return (
        f"playtime:monthly:{user.pk}:{months}:{this_month:%Y%m}:"
        f"{fingerprint['rows']}:{stamp}:{fingerprint['latest_day'] or '0'}"
    )


def _compute_monthly_playtime(user, months: int, this_month: datetime.date) -> dict:
    """真正跑聚合的部分（不碰缓存）。"""
    # 多取一个月：最早那个展示月份的「上一条快照」可能落在窗口前一月。
    window_start = shift_month(this_month, -months)
    labels = month_labels(months, this_month)

    # 1) 每条快照配上「同一游戏的上一条快照」：用相关子查询取，SQLite/Postgres 都支持，
    #    而且能直接参与下面的分组聚合（窗口函数不能放在 GROUP BY 里）。
    previous_snapshot = PlaytimeSnapshot.objects.filter(
        user=user,
        game_id=OuterRef("game_id"),
        captured_on__lt=OuterRef("captured_on"),
    ).order_by("-captured_on")

    rows = (
        PlaytimeSnapshot.objects.filter(
            user=user,
            captured_on__gte=window_start,
            captured_on__lte=this_month,
        )
        .annotate(month=TruncMonth("captured_on"))
        .annotate(
            previous_minutes=Subquery(previous_snapshot.values("playtime_minutes")[:1]),
        )
    )

    # 2) 在库里按月汇总：只算「这个月比上一条快照确实变多了」的游戏，
    #    时长没变的不算玩过，时长倒退的（Steam 重置等）不计负值。
    grew = Q(previous_minutes__isnull=False) & Q(
        playtime_minutes__gt=F("previous_minutes"),
    )
    aggregated = rows.values("month").annotate(
        total_minutes=Sum(
            F("playtime_minutes") - F("previous_minutes"),
            filter=grew,
        ),
        game_count=Count("game_id", filter=grew),
    )

    minutes_by_month = {
        row["month"].strftime("%Y-%m"): row["total_minutes"] or 0
        for row in aggregated
    }
    games_by_month = {
        row["month"].strftime("%Y-%m"): row["game_count"] or 0 for row in aggregated
    }

    labels_with_data = [label for label in labels if games_by_month.get(label)]
    data_start = labels_with_data[0] if labels_with_data else None

    return {
        "labels": labels,
        "datasets": [
            {
                "label": "游玩小时",
                "data": [
                    round(minutes_by_month.get(label, 0) / 60, 1) for label in labels
                ],
                "background_color": HOURS_COLOR,
            },
            {
                "label": "玩过的游戏数",
                "data": [games_by_month.get(label, 0) for label in labels],
                "background_color": GAMES_COLOR,
            },
        ],
        "has_data": bool(labels_with_data),
        "data_start": data_start,
        # 前端可直接拿去显示「数据从 X 月开始积累」。
        "message": (
            f"数据从 {data_start} 开始积累"
            if data_start
            else "还没有可对比的快照，本月是基线，下个月起才有柱子"
        ),
    }
