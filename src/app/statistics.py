import calendar
import datetime
import logging
from collections import defaultdict

from dateutil.relativedelta import relativedelta
from django.apps import apps
from django.db import models
from django.db.models import (
    Prefetch,
    Q,
)
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.translation import get_language

from app import config, i18n_zh
from app.models import TV, BasicMedia, Episode, MediaManager, MediaTypes, Season, Status
from app.templatetags import app_tags
from users.models import WeekStartDayChoices

logger = logging.getLogger(__name__)


def save_date_range(user, start_date, end_date):
    """把本次选择的时间范围写回用户（"all" 表示不限，空串表示没选过）。

    编码成一个字段两端的字符串：``"2026-01-01|2026-10-25"``，
    两端都选中「全部时间」时存 ``"all|all"``。空串（默认值）留给「从没选过」，
    所以老行为（裸进 /statistics 看全部时间）一个字不用改。
    """
    if user is None or not getattr(user, "pk", None):
        return

    timeformat = "%Y-%m-%d"
    start_str = start_date.strftime(timeformat) if start_date is not None else "all"
    end_str = end_date.strftime(timeformat) if end_date is not None else "all"
    value = f"{start_str}|{end_str}"
    # 字段上限 32 字符；两个 10 位日期 + 分隔符最多 21，超了直接不存，
    # 免得触发数据库校验把整个统计页带崩。
    if len(value) > 32:
        logger.warning("统计时间范围过长，未保存：%s", value)
        return

    if getattr(user, "statistics_date_range", "") == value:
        return
    try:
        user.statistics_date_range = value
        user.save(update_fields=["statistics_date_range"])
    except Exception:  # noqa: BLE001 - 记忆功能失败不能影响统计页渲染
        logger.exception("写入统计时间范围失败")


def load_date_range(user):
    """读回上次存的时间范围，返回 (start_date, end_date)，没存过返回 None。

    返回值口径和 ``parse_activity_date_range`` 一致：aware datetime 或 None。
    """
    raw = (getattr(user, "statistics_date_range", "") or "").strip()
    if not raw:
        return None

    start_str, _, end_str = raw.partition("|")
    if not start_str and not end_str:
        return None

    start_date = None
    end_date = None
    if start_str and start_str != "all":
        parsed = parse_date(start_str)
        if parsed is not None:
            start_date = timezone.make_aware(
                datetime.datetime.combine(parsed, datetime.time.min),
            )
    if end_str and end_str != "all":
        parsed = parse_date(end_str)
        if parsed is not None:
            end_date = timezone.make_aware(
                datetime.datetime.combine(parsed, datetime.time.max),
            )

    return start_date, end_date


def parse_activity_date_range(request):
    """Parse ``start-date``/``end-date`` params into an aware datetime range.

    Defaults to the last year. ``all`` for both means no range (``None``).
    """
    timeformat = "%Y-%m-%d"
    today = timezone.localdate()
    # relativedelta clamps Feb 29 to Feb 28 instead of raising ValueError.
    one_year_ago = today - relativedelta(years=1)

    start_date_str = request.GET.get("start-date") or one_year_ago.strftime(timeformat)
    end_date_str = request.GET.get("end-date") or today.strftime(timeformat)

    if start_date_str == "all" and end_date_str == "all":
        return None, None

    start_date = parse_date(start_date_str)
    end_date = parse_date(end_date_str)

    # Normalise each side on its own: a side that is "all" (or unparsable)
    # stays open (None), while a real value becomes an aware datetime. The two
    # sides are independent, so a half-open range must not hand callers a bare
    # ``date`` — they rely on the datetime API (``.date()``, filtering).
    if start_date is not None:
        start_date = timezone.make_aware(
            datetime.datetime.combine(start_date, datetime.time.min),
        )
    if end_date is not None:
        end_date = timezone.make_aware(
            datetime.datetime.combine(end_date, datetime.time.max),
        )

    return start_date, end_date


def get_user_media(user, start_date, end_date):
    """Get all media items and their counts for a user within date range."""
    # Only one side may be open ("all" picked on a single end). Django raises
    # "Cannot use None as a query value" on None lookups, so widen the open
    # side to a sentinel instead of splitting the overlap filter below. Both
    # None still means "no filter at all". Pinned to UTC so the sentinel never
    # picks up a historic local-time offset.
    if start_date is None and end_date is not None:
        start_date = timezone.make_aware(datetime.datetime(1900, 1, 1), datetime.UTC)
    elif end_date is None and start_date is not None:
        end_date = timezone.make_aware(
            datetime.datetime(2999, 12, 31, 23, 59, 59),
            datetime.UTC,
        )

    media_models = [
        apps.get_model(app_label="app", model_name=media_type)
        for media_type in user.get_active_media_types()
    ]
    user_media = {}
    media_count = {"total": 0}

    # Cache the base episodes query
    base_episodes = None
    if TV in media_models or Season in media_models:
        if start_date is None and end_date is None:
            # No date filtering for "All Time"
            base_episodes = Episode.objects.filter(
                related_season__user=user,
            )
        else:
            base_episodes = Episode.objects.filter(
                related_season__user=user,
                end_date__range=(start_date, end_date),
            )

    for model in media_models:
        media_type = model.__name__.lower()
        queryset = None

        if model == TV:
            tv_ids = base_episodes.values_list(
                "related_season__related_tv",
                flat=True,
            ).distinct()
            queryset = TV.objects.filter(id__in=tv_ids).prefetch_related(
                Prefetch(
                    "seasons",
                    queryset=Season.objects.select_related(
                        "item",
                    ).prefetch_related(
                        Prefetch(
                            "episodes",
                            queryset=base_episodes.filter(
                                related_season__related_tv__in=tv_ids,
                            ),
                        ),
                    ),
                ),
            )
        elif model == Season:
            season_ids = base_episodes.values_list(
                "related_season",
                flat=True,
            ).distinct()
            queryset = Season.objects.filter(
                id__in=season_ids,
            ).prefetch_related(
                Prefetch("episodes", queryset=base_episodes),
            )
        # For other models, apply date filtering conditionally
        elif start_date is None and end_date is None:
            # No date filtering for "All Time"
            queryset = model.objects.filter(user=user)
        else:
            queryset = model.objects.filter(user=user).filter(
                # Case 1: Media has both start_date and end_date
                # Include if ranges overlap
                # (exclude if media ends before filter start or starts after filter end)
                (
                    Q(start_date__isnull=False)
                    & Q(end_date__isnull=False)
                    & ~(Q(end_date__lt=start_date) | Q(start_date__gt=end_date))
                )
                |
                # Case 2: Media only has start_date (end_date is null)
                # Include if start_date is within filter range
                (
                    Q(start_date__isnull=False)
                    & Q(end_date__isnull=True)
                    & Q(start_date__gte=start_date)
                    & Q(start_date__lte=end_date)
                )
                |
                # Case 3: Media only has end_date (start_date is null)
                # Include if end_date is within filter range
                (
                    Q(start_date__isnull=True)
                    & Q(end_date__isnull=False)
                    & Q(end_date__gte=start_date)
                    & Q(end_date__lte=end_date)
                ),
            )

        queryset = queryset.select_related("item")
        user_media[media_type] = queryset
        # Dropped titles stay in user_media so the status breakdown can still
        # show the "dropped" slice, but they are left out of the totals: a
        # dropped game is not part of the library count any more.
        # TV and Season are excluded from this on purpose: their counts come
        # from the episode graph, not from a status column of their own.
        count_queryset = (
            queryset
            if model in (TV, Season)
            else queryset.exclude(status=Status.DROPPED.value)
        )
        count = count_queryset.count()
        media_count[media_type] = count
        media_count["total"] += count

    logger.info(
        "%s - Retrieved media %s",
        user,
        "for all time" if start_date is None else f"from {start_date} to {end_date}",
    )
    return user_media, media_count


