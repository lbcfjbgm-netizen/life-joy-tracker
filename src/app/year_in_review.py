"""Aggregate the numbers behind the year-in-review page.

年度游玩报告的聚合逻辑。

这个页面只读 ``app_game`` + ``app_item``，所有统计都走数据库聚合
（``values()`` + ``Sum`` / ``Count``），不实例化模型，也不在循环里访问
``game.item``，因为每条模型实例都要 deepcopy 一次字段（约 0.14ms），
几千条下来页面就废了。

🔴 关于时间维度的硬约束（数据现状决定，不是偷懒）：
``app_historicalgame`` 的 5669 条记录全挤在导入当月，``history_change_reason``
全为 NULL，``progressed_at`` 也被导入时间覆盖了，所以本站**没有真实的游玩
时间线**，更没有「按年切分的时长」。

``app_game.progress`` 存的是**从入库到今天的累计分钟数**（不是本年度增量）：

* 2026 年这批游戏累计 629,802 分钟（10,497 小时），已经超过一年 8,760 小时
* 全库 4,935 款游戏累计 770,862 分钟（12,848 小时）

一年最多 8,760 小时，所以任何「今年玩了 N 小时」的说法都是假数。因此本模块
**只输出累计口径**，页面上所有时长都写明是「累计」，并明确说明它不是这一年的
游玩量。唯一能用的年内日期信号是：

* ``end_date``：通关日期，真实值，2023 起才有（289 条）
* ``start_date``：开始游玩日期，真实值，但目前 4935 条全是 NULL（导入没补）

所以「某年玩过哪些游戏」的口径是：``end_date`` 或 ``start_date`` 落在该年。
这两列都是**用户真实行为**留下的日期，是全年内事件。

🔴 ``achievements_synced_at`` 不能用来判年份（2026-10 修正）：
这一列 2782 条全部挤在 2026-10 同一个月，是导入 Steam 数据时一次性刷进去的
系统快照，不是「这一年同步过」的用户行为。用它判年份的后果是 2026 年
凭空多出 2768 款游戏、8400 小时假时长（10,497 小时里 80% 是假的）。
现在它彻底退出年份判定，只在 ``_achievements()`` 里作为「当前快照」的说明。

这些游戏的 ``progress`` 求和只是它们的**累计**时长（不是年内增量），
用来排序和算占比，不代表它们在当年玩了多少。
"""

from __future__ import annotations

from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.db.models.functions import ExtractYear
from django.utils import timezone

from app.models import Game, Status

# 缓存 10 分钟：游戏时长一天才同步一两次，没必要每次请求都重算。
CACHE_TTL = 600

# 🔴 口径版本号：只要「哪些游戏算这一年的」这条规则变了，就必须把它加一。
# 它进缓存键，老 key 自然查不到（等于全部失效），不用手动 flush，
# 也不依赖部署时机 —— 线上正在跑的进程下次请求就落到新 key 上。
#   v1：end_date 或 achievements_synced_at（错误口径，2026 年虚高 8400 小时）
#   v2：end_date 或 start_date（只认用户真实行为日期）
#   v3：口径没变，只是 payload 多了一个 top_share 字段（Top 5 占这批游戏累计
#       时长的百分比，年度报告页的榜单角标用）。加一档让老缓存自然失效，
#       否则页面上那个角标会先空 10 分钟。
#   v4：年份胶囊把当前请求的年份也包含进来（否则 ?year=2022 这类没数据的年份
#       不会高亮）
CACHE_VERSION = 4

# 各区块的展示上限，改这里即可，模板只按长度循环。
TOP_GAMES = 5
TOP_GENRES = 6
COVER_WALL = 12
OLD_GAME_YEARS = 5
# 3~4 年算「半新不旧」；2 年以内算「刚出的」；再往上就是老游戏了。
RECENT_GAME_YEARS = 2
# 库里最早的发售日期是 1990 年代，1970 只是给手输留的下限。
MIN_YEAR = 1970

