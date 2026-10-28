import datetime
from datetime import timedelta

from django.utils import timezone
from django.utils.text import slugify

from app.models import BasicMedia, MediaTypes, Status, home_media_is_hidden

# 首页「在读」的判定：至少读了 30 分钟、两周内有过进展、并且没被标记读完。
# 115 本 In progress 里 99 本累计不到 10 分钟（导入时留下的空壳记录），
# 只按状态列出来会把书架整个淹掉，所以书的门槛比游戏严。
BOOK_ACTIVE_MIN_READING_SECONDS = 1800
BOOK_ACTIVE_DAYS = 14


def build_home_section(key, media_types):
    """Build home section payload."""
    return {
        "key": key,
        "id": slugify(key),
        "media_types": media_types,
        "count": sum(media_list["total"] for media_list in media_types.values()),
    }


def is_active_in_progress_media(media):
    """Return True when media still has released backlog."""
    if not _is_incoming_media(media):
        return True

    return media.max_progress is not None and media.progress < media.max_progress


def get_home_media_types(
    request,
    sort_by,
    section_key,
    items_limit,
    specific_media_type=None,
    *,
    hide_unreleased=False,
):
    """Return media types for a home section, or one type's load-more page.

    ``items_limit`` is always passed down, so the manager can order plain rows
    and build model instances only for the cards that get rendered. The
    hide-unreleased split happens inside that same pass instead of filtering a
    fully materialised shelf here: on this library that shelf is 2000+ games and
    building the objects cost more than everything else on the page combined.

    On the in-progress shelf the books are narrowed to the ones actually being
    read (see ``_active_book_shelf``); every other shelf is the manager's.
    """
    list_by_type = BasicMedia.objects.get_home_status(
        user=request.user,
        status=section_key,
        sort_by=sort_by,
        items_limit=items_limit,
        specific_media_type=specific_media_type,
        hide_unreleased=hide_unreleased,
    )

    if section_key == Status.IN_PROGRESS.value and specific_media_type in (
        None,
        MediaTypes.BOOK.value,
    ):
        _replace_with_active_books(
            list_by_type,
            request.user,
            sort_by,
            items_limit,
            specific_media_type is not None,
            hide_unreleased,
        )

    return list_by_type


def _replace_with_active_books(
    list_by_type,
    user,
    sort_by,
    items_limit,
    load_more,
    hide_unreleased,
):
    """Swap the manager's book shelf for the "actually reading" one.

    The book conditions are applied on the queryset *before* the page slice.
    Filtering the rendered 14 cards afterwards would report a total that
    disagrees with the cards, and would also lose the qualifying rows that fell
    outside the first page.
    """
    books = _active_book_shelf(user, sort_by, hide_unreleased)
    total = len(books)
    if not total:
        list_by_type.pop(MediaTypes.BOOK.value, None)
        return

    page_start = items_limit if load_more else 0
    list_by_type[MediaTypes.BOOK.value] = {
        "items": books[page_start : page_start + items_limit],
        "total": total,
    }


def _active_book_shelf(user, sort_by, hide_unreleased):
    """Books that belong on the home page's in-progress shelf.

    Same annotations and same sort table as every other home shelf, so the
    ordering cannot drift from the manager; only the row set is narrower.
    """
    queryset = BasicMedia.objects.get_media_list(
        user=user,
        media_type=MediaTypes.BOOK.value,
        status_filter=Status.IN_PROGRESS.value,
        sort_filter=None,
    ).filter(
        reading_seconds__gte=BOOK_ACTIVE_MIN_READING_SECONDS,
        progressed_at__gte=timezone.now() - timedelta(days=BOOK_ACTIVE_DAYS),
        end_date__isnull=True,
    )

    books = list(queryset)
    if not books:
        return []

    manager = BasicMedia.objects
    manager.annotate_max_progress(books, MediaTypes.BOOK.value)
    manager._annotate_next_event(books)

    if hide_unreleased:
        books = [
            book
            for book in books
            if not home_media_is_hidden(
                Status.IN_PROGRESS.value,
                bool(book.next_event),
                book.max_progress,
                book.progress,
            )
        ]

    return manager._sort_home_media(books, sort_by)