def get_media_type_distribution(media_count):
    """Get data formatted for Chart.js pie chart."""
    # Define colors for each media type
    # Format for Chart.js
    chart_data = {
        "labels": [],
        "datasets": [
            {
                "data": [],
                "backgroundColor": [],
            },
        ],
    }

    # Only include media types with counts > 0
    for media_type, count in media_count.items():
        if media_type != "total" and count > 0:
            # Format label with first letter capitalized
            label = app_tags.media_type_readable(media_type)
            chart_data["labels"].append(label)
            chart_data["datasets"][0]["data"].append(count)
            chart_data["datasets"][0]["backgroundColor"].append(
                config.get_stats_color(media_type),
            )
    return chart_data


def get_status_distribution(user_media):
    """Get status distribution for each media type within date range."""
    distribution = {}
    total_completed = 0
    # Define status order to ensure consistent stacking
    status_order = list(Status.values)
    for media_type, media_list in user_media.items():
        status_counts = dict.fromkeys(status_order, 0)
        counts = media_list.values("status").annotate(count=models.Count("id"))
        for count_data in counts:
            status_counts[count_data["status"]] = count_data["count"]
            if count_data["status"] == Status.COMPLETED.value:
                total_completed += count_data["count"]

        distribution[media_type] = status_counts

    # Format the response for charting
    return {
        "labels": [app_tags.media_type_readable(x) for x in distribution],
        "datasets": [
            {
                "label": status,
                "data": [
                    distribution[media_type][status] for media_type in distribution
                ],
                "background_color": get_status_color(status),
                "total": sum(
                    distribution[media_type][status] for media_type in distribution
                ),
            }
            for status in status_order
        ],
        "total_completed": total_completed,
    }


def get_status_total(status_distribution, status):
    """Return the total count of a single status across all media types."""
    for dataset in status_distribution["datasets"]:
        if dataset["label"] == status:
            return dataset["total"]
    return 0


def get_status_pie_chart_data(status_distribution):
    """Get status distribution as a pie chart."""
    # Format for Chart.js pie chart
    chart_data = {
        "labels": [],
        "datasets": [
            {
                "data": [],
                "backgroundColor": [],
            },
        ],
    }

    # 界面上「计划中」并进「已暂停」一档，两个状态同名，饼图里必须合并成一块，
    # 否则会出现两个一模一样的「已暂停」扇区。按显示名归并，顺序沿用状态枚举顺序。
    merged = {}
    for dataset in status_distribution["datasets"]:
        status_label = dataset["label"]
        status_count = dataset["total"]
        status_color = dataset["background_color"]
        display_label = i18n_zh.STATUS_AGGREGATE.get(status_label, i18n_zh.tr(status_label))

        if display_label in merged:
            merged[display_label]["count"] += status_count
        else:
            merged[display_label] = {"count": status_count, "color": status_color}

    # Only include statuses with counts > 0
    for label, entry in merged.items():
        if entry["count"] > 0:
            chart_data["labels"].append(label)
            chart_data["datasets"][0]["data"].append(entry["count"])
            chart_data["datasets"][0]["backgroundColor"].append(entry["color"])

    return chart_data


def _top_rated_group_key(media_type, media):
    """Return the key used to dedupe repeated entries of the same item.

    A single item (e.g. a rewatched movie) can have multiple scored rows, so
    without this they'd surface as separate "duplicate" entries for what a
    user perceives as a single item. Seasons and their parent TV show are
    kept separate since they represent distinct ratings.
    """
    return (media_type, media.item_id)