# 「该年新出」之外的分档，键是 (下限, 上限] 的偏移量，值是人话标签。
AGE_BUCKETS = (
    ("new", "该年新出"),
    ("recent", "1–2 年前"),
    ("mid", "3–4 年前"),
    ("old", "5 年前或更早"),
)

# 类型图和年龄图的配色。服务端只给暗色那一套（首屏渲染时图例小圆点要用），
# 前端按 data-theme 再换成亮色一套，两边顺序必须一致。
GENRE_COLORS = ("#818cf8", "#34d399", "#fbbf24", "#fb7185", "#38bdf8", "#c084fc")


def cache_key(user_id: int, year: int) -> str:
    """Return the cache key for one user's report of one year.

    key 里带 ``CACHE_VERSION``：口径一变，老缓存自动作废，不会被旧数据骗。
    """
    return f"year_in_review:v{CACHE_VERSION}:{user_id}:{year}"


def resolve_year(raw_year: str | None) -> int:
    """Parse ``?year=`` into a sane year; bad input falls back to this year.

    把 ``?year=`` 解析成一个合理年份，坏输入一律回落到今年。

    Args:
        raw_year: URL 上的原始字符串，可能是 None、非数字或离谱的年份。

    Returns:
        1970 到明年之间的整数年份。明年是给「跨年抢先体验」留的余量。
    """
    current = timezone.localdate().year
    if not raw_year:
        return current
    try:
        year = int(str(raw_year).strip())
    except (TypeError, ValueError):
        return current
    if year < MIN_YEAR or year > current + 1:
        return current
    return year


def get_year_in_review(user_id: int, year: int) -> dict:
    """Return the cached report payload for one user and year.

    缓存里只放纯 dict / list / str / 数字，绝不放 QuerySet 或模型实例。
    """
    key = cache_key(user_id, year)
    payload = cache.get(key)
    if payload is not None:
        return payload

    payload = build_year_in_review(user_id, year)
    cache.set(key, payload, CACHE_TTL)
    return payload


def build_year_in_review(user_id: int, year: int) -> dict:
    """Run every aggregation for one report page.

    Args:
        user_id: 用户主键。
        year: 报告年份。

    Returns:
        可直接塞进模板、也能直接进缓存的纯数据字典。

    注意：``total_minutes`` 是这批游戏的**累计**游玩分钟数，不是这一年的
    增量（库里没有按年切分的记录）。模板上必须把它写成「累计」，
    绝不能写成「这一年玩了」。这一点和年份口径修正无关，依然成立。
    """
    cohort = _cohort(user_id, year)
    totals = _totals(cohort)
    # Sum 在没有任何行时给 None，统一成 0，模板和缓存里都不该出现 None 分钟数。
    total_minutes = totals["minutes"] or 0
    total_hours = total_minutes / 60

    top_games = _top_games(cohort, total_minutes)
    completed = _completed(user_id, year, total_minutes)
    genres = _genres(cohort, total_minutes)
    achievements = _achievements(cohort)
    age = _game_age(cohort, year, total_minutes)
    library = _library(user_id, total_minutes)

    favorite = genres[0] if genres else None

    return {
        "year": year,
        "years": _year_options(user_id, year),
        "has_data": bool(total_minutes),
        # 开场大字：这批游戏的累计时长（不是年度增量）
        "total_hours": round(total_hours, 1),
        "total_hours_text": _thousands(round(total_hours)),
        "total_minutes": total_minutes,
        "game_count": totals["games"],
        "played_count": totals["played"],
        # 整库对照，用来把「这批游戏的累计」放回正确的分母里讲清楚
        "library": library,
        "top_games": top_games,
        # Top 5 合计占这批游戏累计时长的百分比（年度报告页榜单角标）。
        # 纯派生值，不进数据库、不进统计口径，只是把 top_games 的 percent 加起来。
        "top_share": top_share(top_games),
        "completed": completed,
        "genres": genres,
        "achievements": achievements,
        "age": age,
        "favorite": favorite,
    }