def _is_incoming_media(media):
    """Return True when media has a real upcoming release."""
    return bool(media.next_event and not media.next_event.is_max_datetime)


# ---------------------------------------------------------------------------
# 首页新版（设计稿 home-A-shelf）用到的三块数据。
#
# 顶部那 5 组数字**不改口径**：全部走统计页同一套函数
# （statistics.load_date_range / get_user_media / get_status_distribution 与
# duration_stats.get_watch_stats / get_reading_stats），时间范围取用户存的
# statistics_date_range。这样首页和 /statistics 永远是同一份账，谁改了统计口径
# 首页自动跟着变，不会写死一堆会过期的数字。
# ---------------------------------------------------------------------------


def _this_year_range():
    """今年 1 月 1 日 00:00 ~ 今天 23:59:59，返回 aware datetime 对。"""
    today = timezone.localdate()
    start_date = timezone.make_aware(
        datetime.datetime.combine(today.replace(month=1, day=1), datetime.time.min),
    )
    end_date = timezone.make_aware(
        datetime.datetime.combine(today, datetime.time.max),
    )
    return start_date, end_date


def build_home_topline(user):
    """首页顶栏的 5 组数字，口径与统计页一致，但固定按「今年」算。

    为什么不直接用 statistics.load_date_range(user)：那个值是统计页上用户自己
    挑的范围，可能是「全部」（all|all）。首页顶栏说的是"今年"，范围一旦跟着
    变成全历史，数字就翻几十倍（实测 1268 已完成 / 12194.5 小时），和标题自相矛盾。
    """
    # 函数内 import：避免 app.home 在模块加载期把 statistics/duration_stats 拖进来
    # 造成循环引用（views.py 同时 import 三者）。
    from app import duration_stats
    from app import statistics as stats

    start_date, end_date = _this_year_range()
    user_media, media_count = stats.get_user_media(user, start_date, end_date)
    status_dist = stats.get_status_distribution(user_media)

    watch_stats = duration_stats.get_watch_stats(user, start_date, end_date)
    by_year = watch_stats.get("by_year") or []
    current_year = str(timezone.localdate().year)
    # 统计页的「今年」就是 by_year 里当年那一行；没有今年数据时退回最后一行。
    year_row = next(
        (row for row in by_year if str(row.get("year")) == current_year),
        by_year[-1] if by_year else None,
    )
    movie_hours = float(year_row.get("movie_hours") or 0) if year_row else 0.0
    tv_hours = float(year_row.get("tv_hours") or 0) if year_row else 0.0

    reading_stats = duration_stats.get_reading_stats(user, start_date, end_date)

    # 游戏的游玩时长在 get_consumption_stats 里是**取整**的小时数，
    # 想拿到 .3 这种精度只能自己把 progress（分钟）加起来。
    game_minutes = sum(
        (media.progress or 0) for media in user_media.get(MediaTypes.GAME.value, [])
    )

    completed = status_dist.get("total_completed") or 0
    total = media_count.get("total") or 0

    return {
        "completed": completed,
        "completed_text": f"{completed:,}",
        "total_text": f"{total:,}",
        "movie_hours_text": f"{movie_hours:,.1f}",
        "movie_count": media_count.get(MediaTypes.MOVIE.value) or 0,
        "game_hours_text": f"{game_minutes / 60:,.1f}",
        "game_count": media_count.get(MediaTypes.GAME.value) or 0,
        "tv_hours_text": f"{tv_hours:,.1f}",
        # 剧集数量按「剧集 + 季」合并算，和统计页「剧集季 32」口径一致。
        "tv_count": (media_count.get(MediaTypes.TV.value) or 0)
        + (media_count.get(MediaTypes.SEASON.value) or 0),
        "reading_hours_text": f"{float(reading_stats.get('this_year_hours') or 0):,.1f}",
        "reading_books": reading_stats.get("this_year_books") or 0,
        "year": current_year,
        "range_text": f"{start_date:%Y.%m.%d} – {end_date:%Y.%m.%d}",
    }