def get_score_distribution(user_media):
    """Get score distribution for each media type within date range."""
    distribution = {}
    total_scored = 0
    total_score_sum = 0

    # One row per scored title, but only the four columns this function reads.
    # Building a full model instance per row costs ~0.1ms (model_utils'
    # FieldTracker copies every field on construction) and this library has
    # 1337 scored rows, so materialising them was most of the page's time.
    # The 14 cards that are actually rendered are built afterwards, once.
    top_rated_rows = {}
    # The model is the source of truth for the score range: the ``score`` field on
    # ``Media`` carries MinValueValidator(-10) / MaxValueValidator(10), so a rating
    # of -10 is a legal value. Upstream Yamtrack assumed 0..10 here and crashed
    # with ``KeyError: -10`` as soon as the library held a negative score, so the
    # buckets are built from the full validator range instead of the old guess.
    score_range = range(-10, 11)

    for media_type, media_list in user_media.items():
        score_counts = dict.fromkeys(score_range, 0)
        # Only these columns are read here, so drop any prefetch (e.g. the TV
        # seasons/episodes graph) that get_user_media attached — surviving
        # top-rated rows are re-fetched with the right prefetch afterwards.
        scored_media = (
            media_list.exclude(score__isnull=True)
            .prefetch_related(None)
            .values("id", "item_id", "score", "created_at")
        )

        for media in scored_media:
            key = (media_type, media["item_id"])
            existing = top_rated_rows.get(key)
            # Keep the score from the most recently created entry so repeated
            # or imported ratings for the same item aren't all listed.
            if existing is None or media["created_at"] > existing["created_at"]:
                # Each media table has its own id sequence, so the row has to
                # carry its own media type: book id 8 and movie id 8 both exist.
                media["media_type"] = media_type
                top_rated_rows[key] = media

            # ``int()`` truncates toward zero, so 9.5 lands in the "9" bucket and
            # -9.5 in the "-9" bucket, which keeps the halves symmetric. The
            # clamp is only a safety net: it keeps a stray out-of-range value in
            # the database from turning the whole statistics page into a 500.
            binned_score = max(min(int(media["score"]), score_range[-1]), score_range[0])
            score_counts[binned_score] += 1
            total_scored += 1
            total_score_sum += media["score"]

        distribution[media_type] = score_counts

    average_score = (
        round(total_score_sum / total_scored, 2) if total_scored > 0 else None
    )

    top_rated_count = 14
    top_rated_rows = sorted(
        top_rated_rows.values(),
        key=lambda media: (-float(media["score"]), -media["created_at"].timestamp()),
    )[:top_rated_count]

    top_rated_media = _annotate_top_rated_media(top_rated_rows)

    return {
        "labels": [str(score) for score in score_range],
        "datasets": [
            {
                "label": app_tags.media_type_readable(media_type),
                "data": [distribution[media_type][score] for score in score_range],
                "background_color": config.get_stats_color(media_type),
            }
            for media_type in distribution
        ],
        "average_score": average_score,
        "total_scored": total_scored,
    }, top_rated_media


def _annotate_top_rated_media(top_rated_rows):
    """Build the models for the chosen top-rated rows, with prefetch applied.

    ``top_rated_rows`` are the plain rows picked by ``get_score_distribution``;
    each carries its own ``media_type`` because every media table has its own
    id sequence, so an id alone does not say which model it belongs to.
    """
    if not top_rated_rows:
        return []

    media_by_type = {}
    for row in top_rated_rows:
        media_by_type.setdefault(row["media_type"], []).append(row["id"])

    media_manager = MediaManager()
    hydrated = {}

    for media_type, media_ids in media_by_type.items():
        model = apps.get_model(app_label="app", model_name=media_type)

        # Fetch fresh instances with proper relationships and annotations
        queryset = model.objects.filter(id__in=media_ids)
        queryset = media_manager._apply_prefetch_related(queryset, media_type)
        media_manager.annotate_max_progress(queryset, media_type)

        for media in queryset:
            hydrated[(media_type, media.id)] = media

    return [
        hydrated[(row["media_type"], row["id"])]
        for row in top_rated_rows
        if (row["media_type"], row["id"]) in hydrated
    ]



def get_status_color(status):
    """Get the color for the status of the media."""
    try:
        return config.get_status_stats_color(status)
    except KeyError:
        return "rgba(201, 203, 207)"


def _consumed_value_and_unit(media_type, queryset, item_count):
    """Return the consumed amount and its sub-unit noun for one media type.

    The sub-unit is ``None`` when the media type is counted as whole items
    (movies, finished books, watched shows). Returns ``None`` when nothing has
    been consumed.
    """
    if media_type == MediaTypes.TV.value:
        # Shows are counted, not their episodes: one row per show no matter how
        # many episodes were watched. ``progress`` is a computed property on TV
        # rather than an aggregatable column, which is why it is not summed.
        value = item_count
    elif media_type == MediaTypes.SEASON.value:
        # ``episodes`` are prefetched in get_user_media, so this reuses the
        # cache instead of issuing a query per season. The card counts
        # episodes, so it is labelled as episodes (see _consumption_label)
        # and carries no separate unit noun.
        value = sum(len(season.episodes.all()) for season in queryset)
        return (value, None) if value else None
    elif media_type == MediaTypes.MOVIE.value:
        # Whole movies are counted, and the item count is already at hand.
        value = item_count
    elif media_type == MediaTypes.BOOK.value:
        # Only books actually finished count, not the whole shelf: the item
        # count also holds in-progress, planned, paused and dropped books.
        # The caller's queryset already carries the date range, so filtering
        # it here inherits that range instead of re-deriving it. ``progress``
        # is not summed either: WeRead only reports a percent, so the sum
        # would be a pile of percentages labelled as pages.
        value = queryset.filter(status=Status.COMPLETED.value).count()
    else:
        # Dropped titles are left out of the consumed total as well: a game
        # marked dropped was given up, so the hours spent on it must not pad
        # the "played" number. The caller's queryset still carries dropped
        # rows (the status breakdown needs them), hence the exclude here.
        value = (
            queryset.exclude(status=Status.DROPPED.value).aggregate(
                total=models.Sum("progress"),
            )["total"]
            or 0
        )

    if not value:
        return None

    if media_type == MediaTypes.GAME.value:
        # ``progress`` is stored in minutes for games; "hour" is a unit of
        # time here, not a media-type name.
        value = round(value / 60)
        if not value:
            return None
        return value, "hour"

    if media_type in (MediaTypes.BOOK.value, MediaTypes.TV.value):
        # A finished book is counted as a whole book. Its ``progress`` is a
        # percent for the books that come from WeRead, so there is no page
        # total to report. A show is likewise one item, not a pile of episodes.
        return value, None

    # Sub-unit (Episode, Chapter, Page, ...) when the media type has one;
    # otherwise it is counted as whole items (movies).
    unit = config.get_config(media_type).get("unit")
    return value, unit[1].lower() if unit else None


def _consumption_label(media_type):
    """Human media type name for a consumption card."""
    if media_type == MediaTypes.TV.value:
        # Whole shows are counted, so the card names the show itself.
        return MediaTypes.TV.label
    if media_type == MediaTypes.SEASON.value:
        # The season card tallies episodes, so it is named after them; the
        # "TV Season" label would sit next to the show card and read as a
        # duplicate of it.
        return MediaTypes.EPISODE.value
    return MediaTypes(media_type).label