def top_share(top_games: list[dict]) -> float:
    """Return how much of the cohort's cumulative time the top list covers.

    Top 5 这几款游戏加起来，占这批游戏累计总时长的百分之多少。

    这是个纯加法的派生展示值，不查库、不改任何既有口径：``top_games``
    每条的 ``percent`` 本来就已经是「占这批游戏累计总时长」的比例，
    这里只是把它们加起来，给年度报告页榜单右上角那个角标用。

    不能直接写 100 再减：``percent`` 逐条四舍五入过，加总常有 ±0.1 的漂移，
    页面上写死反而对不上，所以让模板显示这个算出来的数。
    """
    total = sum(float(item.get("percent") or 0) for item in top_games or [])
    return round(total, 1)


def _library(user_id: int, cohort_minutes: int) -> dict:
    """Return the whole-library cumulative playtime, for the honesty note.

    全库累计时长 = 这个用户所有游戏的 ``progress`` 求和。它只作对照，
    同样**不是**某一年的游玩量。
    """
    totals = Game.objects.filter(user_id=user_id).aggregate(
        minutes=Sum("progress"),
        games=Count("id"),
    )
    minutes = totals["minutes"] or 0
    return {
        "minutes": minutes,
        "hours": round(minutes / 60, 1),
        "hours_text": _thousands(round(minutes / 60)),
        "games": totals["games"] or 0,
        # 这批游戏占了全库累计时长的多少（分母是全库累计，两者同口径）
        "share": round(cohort_minutes / minutes * 100, 1) if minutes else 0,
    }


def available_years(user_id: int) -> list[int]:
    """Return the years that have a real play date, newest first.

    🔴 必须和 :func:`_cohort` 完全同口径：两处都只认 ``end_date`` /
    ``start_date``。否则会出现「年份按钮里有，点开是空的」（按钮按宽口径出、
    内容按严口径算）或者反过来。

    不查 ``achievements_synced_at``：那一列全落在导入那一个月（2026-10），
    用它只会凭空多出一个「有数据」的年份，点开还是假的。
    """
    years = (
        Game.objects.filter(user_id=user_id)
        .filter(Q(end_date__isnull=False) | Q(start_date__isnull=False))
        .order_by()
        .annotate(
            end_year=ExtractYear("end_date"),
            play_year=ExtractYear("start_date"),
        )
        .values_list("end_year", "play_year")
    )
    found: set[int] = set()
    for end_year, play_year in years:
        for value in (end_year, play_year):
            if value:
                found.add(int(value))
    found.add(timezone.localdate().year)
    return sorted(found, reverse=True)


def _year_options(user_id: int, year: int) -> list[int]:
    """Return the year pills to render, including the year being viewed.

    年份胶囊列表 = 「有真实日期的年份」+ 当前页正在看的这一年。

    为什么要把当前年份并进来：``available_years`` 只认有真实日期的年份，
    所以手动访问 ``?year=2022`` 这种没数据的年份时，页面上那颗 2022 的胶囊
    根本不存在，模板里 ``option_year == year`` 永远为假，**没有任何一颗会高亮**，
    用户看不出来自己在看哪一年。并进去之后，哪怕这年是空态，胶囊也在、
    也是亮的，点击行为和其他年份一致。

    已经在这个列表里就原样返回（不能重复，也不能把自己再插一遍）。
    结果保持倒序（新的年份在前），和 ``available_years`` 的顺序一致。
    """
    years = available_years(user_id)
    if year in years:
        return years
    return sorted([*years, year], reverse=True)


def _cohort(user_id: int, year: int):
    """Return the queryset of games that belong to one year.

    口径：``end_date`` 落在该年，或 ``start_date`` 落在该年。

    这两列都是用户真实行为留下的日期（通关 / 开始玩）。**不用**
    ``achievements_synced_at``：那一列 2782 条全挤在 2026-10 一次批量导入里，
    是系统行为，拿它判年份会让 2026 年凭空多出 2768 款游戏、8400 小时假时长。
    宁可少统计，也不要虚假统计。

    ``order_by()`` 必须清空：``Media.Meta.ordering`` 里的字段不在
    ``values()`` 里，一旦留着就会被并进 GROUP BY，把分组拆碎。
    """
    return Game.objects.filter(user_id=user_id).filter(
        Q(end_date__year=year) | Q(start_date__year=year),
    ).order_by()