def _weekly_card(weekly, candidates):
    """本周增量第一张卡：有真增量就用它，没有就诚实降级成「累计玩得最多」。

    weekly 是 playtime.get_weekly_playtime 的返回（可能为空）。空的时候**必须**
    换文案：这张卡以前写着「本周推进最快」，实际排的是累计时长，属于编数字。
    降级时只说它真正在算的东西，不假装自己在算周增量。

    candidates 是「全库玩过的游戏」的一个小切片（调用方已经排好序、只取前几十条），
    它只用来挑这一张卡 —— 首页货架本身还是那 10 条「进行中」，不会因为
    放宽候选池就把 1180 个游戏塞进模板。
    """
    if weekly:
        top = weekly[0]
        minutes = top["delta_minutes"]
        return {
            "media": top["game"],
            "kicker": "本周推进最快",
            "meta": f"本周 +{minutes / 60:,.1f} 小时",
            "pct": None,
        }

    if not candidates:
        return None

    media = max(candidates, key=lambda game: game.progress or 0)
    minutes = media.progress or 0
    return {
        "media": media,
        "kicker": "累计玩得最多",
        "meta": f"{minutes / 60:,.1f} 小时 · 共 {minutes:,} 分钟",
        "pct": None,
    }


def build_continue_cards(games, books, limit=3, weekly=None, candidates=None):
    """「继续上次」那一排卡片：先挑推进最多的游戏，再按阅读时长补书。

    只做展示用的小字典，不额外查库 —— games / books 就是首页「进行中」
    分区已经算好的那份数据，避免同一份货架算两遍。

    Args:
        games: 「进行中」货架上的游戏，用来补「继续游玩」那几张卡。
        books: 「进行中」的在读书籍。
        limit: 最多几张卡。
        weekly: playtime.get_weekly_playtime 的结果。有值 → 第一张卡说「本周推进最快」；
            None / 空 → 降级成「累计玩得最多」（就是它实际在算的东西）。
        candidates: 放宽后的第一张卡候选池（全库 progress>0 的头部若干条）。
            不传就退回 games 本身，行为和改造前一致。
    """
    shelf_games = list(games)
    if candidates is None:
        pool = shelf_games
    else:
        pool = list(candidates)

    games_sorted = sorted(shelf_games, key=lambda media: media.progress or 0, reverse=True)
    books_sorted = sorted(
        books,
        key=lambda media: media.reading_seconds or 0,
        reverse=True,
    )

    cards = []
    first = _weekly_card(weekly, pool)
    if first is not None:
        cards.append(first)

    # 第一张卡已经占了哪个游戏，后面就不要再重复推同一个。
    used_pks = {first["media"].pk} if first is not None else set()

    for media in books_sorted:
        if len(cards) >= limit:
            break
        progress = media.progress
        # 书没有总页数时 progress 直接就是 0-100 的百分比（见 Media.is_percent_book）。
        pct = int(progress) if isinstance(progress, (int, float)) and 0 <= progress <= 100 else None
        hours = (media.reading_seconds or 0) / 3600
        cards.append(
            {
                "media": media,
                "kicker": "继续阅读",
                "meta": f"{hours:,.1f} 小时 · 读了 {pct or 0}%" if hours else f"读了 {pct or 0}%",
                "pct": pct,
            },
        )

    # 补位的游戏只从货架里取，不再往模板里灌全库。
    for media in games_sorted:
        if len(cards) >= limit:
            break
        if media.pk in used_pks:
            continue
        used_pks.add(media.pk)
        minutes = media.progress or 0
        cards.append(
            {
                "media": media,
                "kicker": "继续游玩",
                "meta": f"{minutes / 60:,.1f} 小时",
                "pct": None,
            },
        )

    return cards[:limit]


def planning_book_shelf(user, limit=18):
    """「书架待启」用的未开始书籍。

    这里**不用**统计页的日期范围：Planning 的书两边日期都空，按区间筛就全没了
    （真跑过：范围内 Planning 书 = 0 本）。未开始的书本来就该按「有没有上架」看，
    所以直接走库里的状态过滤。
    """
    return list(
        BasicMedia.objects.get_media_list(
            user=user,
            media_type=MediaTypes.BOOK.value,
            status_filter=Status.PLANNING.value,
            sort_filter=None,
        )[:limit],
    )