def _consumption_descriptor(media_type, value, unit_noun):
    """Build a descriptor like "TV episodes watched" or "Movies watched."""
    label = _consumption_label(media_type)
    verb = config.get_verb(media_type, past_tense=True)

    if str(get_language() or "").startswith("zh"):
        # 中文：「剧集 集（已看过）」「电影（已看过）」
        zh_label = i18n_zh.tr(label)
        zh_verb = i18n_zh.tr(verb)
        if unit_noun is None:
            return f"{zh_label}（已{zh_verb}）"
        zh_unit = i18n_zh.tr(unit_noun)
        if verb.startswith(unit_noun):
            return f"{zh_label} {zh_unit}"
        return f"{zh_label} {zh_unit}（已{zh_verb}）"

    plural = "" if value == 1 else "s"

    if unit_noun is None:
        # Whole items are counted, so the media type name is the noun.
        return f"{label}{plural} {verb}"
    if verb.startswith(unit_noun):
        # Avoid stutter such as "plays played".
        return f"{label} {unit_noun}{plural}"
    return f"{label} {unit_noun}{plural} {verb}"


def get_consumption_stats(user_media, media_count):
    """Aggregate how much of each media type the user has consumed.

    Returns one entry per media type with a real total (shows watched,
    chapters read, hours played, ...), ready for a card grid. Each
    descriptor names the media type so, e.g., TV and anime read
    distinctly. ``media_count`` supplies per-type counts already computed by
    get_user_media so movies and shows aren't counted a second time.
    """
    results = []
    for media_type, queryset in user_media.items():
        if media_type == MediaTypes.SEASON.value:
            # The season card only tallied episodes ("4141 episodes watched")
            # next to the show card that already counts whole shows, so it is
            # dropped at assembly time: the card is never built and the
            # template needs no change. Seasons still feed media_count and the
            # media type distribution, only the consumption card is gone.
            continue
        computed = _consumed_value_and_unit(
            media_type,
            queryset,
            media_count[media_type],
        )
        if computed is None:
            continue
        value, unit_noun = computed
        results.append(
            {
                "media_type": media_type,
                "value": value,
                "descriptor": _consumption_descriptor(media_type, value, unit_noun),
                "color": config.get_stats_color(media_type),
            },
        )

    return results


ZH_MONTHS = {
    "Jan": "1月", "Feb": "2月", "Mar": "3月", "Apr": "4月",
    "May": "5月", "Jun": "6月", "Jul": "7月", "Aug": "8月",
    "Sep": "9月", "Oct": "10月", "Nov": "11月", "Dec": "12月",
}


def _month_label(abbr):
    """中文界面下把英文月份缩写换成「N月」。"""
    if str(get_language() or "").startswith("zh"):
        return ZH_MONTHS.get(abbr, abbr)
    return abbr


def _build_month_labels(date_range, week_start_weekday):
    """Build month labels and their corresponding week counts for the activity grid."""
    months = []
    weeks_per_month = []
    current_month = date_range[0].strftime("%b")
    week_count = 0

    for current_date in date_range:
        if current_date.weekday() == week_start_weekday:
            month = current_date.strftime("%b")

            if current_month != month:
                if current_month is not None:
                    if week_count > 1:
                        months.append(_month_label(current_month))
                        weeks_per_month.append(week_count)
                    else:
                        months.append("")
                        weeks_per_month.append(week_count)
                current_month = month
                week_count = 0

            week_count += 1
    # For the last month
    if week_count > 1:
        months.append(_month_label(current_month))
        weeks_per_month.append(week_count)

    return months, weeks_per_month


def _as_date(value):
    """Return a ``datetime.date`` for a ``date`` or ``datetime`` input.

    Callers of the activity helpers pass either kind (the date-range parser
    yields datetimes, tests and historical buckets use plain dates), so every
    ``.date()`` below goes through this instead of assuming a datetime.
    """
    return value.date() if isinstance(value, datetime.datetime) else value


def get_activity_data(user, start_date, end_date):
    """Get daily activity counts for the last year."""
    if end_date is None:
        end_date = timezone.localtime()

    week_start_sunday = user.week_start_day == WeekStartDayChoices.SUNDAY
    start_date_aligned = get_aligned_week_start(
        start_date,
        week_start_sunday=week_start_sunday,
    )

    combined_data = get_filtered_historical_data(start_date_aligned, end_date, user)

    # update start_date values from historical records if not provided
    if start_date is None:
        dates = [item["date"] for item in combined_data]
        start_date = datetime.datetime.combine(
            min(dates) if dates else timezone.localdate(),
            datetime.time.min,
        )
        start_date_aligned = get_aligned_week_start(
            start_date,
            week_start_sunday=week_start_sunday,
        )

    # Aggregate counts by date
    date_counts = {}
    for item in combined_data:
        date = item["date"]
        date_counts[date] = date_counts.get(date, 0) + item["count"]

    # Both callers and tests may hand us either a ``datetime`` or a plain
    # ``date``; normalise through _as_date so this block never calls
    # ``.date()`` on something that is already a date.
    start_date_aligned_day = _as_date(start_date_aligned)
    end_date_day = _as_date(end_date)

    date_range = [
        start_date_aligned_day + datetime.timedelta(days=x)
        for x in range((end_date_day - start_date_aligned_day).days + 1)
    ]

    # Calculate activity statistics
    most_active_day, day_percentage = calculate_day_of_week_stats(
        date_counts,
        _as_date(start_date),
    )
    current_streak, longest_streak = calculate_streaks(
        date_counts,
        end_date_day,
    )

    # Create complete date range including padding days
    activity_data = [
        {
            "date": current_date.strftime("%Y-%m-%d"),
            "count": date_counts.get(current_date, 0),
            "level": get_level(date_counts.get(current_date, 0)),
        }
        for current_date in date_range
    ]

    # Format data into calendar weeks
    calendar_weeks = [activity_data[i : i + 7] for i in range(0, len(activity_data), 7)]

    # Generate months list with their week-start-day counts
    # The first day of each week column corresponds to the user's chosen week start day
    week_start_weekday = 6 if week_start_sunday else 0  # 0=Monday, 6=Sunday
    months, weeks_per_month = _build_month_labels(date_range, week_start_weekday)

    # Weekday labels depend on week start day
    if str(get_language() or "").startswith("zh"):
        days = ["一", "二", "三", "四", "五", "六", "日"]
    else:
        days = list(calendar.day_abbr)
    weekday_labels = [days[6], *days[0:6]] if week_start_sunday else days

    return {
        "calendar_weeks": calendar_weeks,
        "months": list(zip(months, weeks_per_month, strict=False)),
        "weekday_labels": weekday_labels,
        "stats": {
            "most_active_day": most_active_day,
            "most_active_day_percentage": day_percentage,
            "current_streak": current_streak,
            "longest_streak": longest_streak,
        },
    }


