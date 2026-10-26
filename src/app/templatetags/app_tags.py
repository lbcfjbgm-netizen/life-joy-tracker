from pathlib import Path

from django import template
from django.conf import settings
from django.urls import NoReverseMatch, reverse
from django.utils import formats, timezone
from django.utils.dateparse import parse_date
from django.utils.html import format_html
from unidecode import unidecode

from app import config, helpers, i18n_messages, i18n_zh, image_relay
from app.models import MediaTypes, Sources, Status
from users.models import WATCH_PROVIDER_REGION_UNSET

register = template.Library()


@register.simple_tag
def get_static_file_mtime(file_path):
    """Return the last modification time of a static file for cache busting."""
    full_path = Path(settings.STATIC_ROOT) / file_path
    try:
        mtime = int(full_path.stat().st_mtime)
    except OSError:
        # If file doesn't exist or can't be accessed
        return ""
    else:
        return f"?{mtime}"


@register.simple_tag(takes_context=True)
def absolute_app_url(context, path):
    """Return an absolute app URL for links copied into external services."""
    return helpers.build_absolute_app_url(context.get("request"), path)


@register.filter
def no_underscore(arg1):
    """Return the title case of the string."""
    return arg1.replace("_", " ")


@register.filter
def slug(arg1):
    """Return the slug of the string.

    Sometimes slugify removes all characters from a string, so we need to
    urlencode the special characters first.
    e.g Anime: 31687
    """
    cleaned = template.defaultfilters.slugify(arg1)
    if cleaned == "":
        cleaned = template.defaultfilters.slugify(
            template.defaultfilters.urlencode(unidecode(arg1)),
        )
        if cleaned == "":
            cleaned = template.defaultfilters.urlencode(unidecode(arg1))

            if cleaned == "":
                cleaned = template.defaultfilters.urlencode(arg1)

    return cleaned


@register.filter
def date_format(datetime, user):
    """Format a datetime using user's preferred date format (date only, no time).

    Args:
        datetime: The datetime object to format
        user: User object to get preferred date format
    """
    if not datetime:
        return None
    local_dt = timezone.localtime(datetime)
    return formats.date_format(local_dt, user.date_format)


@register.filter
def iso_date_format(value, user):
    """Format an ISO date string (YYYY-MM-DD) using user's preferred date format.

    If value is not a valid ISO date string, returns the original value.
    """
    if isinstance(value, str):
        date_obj = parse_date(value)
        if date_obj:
            return formats.date_format(date_obj, user.date_format)

    return value


@register.filter
def time_format(datetime, user):
    """Format a datetime using user's preferred time format (time only, no date)."""
    if not datetime:
        return None
    local_dt = timezone.localtime(datetime)
    return formats.time_format(local_dt, user.time_format)


@register.filter
def datetime_format(datetime, user):
    """Format a datetime using user's preferred formats.

    Includes time only if TRACK_TIME setting is enabled.

    Args:
        datetime: The datetime object to format
        user: User object to get preferred date/time format
    """
    if not datetime:
        return None
    local_dt = timezone.localtime(datetime)
    formatted_date = formats.date_format(local_dt, user.date_format)

    if settings.TRACK_TIME:
        formatted_time = formats.time_format(local_dt, user.time_format)
        return f"{formatted_date} {formatted_time}"
    return formatted_date


@register.filter
def is_list(arg1):
    """Return True if the object is a list."""
    return isinstance(arg1, list)


@register.filter
def source_readable(source):
    """Return the readable source name."""
    return i18n_zh.tr(Sources(source).label)


@register.filter
def media_type_readable(media_type):
    """Return the readable media type."""
    return i18n_zh.media_type_label(media_type)


@register.filter
def media_type_readable_plural(media_type):
    """Return the readable media type in plural form."""
    return i18n_zh.media_type_label(media_type, plural=True)


