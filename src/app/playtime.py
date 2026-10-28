"""按月统计游玩时长。

数据只有「每个游戏每月一条累计时长快照」这一种来源（见 models.PlaytimeSnapshot）。

当月游玩时长 = 这个游戏本月快照的累计值 - 它上一条快照的累计值。
采集脚本为了省行数会跳过「时长没变」的月份，所以「上一条」可能是几个月前那条，
差值归到「观察到变化」的那个月，总量不会丢。每个游戏的第一条快照是基线，
没有上一条可减，当月计 0。

「最近 7 天玩了多久」用的是另一张日表 PlaytimeDaily（见 get_weekly_playtime），
两张表口径互不干扰：月表算月柱状图，日表算首页的「本周推进最快」。
"""

from __future__ import annotations

import datetime
import logging

from django.core.cache import cache
from django.db.models import Count, F, Max, OuterRef, Q, Subquery, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from app.models import Game, PlaytimeDaily, PlaytimeSnapshot

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


# ---------------------------------------------------------------------------
# 最近 N 天的游玩增量（首页「本周推进最快」那张卡）
#
# 数据来源是日表 PlaytimeDaily：某天的增量 = 今天的累计分钟数 -
# 「N 天前或更早的最近一条」的累计分钟数。两个数来自同一张表、同一套采集脚本，
# 所以相减出来的差值一定自洽，不会像 historical 记录那样掺进假的批量快照。
# ---------------------------------------------------------------------------

# 首页「本周推进最快」这一排最多展示几个游戏。
WEEKLY_LIMIT = 3


def get_weekly_playtime(user, days: int = 7) -> list[dict]:
    """返回最近 days 天涨得最多的几个游戏，用于首页「本周推进最快」。

    为什么必须等 7 天：这套增量靠「同一张日表里两个不同日期的累计值相减」算出来。
    站点刚上线那天，库里只有一行，没有 7 天前的对照值 —— 那时候任何
    「本周涨了 X 小时」都是编的。所以基线（captured_on <= 最新那天 - days 的最近一条）
    不存在的游戏一律不参与排名；一条合法基线都没有时直接返回空 list，
    调用方（首页）必须据此降级成「累计玩得最多」的文案，不许拿累计值冒充周增量。

    Args:
        user: 用户实例。
        days: 往回看多少天，默认 7。

    Returns:
        list[dict]: 每条含 game（Game 实例）、delta_minutes、base_date、delta_hours_text，
        按 delta_minutes 从大到小，最多 WEEKLY_LIMIT 条。没有合法基线时是空 list。
    """
    days = max(1, int(days))
    today = timezone.localdate()
    cache_key = f"playtime:weekly:{user.pk}:{today}"
    # 缓存键按需求固定不含 days；天数不默认时就不碰缓存，免得 14 天拿到 7 天的结果。
    use_cache = days == 7

    cached = None
    if use_cache:
        try:
            cached = cache.get(cache_key)
        except Exception:  # noqa: BLE001 - 缓存坏了不能拖垮首页
            logger.warning("%s - weekly playtime cache read failed", user)
            cached = None

    if cached is None:
        try:
            cached = _compute_weekly_playtime(user, today, days)
        except Exception:  # noqa: BLE001 - 查库失败就当作「还没有数据」
            logger.warning("%s - weekly playtime query failed", user, exc_info=True)
            return []

        if use_cache:
            try:
                cache.set(cache_key, cached, MONTHLY_CACHE_TTL)
            except Exception:  # noqa: BLE001 - 写失败不影响返回
                logger.warning("%s - weekly playtime cache write failed", user)

    return _hydrate_weekly(user, cached)


def _compute_weekly_playtime(user, today: datetime.date, days: int) -> list[dict]:
    """跑聚合，返回可缓存的纯数据（不含模型实例）。

    只缓 game_id / delta_minutes / base_date 三个标量：模型实例缓存进 Redis
    既占地方又容易拿到旧对象，取回后再用一次 in_bulk 补齐更稳。

    「今天」用的是**库里最新那一天**（captured_on <= 今天 的最大值），不是死等
    timezone.localdate()。采集任务每天 23:50 才写当天那行，如果硬要等今天这行，
    那这一整天（00:00–23:50）首页都只能显示降级文案，等于白做。用「最新一天」
    相当于拿昨天那份数据算一个滚动 7 天窗口，数字仍是同源相减，不会失真。

    但如果最新那条已经比今天早两天以上，说明采集任务断了，这时候再喊「本周」
    就是在骗人：直接返回空 list，让首页降级。
    """
    anchor = PlaytimeDaily.objects.filter(
        user=user,
        captured_on__lte=today,
    ).aggregate(latest=Max("captured_on"))["latest"]
    if anchor is None:
        # 一条快照都没有：没有任何可信的东西可算。
        return []

    if anchor < today - datetime.timedelta(days=1):
        logger.warning(
            "%s - weekly playtime: 最新快照停在 %s，采集可能断了，降级不显示",
            user,
            anchor,
        )
        return []

    cutoff = anchor - datetime.timedelta(days=days)

    # 基线：同一游戏、captured_on <= cutoff 的最近一条。
    baseline = PlaytimeDaily.objects.filter(
        user=user,
        game_id=OuterRef("game_id"),
        captured_on__lte=cutoff,
    ).order_by("-captured_on")

    rows = (
        PlaytimeDaily.objects.filter(
            user=user,
            captured_on=anchor,
            playtime_minutes__gt=0,
        )
        .annotate(
            base_minutes=Subquery(baseline.values("playtime_minutes")[:1]),
            base_date=Subquery(baseline.values("captured_on")[:1]),
        )
        # 没有基线 = 算不出真增量，直接不参与排名（不是当 0 处理）。
        .filter(base_minutes__isnull=False)
        .annotate(delta_minutes=F("playtime_minutes") - F("base_minutes"))
        .filter(delta_minutes__gt=0)
        .order_by("-delta_minutes", "game_id")
        .values("game_id", "delta_minutes", "base_date")[:WEEKLY_LIMIT]
    )

    return [
        {
            "game_id": row["game_id"],
            "delta_minutes": int(row["delta_minutes"]),
            "base_date": row["base_date"],
        }
        for row in rows
    ]


def _hydrate_weekly(user, rows: list[dict]) -> list[dict]:
    """把缓存里的 game_id 补成 Game 实例（带 item，模板要拿标题和封面）。"""
    if not rows:
        return []

    games = {
        game.pk: game
        for game in Game.objects.filter(
            pk__in=[row["game_id"] for row in rows],
            user=user,
        ).select_related("item")
    }

    result = []
    for row in rows:
        game = games.get(row["game_id"])
        if game is None:
            # 游戏被删了：跳过，不补假数据。
            continue
        minutes = row["delta_minutes"]
        result.append(
            {
                "game": game,
                "delta_minutes": minutes,
                "base_date": row["base_date"],
                "delta_hours_text": f"{minutes / 60:,.1f}",
            },
        )
    return result