def get_aligned_week_start(datetime_obj, *, week_start_sunday=False):
    """Get the first day of the week containing the given date."""
    if datetime_obj is None:
        return None

    if week_start_sunday:
        # Sunday=weekday 6; if Sunday, subtract 0; else subtract (weekday+1)
        days_to_subtract = (datetime_obj.weekday() + 1) % 7
    else:
        days_to_subtract = datetime_obj.weekday()  # 0=Monday, 6=Sunday
    return datetime_obj - datetime.timedelta(days=days_to_subtract)


def get_level(count):
    """Calculate intensity level (0-4) based on count."""
    thresholds = [0, 3, 6, 9]
    for i, threshold in enumerate(thresholds):
        if count <= threshold:
            return i
    return 4


def get_filtered_historical_data(start_date, end_date, user):
    """Return [{"date": datetime.date, "count": int}]."""
    historical_models = BasicMedia.objects.get_historical_models()
    local_tz = timezone.get_current_timezone()

    day_buckets = defaultdict(int)

    for model_name in historical_models:
        model = apps.get_model("app", model_name)

        qs = model.objects.filter(history_user_id=user)

        if start_date:
            qs = qs.filter(history_date__gte=start_date)
        if end_date:
            qs = qs.filter(history_date__lte=end_date)

        # We only need the timestamp, stream results to keep memory usage flat
        for ts in qs.values_list("history_date", flat=True).iterator(chunk_size=2_000):
            aware_ts = timezone.localtime(ts, local_tz)

            # Safe as written: ``ts`` comes from a DateTimeField and
            # ``localtime`` returns a datetime, so ``.date()`` is always valid.
            day_buckets[aware_ts.date()] += 1

    combined_data = [
        {"date": day, "count": count} for day, count in day_buckets.items()
    ]

    logger.info("%s - built historical data (%s rows)", user, len(combined_data))
    return combined_data


def calculate_day_of_week_stats(date_counts, start_date):
    """Calculate the most active day of the week based on activity frequency.

    Returns the day name and its percentage of total activity.
    """
    # Initialize counters for each day of the week
    day_counts = defaultdict(int)
    total_active_days = 0

    # Count occurrences of each day of the week where activity happened
    for date in date_counts:
        if date < start_date:
            continue
        if date_counts[date] > 0:
            day_name = date.strftime("%A")  # Get full day name
            day_counts[day_name] += 1
            total_active_days += 1

    if not total_active_days:
        return None, 0

    # Find the most active day
    most_active_day = max(day_counts.items(), key=lambda x: x[1])
    percentage = (most_active_day[1] / total_active_days) * 100

    return _weekday_label(most_active_day[0]), round(percentage)


ZH_WEEKDAYS = {
    "Monday": "周一",
    "Tuesday": "周二",
    "Wednesday": "周三",
    "Thursday": "周四",
    "Friday": "周五",
    "Saturday": "周六",
    "Sunday": "周日",
}


def _weekday_label(name):
    """中文界面下把英文星期名换成中文。"""
    if str(get_language() or "").startswith("zh"):
        return ZH_WEEKDAYS.get(name, name)
    return name


def calculate_streaks(date_counts, end_date):
    """Calculate current and longest activity streaks."""
    # Get active dates and sort them in descending order (newest first)
    active_dates = sorted(
        [date for date, count in date_counts.items() if count > 0],
        reverse=True,
    )

    if not active_dates:
        return 0, 0

    longest_streak = 1
    streak_count = 1

    # Check if the most recent active date is today/end_date
    is_current = active_dates[0] == end_date

    current_streak = 1 if is_current else 0

    for i in range(1, len(active_dates)):
        # Check if this date is consecutive with the previous one
        if (active_dates[i - 1] - active_dates[i]).days == 1:
            streak_count += 1

            if is_current:
                current_streak += 1
        else:
            longest_streak = max(longest_streak, streak_count)
            streak_count = 1

            if is_current:
                is_current = False

    # Check final streak for longest calculation
    # needed if the last date is today/end_date
    longest_streak = max(longest_streak, streak_count)

    return current_streak, longest_streak


# ---------------------------------------------------------------------------
# 四张新图表：年度完成趋势 / 游玩时长 Top 10 / 成就完成度分布 / 发行年份分布。
#
# 这里全部走数据库聚合（values + annotate），绝不实例化模型，也不在循环里碰
# ``.item``：本站每实例化一个模型就要 deepcopy 一次（约 0.14ms/行），2783 个
# 游戏逐行读光实体化就要 0.4 秒。返回的 dict 只含 list/dict/str/数字，
# 进缓存不会碰到 pickle 问题。
# ---------------------------------------------------------------------------

EXTRA_CHARTS_CACHE_TTL = 600
# v2：账本 / 状态小计 / 年代行一起塞进这份缓存，结构变了必须换版本号，
# 否则旧缓存里没有这些键，页面上那几块会一直空着。
EXTRA_CHARTS_CACHE_VERSION = "v2"


def _empty_extra_chart():
    """空图结构：模板靠 ``labels`` 为空来显示空状态。"""
    return {"labels": [], "datasets": []}


def _has_field(model, field_name):
    """模型上是否真的有这个字段（TV/Season 就没有 end_date / progress）。"""
    try:
        model._meta.get_field(field_name)
    except Exception:  # noqa: BLE001 - FieldDoesNotExist，但几种 ORM 包装都吃掉
        return False
    return True


def _extra_charts_cache_key(user, start_date, end_date):
    """缓存键含用户和时间范围，起止日期都可能是 None（表示全部时间）。"""
    return (
        f"stats:extra_charts:{EXTRA_CHARTS_CACHE_VERSION}:{user.id}:"
        f"{start_date}:{end_date}"
    )