@register.filter
def media_status_readable(media_status):
    """Return the readable media status.

    两种用法：
    - 传媒体对象（卡片、列表行、详情页的状态）：Completed 会用 is_all_achievements
      拆成「全成就」/「已通关」；拿得到媒体类型时还会换成人话词（电影「已观看」、
      书「已读完」），见 i18n_zh.STATUS_BY_MEDIA_TYPE；
    - 传纯字符串（老用法，比如下拉选项、区块标题、状态筛选）：拿不到成就信息和
      媒体类型，Completed 一律显示「已通关」。
    """
    if not isinstance(media_status, str):
        is_all = bool(getattr(media_status, "is_all_achievements", False))
        item = getattr(media_status, "item", None)
        media_type = getattr(item, "media_type", None)
        media_status = getattr(media_status, "status", None)
        if media_status is None:
            return ""
        if is_all and media_status == Status.COMPLETED.value:
            return i18n_zh.tr("All achievements")
        # 游戏和动画之外的媒体类型换成人话词；游戏仍按统一说法叫「已通关」。
        if media_type in i18n_zh.STATUS_BY_MEDIA_TYPE:
            label = i18n_zh.STATUS_BY_MEDIA_TYPE[media_type].get(media_status)
            if label:
                return label
    return i18n_zh.tr(Status(media_status).label)


@register.filter
def default_source(media_type):
    """Return the default source for the media type."""
    return i18n_zh.tr(config.get_default_source_name(media_type).label)


@register.filter
def media_past_verb(media_type):
    """Return the past tense verb for the given media type."""
    return i18n_zh.tr(config.get_verb(media_type, past_tense=True))


@register.filter
def sample_search(media_type):
    """Return a sample search URL for the given media type using GET parameters."""
    return config.get_sample_search_url(media_type)


@register.filter
def short_unit(media_type):
    """Return the short unit for the media type."""
    return i18n_zh.tr(config.get_unit(media_type, short=True))


@register.filter
def long_unit(media_type):
    """Return the long unit for the media type."""
    return i18n_zh.tr(config.get_unit(media_type, short=False))


@register.filter
def sources(media_type):
    """Template filter to get source options for a media type."""
    return [
        {"value": source.value, "label": i18n_zh.tr(source.label)}
        for source in config.get_sources(media_type)
    ]


@register.simple_tag
def get_search_media_types(user):
    """Return available media types for search based on user preferences."""
    enabled_types = user.get_enabled_media_types()

    # Filter and format the types for search
    return [
        {
            "display": media_type_readable_plural(media_type),
            "value": media_type,
        }
        for media_type in enabled_types
        if media_type != MediaTypes.SEASON.value
    ]


@register.simple_tag
def get_sidebar_media_types(user):
    """Return available media types for sidebar navigation based on user preferences."""
    enabled_types = user.get_enabled_media_types()

    # Format the types for sidebar
    return [
        {
            "media_type": media_type,
            "display_name": media_type_readable_plural(media_type),
        }
        for media_type in enabled_types
    ]


@register.filter
def media_color(media_type):
    """Return the color associated with the media type."""
    return config.get_text_color(media_type)


@register.filter
def journal_accent(accent):
    """Return the badge background class and icon template for a journal accent."""
    return config.get_journal_accent(accent)


@register.filter
def status_config(status):
    """Return the config dict for a status, or None if it is unrecognized."""
    return config.get_status_config(status)


@register.filter
def status_color(status):
    """Return the color associated with the status."""
    return config.get_status_text_color(status)


@register.filter
def status_icon(status):
    """Return the icon template associated with the status."""
    return config.get_status_icon(status)


@register.filter
def status_background_color(status):
    """Return the background color associated with the status."""
    return config.get_status_background_color(status)


@register.filter
def status_button_color(status):
    """Return the button background and hover classes for the status."""
    return config.get_status_button_color(status)


@register.filter
def natural_day(datetime, user):
    """Format date with natural language (Today, Tomorrow, etc.)."""
    if not datetime:
        return None

    today = timezone.localdate()

    local_dt = timezone.localtime(datetime)
    datetime_date = local_dt.date()
    formatted_date = formats.date_format(local_dt, user.date_format)
    formatted_time = formats.time_format(local_dt, user.time_format)
    days = (datetime_date - today).days

    if days == 0:
        return f"Today {formatted_time}"
    if days == 1:
        return f"Tomorrow {formatted_time}"

    return f"{formatted_date} {formatted_time}"