def _totals(cohort) -> dict:
    """Aggregate the cohort's cumulative playtime and game counts.

    ``minutes`` 是这批游戏 ``progress`` 的累计和，不是某一年的增量。
    """
    return cohort.aggregate(
        minutes=Sum("progress"),
        games=Count("id"),
        played=Count("id", filter=Q(progress__gt=0)),
    )


def _top_games(cohort, total_minutes: int) -> list[dict]:
    """Return the most played games of the cohort, minutes descending.

    排序和占比都用 ``progress`` 的累计值：一款游戏排前面说明它库里累计花的时间
    多，不代表它是在这一年玩的。占比分母是这批游戏的累计总时长，所以叫
    「占这批游戏累计时长的 X%」。
    """
    rows = (
        cohort.filter(progress__gt=0)
        .values(
            "item__title",
            "item__image",
            "item__media_id",
            "item__source",
        )
        .annotate(minutes=Sum("progress"))
        .order_by("-minutes")[:TOP_GAMES]
    )
    out = []
    for row in rows:
        minutes = row["minutes"] or 0
        out.append(
            {
                "title": row["item__title"],
                "image": row["item__image"],
                "media_id": row["item__media_id"],
                "source": row["item__source"],
                "minutes": minutes,
                "hours": round(minutes / 60, 1),
                "percent": round(minutes / total_minutes * 100, 1)
                if total_minutes
                else 0,
            },
        )
    return out


def _completed(user_id: int, year: int, total_minutes: int) -> dict:
    """Return the games finished (status Completed, end_date) in the year."""
    cohort = Game.objects.filter(
        user_id=user_id,
        status=Status.COMPLETED.value,
        end_date__year=year,
    ).order_by()
    totals = cohort.aggregate(
        count=Count("id"),
        minutes=Sum("progress"),
    )
    rows = (
        cohort.values(
            "item__title",
            "item__image",
            "item__media_id",
            "item__source",
            "end_date",
        )
        .annotate(minutes=Sum("progress"))
        .order_by("-end_date")[:COVER_WALL]
    )
    covers = []
    for row in rows:
        minutes = row["minutes"] or 0
        covers.append(
            {
                "title": row["item__title"],
                "image": row["item__image"],
                "media_id": row["item__media_id"],
                "source": row["item__source"],
                "end_date": row["end_date"].strftime("%Y-%m-%d")
                if row["end_date"]
                else "",
                "hours": round(minutes / 60, 1),
                "percent": round(minutes / total_minutes * 100, 1)
                if total_minutes
                else 0,
            },
        )
    count = totals["count"] or 0
    return {
        "count": count,
        "hours": round((totals["minutes"] or 0) / 60, 1),
        "covers": covers,
        "hidden": max(count - len(covers), 0),
    }


def _genres(cohort, total_minutes: int) -> list[dict]:
    """Return the genre portrait, top genres by cumulative playtime.

    ``item.genres`` 是 JSON 数组，SQLite 里没法直接 GROUP BY，所以先让
    数据库按「一行一个类型组合」聚合（2026 年 242 组，不是 2816 行），
    再在 Python 里把组合摊平成单个类型。这样既没实例化模型，也没逐游戏循环。
    """
    rows = (
        cohort.values("item__genres")
        .annotate(minutes=Sum("progress"), games=Count("id"))
    )
    summed: dict[str, dict] = {}
    for row in rows:
        minutes = row["minutes"] or 0
        for name in row["item__genres"] or []:
            entry = summed.setdefault(name, {"minutes": 0, "games": 0})
            entry["minutes"] += minutes
            entry["games"] += row["games"] or 0

    # 分母用「这批游戏的累计总时长」而不是「各类型时长之和」：一款游戏常带
    # 3~4 个类型，求和会把同一分钟数记好几次，那样算出来的占比加起来远超 100%，
    # 也没有「这个类型占了多少」这层意思。收官那句也读这个百分比。
    total = total_minutes or sum(entry["minutes"] for entry in summed.values())

    out = []
    for rank, (name, entry) in enumerate(
        sorted(
            summed.items(),
            key=lambda kv: kv[1]["minutes"],
            reverse=True,
        )[:TOP_GENRES],
    ):
        out.append(
            {
                "name": name,
                "minutes": entry["minutes"],
                "hours": round(entry["minutes"] / 60, 1),
                "games": entry["games"],
                "percent": round(entry["minutes"] / total * 100, 1) if total else 0,
                "color": GENRE_COLORS[rank % len(GENRE_COLORS)],
            },
        )
    return out