def _release_year_buckets(year_counts):
    """把「年份 -> 计数」摊成排好序的 (labels, data)。

    年份跨度超过 30 年时按年代合并（1990s/2000s），否则 X 轴会挤成一团。
    """
    years = sorted(year_counts)
    if not years:
        return [], []

    if years[-1] - years[0] <= 30:
        return [str(year) for year in years], [year_counts[year] for year in years]

    decade_counts = defaultdict(int)
    for year in years:
        decade_counts[(year // 10) * 10] += year_counts[year]
    decades = sorted(decade_counts)
    return (
        [f"{decade}s" for decade in decades],
        [decade_counts[decade] for decade in decades],
    )


def _get_extra_yearly_completed(user_media):
    """年度完成趋势：按 end_date 的年份聚合，按媒体类型分组堆叠。

    只统计状态是「已完成/已通关」的条目。``user_media`` 里的每个 queryset
    已经带好了用户选的时间范围，这里不再重复过滤。
    """
    from django.db.models.functions import ExtractYear

    counts = defaultdict(lambda: defaultdict(int))  # 年份 -> 媒体类型 -> 条数

    for media_type, queryset in user_media.items():
        # 剧集（TV）和季（Season）没有 end_date 字段：它们的完成时间记在分集上，
        # 直接给这两个表加 end_date 过滤会 FieldError，所以整类跳过。
        if not _has_field(queryset.model, "end_date"):
            continue
        rows = (
            queryset.prefetch_related(None)
            .filter(status=Status.COMPLETED.value, end_date__isnull=False)
            .values(year=ExtractYear("end_date"))
            .annotate(count=models.Count("id"))
        )
        for row in rows:
            if row["year"] is None:
                continue
            counts[row["year"]][media_type] += row["count"]

    if not counts:
        return _empty_extra_chart()

    years = sorted(counts)
    media_types = [
        media_type
        for media_type in user_media
        if any(counts[year].get(media_type, 0) for year in years)
    ]
    if not media_types:
        return _empty_extra_chart()

    return {
        "labels": [str(year) for year in years],
        "datasets": [
            {
                "label": app_tags.media_type_readable(media_type),
                "data": [counts[year].get(media_type, 0) for year in years],
                "background_color": config.get_stats_color(media_type),
            }
            for media_type in media_types
        ],
    }


def _get_extra_playtime_top(user_media):
    """游玩时长 Top 10：游戏的 progress 是分钟，换算成小时（1 位小数）。"""
    queryset = user_media.get(MediaTypes.GAME.value)
    if queryset is None:
        return _empty_extra_chart()

    rows = (
        queryset.prefetch_related(None)
        .filter(progress__gt=0)
        .values("item_id", "item__title")
        .annotate(total_minutes=models.Sum("progress"))
        .order_by("-total_minutes")[:10]
    )

    labels = []
    hours = []
    for row in rows:
        labels.append(row["item__title"] or "未知游戏")
        hours.append(round((row["total_minutes"] or 0) / 60, 1))

    if not labels:
        return _empty_extra_chart()

    return {
        "labels": labels,
        "datasets": [
            {
                "label": "小时",
                "data": hours,
                "background_color": config.get_stats_color(MediaTypes.GAME.value),
            },
        ],
    }


def _get_extra_achievement_buckets(user_media):
    """成就完成度分布：全成就 / 部分完成 / 无成就数据，只针对游戏。"""
    queryset = user_media.get(MediaTypes.GAME.value)
    if queryset is None:
        return _empty_extra_chart()

    # 一次 aggregate 用三个条件计数，别把行拉回 Python 再分类。
    totals = queryset.prefetch_related(None).aggregate(
        all_done=models.Count(
            "id",
            filter=models.Q(
                achievements_total__gt=0,
                achievements_unlocked__gte=models.F("achievements_total"),
            ),
        ),
        partial=models.Count(
            "id",
            filter=models.Q(
                achievements_total__gt=0,
                achievements_unlocked__lt=models.F("achievements_total"),
            ),
        ),
        no_data=models.Count("id", filter=models.Q(achievements_total=0)),
    )

    data = [totals["all_done"], totals["partial"], totals["no_data"]]
    if not any(data):
        return _empty_extra_chart()

    return {
        "labels": ["全成就", "部分完成", "无成就数据"],
        "datasets": [
            {
                "label": "游戏数",
                "data": data,
                "background_color": "#6366f1",
            },
        ],
    }


def _get_extra_release_years(user_media):
    """发行年份分布：所有媒体类型的 item.release_date 年份聚合，升序。"""
    from django.db.models.functions import ExtractYear

    year_counts = defaultdict(int)

    for queryset in user_media.values():
        rows = (
            queryset.prefetch_related(None)
            .filter(item__release_date__isnull=False)
            .values(year=ExtractYear("item__release_date"))
            .annotate(count=models.Count("id"))
        )
        for row in rows:
            if row["year"] is None:
                continue
            year_counts[row["year"]] += row["count"]

    if not year_counts:
        return _empty_extra_chart()

    labels, data = _release_year_buckets(year_counts)
    return {
        "labels": labels,
        "datasets": [
            {
                "label": "条目数",
                "data": data,
                "background_color": "#87ceeb",
            },
        ],
    }


# KPI 丝带 / 全库时长账本用的四种媒体：顺序固定（游戏 → 电影 → 书籍 → 剧集 → 季），
# 颜色取设计稿的语义色，模板里用 var(--st-<tone>) 取值，跟着主题自动换色。
_OVERVIEW_MEDIA_ROWS = (
    (MediaTypes.GAME.value, "violet", "款"),
    (MediaTypes.MOVIE.value, "sky", "部"),
    (MediaTypes.BOOK.value, "emerald", "本"),
    (MediaTypes.TV.value, "amber", "部"),
    (MediaTypes.SEASON.value, "gold", "季"),
)


def _format_int(value):
    """整数加千分位；None / 非法值一律当 0。"""
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return "0"


def _format_hours(value):
    """小时数展示：整数不带小数点，其余保留 1 位，都加千分位。"""
    try:
        hours = round(float(value or 0), 1)
    except (TypeError, ValueError):
        return "0"
    if abs(hours - round(hours)) < 1e-9:
        return f"{int(round(hours)):,}"
    return f"{hours:,.1f}"


def _percent(part, whole):
    """算百分比，保留 1 位小数；总量为 0 时直接给 0.0。"""
    if not whole:
        return 0.0
    return round(part / whole * 100, 1)


def _get_time_ledger(user, user_media, start_date, end_date):
    """全库时长账本：游戏 / 电影 / 剧集 / 书籍四维小时数与占比。

    ``views.py`` 里虽然已经算过 reading / watch，但那是视图自己注入 context 的字典，
    这里拿不到，所以账本自己再调一次 :mod:`app.duration_stats` 的只读聚合。
    两者都是纯聚合查询（几分钟的噪声），而且整份结果会进 ``get_extra_charts``
    的缓存，不会每刷一次页面都重算。
    """
    from app import duration_stats

    reading = duration_stats.get_reading_stats(user, start_date, end_date)
    watch = duration_stats.get_watch_stats(user, start_date, end_date)

    game_queryset = user_media.get(MediaTypes.GAME.value)
    game_hours = 0.0
    game_count = 0
    game_completed = 0
    if game_queryset is not None:
        games = game_queryset.prefetch_related(None).exclude(
            status=Status.DROPPED.value,
        )
        totals = games.aggregate(
            minutes=models.Sum("progress"),
            count=models.Count("id"),
            completed=models.Count(
                "id",
                filter=models.Q(status=Status.COMPLETED.value),
            ),
        )
        game_hours = round((totals["minutes"] or 0) / 60, 1)
        game_count = totals["count"] or 0
        game_completed = totals["completed"] or 0

    tv_hours = round(watch.get("tv_hours") or 0, 1)
    movie_hours = round(watch.get("movie_hours") or 0, 1)
    book_hours = round(reading.get("total_hours") or 0, 1)
    movie_count = watch.get("movie_count") or 0
    tv_episodes = watch.get("tv_episodes") or 0
    tv_count = (
        user_media[MediaTypes.TV.value].count()
        if MediaTypes.TV.value in user_media
        else 0
    )
    book_count = reading.get("book_count") or 0

    all_done = 0
    achievement = user_media.get(MediaTypes.GAME.value)
    if achievement is not None:
        all_done = (
            achievement.prefetch_related(None)
            .filter(
                achievements_total__gt=0,
                achievements_unlocked__gte=models.F("achievements_total"),
            )
            .count()
        )

    rows = [
        {
            "key": "game",
            "name": "🎮 游戏（已玩过）",
            "short": "🎮 游戏",
            "tone": "violet",
            "hours": game_hours,
            "hours_text": _format_hours(game_hours),
            "unit": "小时",
            "meta": (
                f"{_format_int(game_count)} 款入库 · "
                f"{_format_int(game_completed)} 款通关 · "
                f"{_format_int(all_done)} 款全成就"
            ),
        },
        {
            "key": "movie",
            "name": "🎬 电影（已看过）",
            "short": "🎬 电影",
            "tone": "sky",
            "hours": movie_hours,
            "hours_text": _format_hours(movie_hours),
            "unit": "小时",
            "meta": (
                f"{_format_int(movie_count)} 部 · 按实际片长精算"
            ),
        },
        {
            "key": "tv",
            "name": "📺 剧集（已看过）",
            "short": "📺 剧集",
            "tone": "amber",
            "hours": tv_hours,
            "hours_text": _format_hours(tv_hours),
            "unit": "小时",
            "meta": (
                f"{_format_int(tv_count)} 部 · {_format_int(tv_episodes)} 集"
                f" · 约 {_format_hours(tv_hours)} 小时"
            ),
        },
        {
            "key": "book",
            "name": "📖 书籍（已读过）",
            "short": "📖 书籍",
            "tone": "emerald",
            "hours": book_hours,
            "hours_text": _format_hours(book_hours),
            "unit": "小时",
            "meta": f"{_format_int(book_count)} 本有微信读书记录",
        },
    ]

    total_hours = round(sum(row["hours"] for row in rows), 1)
    for row in rows:
        row["percent"] = _percent(row["hours"], total_hours)
        row["percent_text"] = f"{row['percent']:.1f}%"

    peak_year = ""
    peak_hours = 0.0
    for row in reading.get("by_year") or []:
        if (row.get("hours") or 0) > peak_hours:
            peak_hours = row["hours"]
            peak_year = row.get("year") or ""

    return {
        "total_hours": total_hours,
        "total_text": _format_hours(total_hours),
        "items": rows,
        "reading_peak_year": peak_year,
        "reading_peak_hours": _format_hours(peak_hours),
        "has_data": any(row["hours"] > 0 for row in rows),
    }


def _get_overview_summary(user_media, achievement_buckets):
    """KPI 丝带要用的分成、比例和小灰字。

    模板里做不了除法，也不能把 queryset 直接丢给模板算百分比，所以这里一次性
    把「四类媒体各自的条数 / 已完成 / 进行中 / 已评分」和几行小灰字拼好。
    """
    rows = []
    for media_type, tone, unit in _OVERVIEW_MEDIA_ROWS:
        queryset = user_media.get(media_type)
        if queryset is None:
            continue

        base = queryset.prefetch_related(None)
        if media_type not in (MediaTypes.TV.value, MediaTypes.SEASON.value):
            # 和 get_user_media 的口径保持一致：放弃的条目不进总数。
            base = base.exclude(status=Status.DROPPED.value)

        stats = base.aggregate(
            total=models.Count("id"),
            completed=models.Count(
                "id",
                filter=models.Q(status=Status.COMPLETED.value),
            ),
            in_progress=models.Count(
                "id",
                filter=models.Q(status=Status.IN_PROGRESS.value),
            ),
        )
        # 评分口径单独算：get_score_distribution 统计的是「所有打过分的条目」，
        # 包括已放弃的，所以这里也不能排除 Dropped，否则卡片会和右侧直方图对不上。
        scored_stats = queryset.prefetch_related(None).aggregate(
            scored=models.Count("id", filter=models.Q(score__isnull=False)),
            score_sum=models.Sum("score"),
        )
        rows.append(
            {
                "key": media_type,
                "label": app_tags.media_type_readable(media_type),
                "tone": tone,
                "unit": unit,
                "count": stats["total"] or 0,
                "completed": stats["completed"] or 0,
                "in_progress": stats["in_progress"] or 0,
                "scored": scored_stats["scored"] or 0,
                "score_sum": float(scored_stats["score_sum"] or 0),
            }
        )

    media_total = sum(row["count"] for row in rows)
    scored_total = sum(row["scored"] for row in rows)
    score_sum = sum(row["score_sum"] for row in rows)
    average_score = round(score_sum / scored_total, 2) if scored_total else 0.0

    for row in rows:
        row["percent"] = _percent(row["count"], media_total)
        row["percent_text"] = f"{row['percent']:.1f}%"
        row["count_text"] = _format_int(row["count"])

    # 「季」和「剧集」在库里是两张表，但读起来是一回事：KPI 的总条目要算上它
    # （4429 里含 170 季），比例条和「几类媒体」却只数四大类，否则会出现
    # 「剧集 170 · 季 170」两条一模一样的 3.8%。
    media_split = [row for row in rows if row["count"] > 0 and row["key"] != MediaTypes.SEASON.value]
    split_total = sum(row["count"] for row in media_split)
    for row in media_split:
        row["split_percent"] = _percent(row["count"], split_total)
        row["split_percent_text"] = f"{row['split_percent']:.1f}%"
    completed_split = [
        dict(row, percent=_percent(row["completed"], sum(r["completed"] for r in rows)))
        for row in rows
        if row["completed"] > 0
    ]
    in_progress_split = [
        dict(
            row,
            percent=_percent(
                row["in_progress"],
                sum(r["in_progress"] for r in rows),
            ),
        )
        for row in rows
        if row["in_progress"] > 0
    ]

    def _subtext(items, field, unit_map):
        return " · ".join(
            f"{item['label']} {_format_int(item[field])} {unit_map(item)}"
            for item in items
        )

    # 剧集和季在库里是两张表，展示时合并成一项，避免「剧集 170 · 季 170」这种重复。
    tv_completed = sum(
        row["completed"]
        for row in rows
        if row["key"] in (MediaTypes.TV.value, MediaTypes.SEASON.value)
    )
    completed_subtext = " · ".join(
        part
        for part in (
            (
                f"电影 {_format_int(next((r['completed'] for r in rows if r['key'] == MediaTypes.MOVIE.value), 0))}"
            ),
            (
                f"游戏 {_format_int(next((r['completed'] for r in rows if r['key'] == MediaTypes.GAME.value), 0))}"
            ),
            f"剧集/季 {_format_int(tv_completed)}",
            (
                f"书籍 {_format_int(next((r['completed'] for r in rows if r['key'] == MediaTypes.BOOK.value), 0))}"
            ),
        )
        if part
    )

    achievement_rows = []
    achie_labels = (achievement_buckets or {}).get("labels") or []
    achie_data = ((achievement_buckets or {}).get("datasets") or [{}])[0].get("data") or []
    achie_total = sum(achie_data)
    for index, label in enumerate(achie_labels):
        count = achie_data[index] if index < len(achie_data) else 0
        achievement_rows.append(
            {
                "label": label,
                "count": count,
                "count_text": _format_int(count),
                "percent": _percent(count, achie_total),
                "percent_text": f"{_percent(count, achie_total):.1f}%",
                "tone": ("gold", "violet", "muted")[index]
                if index < 3
                else "muted",
            }
        )

    return {
        "media_total": media_total,
        "media_total_text": _format_int(media_total),
        "media_kinds": len(media_split),
        "media_split": media_split,
        "media_subtext": " · ".join(
            f"{row['label']} {row['count_text']}"
            for row in media_split
            if row["key"] != MediaTypes.SEASON.value
        ),
        "completed_total": sum(row["completed"] for row in rows),
        "completed_text": _format_int(sum(row["completed"] for row in rows)),
        "completed_subtext": completed_subtext,
        "completed_split": completed_split,
        "in_progress_total": sum(row["in_progress"] for row in rows),
        "in_progress_text": _format_int(sum(row["in_progress"] for row in rows)),
        "in_progress_split": in_progress_split,
        "in_progress_subtext": _subtext(
            in_progress_split,
            "in_progress",
            lambda item: item["unit"],
        ),
        "scored_total": scored_total,
        "scored_text": _format_int(scored_total),
        "rated_percent": round(_percent(scored_total, media_total)),
        "average_score": average_score,
        "achievement_total": achie_total,
        "achievement_total_text": _format_int(achie_total),
        "achievement_rows": achievement_rows,
        "has_data": media_total > 0,
    }


def _get_playtime_rows(chart_data):
    """把 Top 10 的 {labels, datasets[0].data} 摊成带百分比的行，给 HTML 榜单用。"""
    labels = (chart_data or {}).get("labels") or []
    datasets = (chart_data or {}).get("datasets") or []
    if not labels or not datasets:
        return []
    values = datasets[0].get("data") or []
    peak = max(values) if values else 0
    rows = []
    for index, label in enumerate(labels):
        hours = values[index] if index < len(values) else 0
        rows.append(
            {
                "rank": index + 1,
                "name": label,
                "hours": hours,
                "hours_text": _format_hours(hours),
                "percent": _percent(hours, peak),
            }
        )
    return rows


def _get_release_year_rows(chart_data):
    """发行年份 / 年代分布摊成行（横条 + 百分比）。"""
    labels = (chart_data or {}).get("labels") or []
    datasets = (chart_data or {}).get("datasets") or []
    if not labels or not datasets:
        return []
    values = datasets[0].get("data") or []
    peak = max(values) if values else 0
    total = sum(values)
    rows = []
    for index, label in enumerate(labels):
        count = values[index] if index < len(values) else 0
        rows.append(
            {
                "label": label,
                "count": count,
                "count_text": _format_int(count),
                "percent": _percent(count, peak),
                "share_text": f"{_percent(count, total):.1f}%",
            }
        )
    # 从近到远排：年代越新越靠上，和设计稿一致。
    return list(reversed(rows))


def get_extra_charts(user, user_media, start_date, end_date):
    """返回四张图 + 概览 / 账本 / 榜单行的数据。

    键名固定为 ``yearly_completed`` / ``playtime_top`` / ``achievement_buckets``
    / ``release_years``，另外附带几个给新模板直接渲染用的纯数据键。
    """
    from django.core.cache import cache

    cache_key = _extra_charts_cache_key(user, start_date, end_date)
    cached = None
    try:
        cached = cache.get(cache_key)
    except Exception:  # noqa: BLE001 - 缓存只是加速，坏了不能拖垮页面
        logger.warning("%s - extra charts cache read failed, computing", user)

    if cached is not None:
        return cached

    playtime_top = _get_extra_playtime_top(user_media)
    achievement_buckets = _get_extra_achievement_buckets(user_media)
    release_years = _get_extra_release_years(user_media)

    charts = {
        "yearly_completed": _get_extra_yearly_completed(user_media),
        "playtime_top": playtime_top,
        "achievement_buckets": achievement_buckets,
        "release_years": release_years,
        # 下面几项不是 Chart.js 的数据，而是模板直接渲染 HTML 用的纯数据行。
        "overview": _get_overview_summary(user_media, achievement_buckets),
        "time_ledger": _get_time_ledger(user, user_media, start_date, end_date),
        "playtime_rows": _get_playtime_rows(playtime_top),
        "release_year_rows": _get_release_year_rows(release_years),
    }

    try:
        cache.set(cache_key, charts, EXTRA_CHARTS_CACHE_TTL)
    except Exception:  # noqa: BLE001 - 同上，写缓存失败不影响返回
        logger.warning("%s - extra charts cache write failed", user)

    return charts