@register.filter
def media_url(media):
    """Return the media URL for both metadata and model object cases."""
    is_dict = isinstance(media, dict)

    # Get attributes using either dict access or object attribute
    media_type = media["media_type"] if is_dict else media.media_type
    source = media["source"] if is_dict else media.source
    media_id = media["media_id"] if is_dict else media.media_id
    title = media["title"] if is_dict else media.title

    if media_type in [MediaTypes.SEASON.value, MediaTypes.EPISODE.value]:
        season_number = media["season_number"] if is_dict else media.season_number
        return reverse(
            "season_details",
            kwargs={
                "source": source,
                "media_id": media_id,
                "title": slug(title),
                "season_number": season_number,
            },
        )

    return reverse(
        "media_details",
        kwargs={
            "source": source,
            "media_type": media_type,
            "media_id": media_id,
            "title": slug(title),
        },
    )


@register.simple_tag
def media_view_url(view_name, media):
    """Return the modal URL for both metadata and model object cases.

    An episode dict built by a provider that does not list its episodes may lack
    ``source`` / ``media_type`` / ``media_id``. A missing one yields an empty
    URL instead of a ``KeyError``: ``hx-get=""`` does nothing, a 500 loses the
    whole page.
    """
    is_dict = isinstance(media, dict)

    # Build kwargs using either dict access or object attribute
    kwargs = {
        "source": media.get("source") if is_dict else media.source,
        "media_type": media.get("media_type") if is_dict else media.media_type,
        "media_id": media.get("media_id") if is_dict else media.media_id,
    }
    if any(value is None for value in kwargs.values()):
        return ""

    # Handle season/episode numbers if they exist
    if is_dict:
        if media.get("season_number") is not None:
            kwargs["season_number"] = media["season_number"]
        if media.get("episode_number") is not None:
            kwargs["episode_number"] = media["episode_number"]
    else:
        if media.season_number is not None:
            kwargs["season_number"] = media.season_number
        if media.episode_number is not None:
            kwargs["episode_number"] = media.episode_number

    try:
        return reverse(view_name, kwargs=kwargs)
    except NoReverseMatch:
        # A provider-built dict can also lack the season number the route wants.
        # Same rule as above: no URL at all is better than a dead page.
        return ""


@register.simple_tag
def component_id(component_type, media, instance_id=None):
    """Return the component ID for both metadata and model object cases."""
    is_dict = isinstance(media, dict)

    # Get base attributes using either dict access or object attribute
    media_type = media["media_type"] if is_dict else media.media_type
    media_id = media["media_id"] if is_dict else media.media_id

    component_id = f"{component_type}-{media_type}-{media_id}"

    # Handle season/episode numbers if they exist
    if is_dict:
        if "season_number" in media:
            component_id += f"-{media['season_number']}"
        if "episode_number" in media:
            component_id += f"-{media['episode_number']}"
    else:
        if media.season_number is not None:
            component_id += f"-{media.season_number}"
        if media.episode_number is not None:
            component_id += f"-{media.episode_number}"

    # Add instance id if provided
    if instance_id:
        component_id += f"-{instance_id}"

    return component_id


@register.simple_tag
def unicode_icon(name):
    """Return the Unicode icon for the media type."""
    return config.get_unicode_icon(name)


@register.simple_tag
def icon(name, is_active, extra_classes="w-5 h-5"):
    """Return the SVG icon for the given name."""
    base_svg = """<svg xmlns="http://www.w3.org/2000/svg"
                      width="24"
                      height="24"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="2"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                      class="{active_class}{extra_classes}">
                      {content}
                 </svg>"""

    content = config.get_svg_icon(name)
    active_class = "text-indigo-400 " if is_active else ""

    svg = base_svg.format(
        content=content,
        active_class=active_class,
        extra_classes=extra_classes,
    )

    return format_html(svg)


@register.filter
def str_equals(value, arg):
    """Return True if the string value is equal to the argument."""
    return str(value) == str(arg)


@register.filter
def get_range(value):
    """Return a range from 1 to the given value."""
    return range(1, int(value) + 1)