def _achievements(cohort) -> dict:
    """Return the achievement snapshot for the cohort.

    ⚠ 这里给的是「当前快照」，不是「这一年新解锁」：``achievements_synced_at``
    被导入统一覆盖过，逐条解锁时间库里根本没有。所以 ``unlocked`` /
    ``percent`` 都只说明这批游戏此刻的状态，模板文案不能写成「今年解锁了 N 个」。

    ``synced_games`` / ``synced_unlocked`` 在 v2 口径下**恒为 0**：
    cohort 已经不含「靠同步时间混进来」的游戏，这一年同步过的游戏本来就
    不属于这一年。字段保留只是为了不让还在引用它的模板炸掉，别据此写文案。
    """
    totals = cohort.aggregate(
        unlocked=Sum("achievements_unlocked"),
        available=Sum("achievements_total"),
        games_with_data=Count("id", filter=Q(achievements_total__gt=0)),
        perfect=Count("id", filter=Q(is_all_achievements=True)),
    )
    return {
        "unlocked": totals["unlocked"] or 0,
        "available": totals["available"] or 0,
        "games_with_data": totals["games_with_data"] or 0,
        "perfect": totals["perfect"] or 0,
        # v2 起恒为 0，见上面 docstring，保留键只为模板兼容
        "synced_unlocked": 0,
        "synced_games": 0,
        "percent": round(
            (totals["unlocked"] or 0) / totals["available"] * 100,
        )
        if totals["available"]
        else 0,
    }


def _game_age(cohort, year: int, total_minutes: int) -> dict:
    """Return how much cumulative playtime went to new vs old releases."""
    rows = (
        cohort.filter(item__release_date__isnull=False)
        .values(release_year=ExtractYear("item__release_date"))
        .annotate(minutes=Sum("progress"), games=Count("id"))
    )
    buckets = {
        key: {"key": key, "label": label, "minutes": 0, "games": 0}
        for key, label in AGE_BUCKETS
    }
    known = 0
    for row in rows:
        release_year = row["release_year"]
        minutes = row["minutes"] or 0
        games = row["games"] or 0
        known += minutes
        if release_year is None:
            continue
        gap = year - int(release_year)
        if gap <= 0:
            key = "new"
        elif gap <= RECENT_GAME_YEARS:
            key = "recent"
        elif gap < OLD_GAME_YEARS:
            key = "mid"
        else:
            key = "old"
        buckets[key]["minutes"] += minutes
        buckets[key]["games"] += games

    missing = cohort.filter(item__release_date__isnull=True).aggregate(
        minutes=Sum("progress"),
        games=Count("id"),
    )

    out = []
    for key, _label in AGE_BUCKETS:
        bucket = buckets[key]
        out.append(
            {
                "key": key,
                "label": bucket["label"],
                "minutes": bucket["minutes"],
                "hours": round(bucket["minutes"] / 60, 1),
                "games": bucket["games"],
                "percent": round(bucket["minutes"] / known * 100, 1)
                if known
                else 0,
            },
        )
    return {
        "buckets": out,
        "known_minutes": known,
        "missing_minutes": missing["minutes"] or 0,
        "missing_games": missing["games"] or 0,
        "total_minutes": total_minutes,
    }


def _thousands(value: int) -> str:
    """Format a number with thousands separators, e.g. 12848 -> 12,848."""
    return f"{value:,}"


__all__ = [
    "CACHE_TTL",
    "CACHE_VERSION",
    "available_years",
    "build_year_in_review",
    "cache_key",
    "get_year_in_review",
    "resolve_year",
    "top_share",
]