@register.simple_tag
def get_pagination_range(current_page, total_pages, window):
    """
    Return a list of page numbers to display in pagination.

    Args:
        current_page: The current page number
        total_pages: Total number of pages
        window: Number of pages to show before and after current page

    Returns:
        A list of page numbers and None values (for ellipses)
    """
    if total_pages <= 5 + window * 2:
        # If few pages, show all
        return list(range(1, total_pages + 1))

    # Calculate left and right boundaries
    left_boundary = max(2, current_page - window)
    right_boundary = min(total_pages - 1, current_page + window)

    # Add ellipsis indicators and page numbers
    result = [1]

    second_page = 2
    # Add left ellipsis if needed
    if left_boundary > second_page:
        result.append(None)  # None represents ellipsis

    # Add pages around current page
    result.extend(range(left_boundary, right_boundary + 1))

    # Add right ellipsis if needed
    if right_boundary < total_pages - 1:
        result.append(None)  # None represents ellipsis

    # Add last page if not already included
    if total_pages not in result:
        result.append(total_pages)

    return result


@register.filter
def hours_brief(minutes):
    """Return a minute count as a compact hour number, e.g. 1716 -> '28.6'.

    Cards show "28.6h 游玩" instead of "28h 36min" so the line stays on one row.
    Returns an empty string when there is nothing to show, so templates can
    test the value before printing the unit.
    """
    try:
        value = float(minutes or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return f"{value / 60:.1f}"


@register.filter
def show_media_score(rating, user):
    """
    Return if we should show the rating of a media.

    Args:
        rating: the rating value of the media
        user: the user to check preferences for

    Returns:
        True if we should show the media score
    """
    return rating is not None and (not user.hide_zero_rating or rating > 0)


@register.simple_tag
def media_section_count(
    media,
    user_medias,
    watch_providers=None,
    watch_provider_region=None,
):
    """Return the number of content sections on the media details page."""
    count = 0
    if media.get("cast"):
        count += 1
    related = media.get("related") or {}
    count += sum(1 for related_items in related.values() if related_items)
    if len(user_medias) > 1:
        count += 1
    if media.get("episodes"):
        count += 1
    has_streaming = (
        watch_provider_region == WATCH_PROVIDER_REGION_UNSET or watch_providers
    )
    if (
        media.get("media_type") in (MediaTypes.MOVIE.value, MediaTypes.TV.value)
        and has_streaming
    ):
        count += 1
    if media.get("time_to_beat"):
        count += 1
    return count


@register.filter
def seconds_to_duration(seconds):
    """Convert seconds to human-readable duration.

    Under 30 min: rounds to nearest 5 min. 30 min and above: rounds to nearest 30 min.
    """
    if not seconds:
        return None
    total_minutes = seconds // 60
    if total_minutes < 30:  # noqa: PLR2004
        return f"{max(5, round(total_minutes / 5) * 5)}m"
    hours, minutes = divmod(total_minutes, 60)
    if hours == 0:
        return "30m" if minutes < 45 else "1h"  # noqa: PLR2004
    if minutes >= 45:  # noqa: PLR2004
        return f"{hours + 1}h"
    return f"{hours}h" if minutes < 15 else f"{hours}h 30m"  # noqa: PLR2004


@register.filter
def localize_message(message):
    """Translate a flash message / form error for the current language."""
    return i18n_messages.tr_message(message)


@register.filter
def relay_image(url):
    """Return a local URL for cover images served through the local cache."""
    return image_relay.localize(url)


@register.filter
def hero_image(media_id):
    """Return the local URL of a game's wide ``library_hero.jpg``.

    The URL is built from the appid so the template never pastes a raw Steam
    host into the page; it then goes through the same cache as the covers.
    Non-digit ids (films, books) return "" so nothing is requested.
    """
    return image_relay.localize(image_relay.steam_hero_url(media_id))


@register.filter
def seq_watermark(value):
    """列表页首卡右下角的大号序号水印：位数 + 1，再左补 0 到等长。

    1~9 => 01；10~99 => 010；100~999 => 0100；1000+ => 01000。
    同位数内等长，跨位数按「位数 + 1」加宽。非数字返回空串。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ""
    if n < 1:
        return ""
    return str(n).zfill(len(str(n)) + 1)
