import datetime
import logging
from pathlib import Path
from urllib.parse import urlencode

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import IntegrityError, connection
from django.db.models import Prefetch, Q, prefetch_related_objects
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseNotFound,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from app import (
    config,
    duration_stats,
    helpers,
    history_processor,
    i18n_zh,
    image_relay,
    status_filters,
    steam_screenshots,
)
from app import home as home_helpers
from app import near_complete as near_complete_agg
from app import playtime as stats_playtime
from app import statistics as stats
from app import year_in_review as year_review
from app.forms import EpisodeForm, ManualItemForm, get_form_class
from app.models import (
    TV,
    BasicMedia,
    Episode,
    Highlight,
    Item,
    MediaTypes,
    Note,
    Season,
    Sources,
    Status,
    UserMessage,
)
from app.providers import manual, services, tmdb
from app.templatetags import app_tags
from events.models import Event
from users.models import (
    DateFormatChoices,
    HomeSortChoices,
    LayoutChoices,
    MediaSortChoices,
    MediaStatusChoices,
    User,
)

logger = logging.getLogger(__name__)

# Highest page number the search page will pass on to a provider. Steam's
# results arrive in a single batch and are sliced locally, so a huge page
# number is only a very large offset; capping keeps that bounded.
MAX_SEARCH_PAGE = 1000


# ---------------------------------------------------------------------------
# 列表页「每个库各记各的」偏好（session 里按 media_type 分桶）
#
# 为什么不用数据库：users.User 是按媒体类型分了 layout/sort/status 三列，但
# 卡片密度 list_density 只有**一个全局列**，游戏和电影共用一份，这就是
# 「选了巨幕，点游戏是巨幕、点电影也变巨幕」的根因；而且那一列的 check
# constraint 只允许 ""/4/8/12/16，巨幕档 "1" 和伪状态 "All achievements"
# 根本存不进去。session 不受数据库约束，所以四项共用一份 session 字典：
#
#     request.session["lj_list_prefs"] = {
#         "game":  {"density": "1", "status": "All achievements",
#                   "sort": "end_date", "layout": "grid", "lead": "1"},
#         "movie": {"density": "4", "status": "Completed", ...},
#     }
#
# 搜索关键词故意不存（用户明确要求「搜索不用记」）。
# ---------------------------------------------------------------------------
PREF_SESSION_KEY = "lj_list_prefs"

# session 里最多存这四项；lead 单独走 PREF_LEAD。
PREF_NAMES = ("layout", "sort", "status", "density")
PREF_LEAD = "lead"

# 密度档位。注意 "1"（巨幕）只在游戏库成立，写库时永远不写 "1"。
VALID_DENSITIES = {"4", "8", "12", "16"}
DENSITY_HERO = "1"

_VALID_LAYOUTS = {value for value, _ in LayoutChoices.choices}
_VALID_SORTS = {value for value, _ in MediaSortChoices.choices}


def _get_list_prefs(request):
    """取出整个偏好字典（没存过就是空字典）。"""
    prefs = request.session.get(PREF_SESSION_KEY)
    return prefs if isinstance(prefs, dict) else {}


def _get_list_pref_bucket(request, media_type):
    """取出某个库自己那一桶（没存过就是空字典）。"""
    bucket = _get_list_prefs(request).get(media_type)
    return bucket if isinstance(bucket, dict) else {}


def _set_list_pref(request, media_type, name, value):
    """写一项偏好。只有值真的变了才改 session（避免每次请求都写库）。

    注意必须显式给 request.session[key] 赋值，session 才会被标记 modified；
    只改嵌套字典里的值 Django 察觉不到。
    """
    if not media_type:
        return
    prefs = dict(_get_list_prefs(request))
    bucket = dict(_get_list_pref_bucket(request, media_type))
    if bucket.get(name) == value:
        return
    bucket[name] = value
    prefs[media_type] = bucket
    request.session[PREF_SESSION_KEY] = prefs


def _clean_pref_value(name, raw, media_type):
    """把 URL 里的原始值校验成可存的干净值，脏值返回 None（不存）。"""
    if raw is None:
        return None
    if name == "density":
        # 巨幕 "1"（含老链接的 auto 已在调用前换算）只认游戏库；
        # 非游戏库里出现 "1" 是脏值，不记。
        if raw == DENSITY_HERO:
            return DENSITY_HERO if media_type == MediaTypes.GAME.value else None
        return raw if raw in VALID_DENSITIES else None
    if name == "status":
        return raw if raw in status_filters.FILTER_VALUES else None
    if name == "sort":
        return raw if raw in _VALID_SORTS else None
    if name == "layout":
        return raw if raw in _VALID_LAYOUTS else None
    if name == PREF_LEAD:
        return raw if raw in {"1", "0"} else None
    return None


def _list_param_names():
    """URL 里用来表达这 5 项的参数名（lead 用 "lead"）。"""
    return (*PREF_NAMES, PREF_LEAD)


def _full_list_params(saved):
    """把某个库 session 里记的偏好拼成 URL 参数（顺序固定，便于肉眼比对）。

    只输出合法值；空桶返回空列表，调用方据此决定「不跳 / 不推 URL」。
    """
    bucket = saved if isinstance(saved, dict) else {}
    params = []
    for name in PREF_NAMES:
        value = bucket.get(name)
        if name == "density":
            if value == DENSITY_HERO or value in VALID_DENSITIES:
                params.append((name, value))
        elif name == "status":
            if value in status_filters.FILTER_VALUES:
                params.append((name, value))
        elif name == "sort":
            if value in _VALID_SORTS:
                params.append((name, value))
        elif name == "layout":
            if value in _VALID_LAYOUTS:
                params.append((name, value))
    lead = bucket.get(PREF_LEAD)
    if lead in {"1", "0"}:
        params.append((PREF_LEAD, lead))
    return params


def _saved_game_status(saved, target_user):
    """游戏库的状态三层优先级：session → 数据库偏好 → 「全成就」。

    - session 里存过（含伪值 "All achievements"）就用它，这是**分库**的保证
    - 否则退回库里存的 game_status（真实状态：All / Completed / ...）
    - 都是空/脏值才落到原来的默认「全成就」

    以前这一支无条件返回 ALL_ACHIEVEMENTS，库里存什么都会被盖掉，
    这就是用户说的「换了状态，下次进来又变回全成就」。
    """
    cand = (saved or {}).get("status") or ""
    if cand in status_filters.FILTER_VALUES:
        return cand
    db_status = getattr(target_user, "game_status", "") or ""
    if db_status in Status.values:
        return db_status
    return status_filters.ALL_ACHIEVEMENTS


@require_GET
def home(request):
    """Home page with the media items currently in progress.

    首页只留「进行中」一个分区。原来还渲染「已暂停」（Paused，958 个游戏）
    和「计划中」（Planning，2086 个游戏）两个分区，这两个货架在本机几乎没人看，
    却要各自跑一遍排序，所以整块去掉了；书的口径见 app/home.py。
    """
    sort_by = request.user.update_preference("home_sort", request.GET.get("sort"))
    media_type_to_load = request.GET.get("load_media_type")
    section_to_load = request.GET.get("load_status", Status.IN_PROGRESS.value)
    hide_unreleased_param = request.GET.get("hide_unreleased")
    hide_unreleased = request.user.update_preference(
        "home_hide_unreleased",
        None if hide_unreleased_param is None else hide_unreleased_param == "true",
    )
    items_limit = 14

    # If this is an HTMX request to load more items for a specific media type
    if request.headers.get("HX-Request") and media_type_to_load:
        list_by_type = home_helpers.get_home_media_types(
            request,
            sort_by,
            section_to_load,
            items_limit,
            media_type_to_load,
            hide_unreleased=hide_unreleased,
        )
        return render(
            request,
            "app/components/home_grid.html",
            {
                "media_list": list_by_type.get(
                    media_type_to_load,
                    {"items": [], "total": 0},
                ),
                "home_status": section_to_load,
            },
        )

    # 首页只保留「进行中」一个分区：已暂停（Paused）和计划中（Planning）
    # 两个分区整块不再构建，连查询都不发。
    enabled_sections = request.user.get_enabled_home_sections()
    section_keys = [
        section_key
        for section_key in (Status.IN_PROGRESS.value,)
        if section_key in enabled_sections
    ]

    home_sections = [
        home_helpers.build_home_section(
            section_key,
            home_helpers.get_home_media_types(
                request,
                sort_by,
                section_key,
                items_limit,
                hide_unreleased=hide_unreleased,
            ),
        )
        for section_key in section_keys
    ]

    context = {
        "home_sections": home_sections,
        "current_sort": sort_by,
        "sort_choices": i18n_zh.tr_choices(HomeSortChoices.choices),
        "hide_unreleased": hide_unreleased,
        "items_limit": items_limit,
    }
    return render(request, "app/home.html", context)


@require_POST
def progress_edit(request, media_type, instance_id):
    """Increase or decrease the progress of a media item from home page."""
    operation = request.POST["operation"]
    hide_unreleased = request.user.home_hide_unreleased
    home_status = request.POST.get("home_status")
    # 详情页的快捷动作（+30 分钟 / 标记完成）复用同一个端点，只是换一份局部模板回来
    detail = request.POST.get("detail") == "1"

    media = helpers.get_owned_media_or_404(
        request, media_type, instance_id, prefetch=True
    )

    if operation == "increase":
        media.increase_progress()
    elif operation == "decrease":
        media.decrease_progress()
    elif operation == "complete":
        media.status = Status.COMPLETED.value
        if not media.end_date:
            media.end_date = timezone.now().replace(second=0, microsecond=0)
        media.save()

    if media_type == MediaTypes.SEASON.value:
        # clear prefetch cache to get the updated episodes
        media.refresh_from_db()
        prefetch_related_objects(
            [media],
            Prefetch(
                "episodes",
                queryset=Episode.objects.select_related("item"),
            ),
            Prefetch(
                "item__event_set",
                queryset=Event.objects.all(),
                to_attr="prefetched_events",
            ),
        )

    # 详情页快捷动作：同一端点、换一份局部模板（首页那套不动）
    if detail:
        media.refresh_from_db()
        return render(
            request,
            "app/components/detail_progress_actions.html",
            {
                "media": media,
                "media_type": media_type,
                "Status": Status,
            },
        )

    if hide_unreleased and home_status == Status.IN_PROGRESS.value:
        if media_type == MediaTypes.SEASON.value:
            BasicMedia.objects.annotate_max_progress([media], media_type)
        BasicMedia.objects._annotate_next_event([media])

        if not home_helpers.is_active_in_progress_media(media):
            response = HttpResponse()
            response["HX-Retarget"] = f"#home-media-{media.item.media_type}-{media.id}"
            response["HX-Reswap"] = "delete"
            return response

    context = {
        "media": media,
        "home_status": home_status,
    }
    return render(
        request,
        "app/components/progress_changer.html",
        context,
    )


# ---------------------------------------------------------------------------
# 分类（genres）筛选
#
# ?genres=动作,角色扮演，逗号分隔、多选取并集。genres 是 Item 上的 JSONField
# （models.py:102），SQLite 后端不支持 Django 的 contains 查找（会抛
# NotSupportedError），所以这里统一走 JSON1 的 json_each 写成 SQL 子查询，
# 分类名全部用 %s 占位参数化，绝不拼进字符串。
# ---------------------------------------------------------------------------

# 中文输入法很容易打出全角逗号，两种都当分隔符
_GENRE_SEPARATORS = {"，": ","}

# json_each 只对合法 JSON 数组展开；这两个守卫条件避免脏数据直接炸整页
_JSON_GENRES_GUARD = "json_valid(i.genres) AND json_type(i.genres) = 'array'"


def _parse_genres_param(raw):
    """把 ?genres=动作,角色扮演 解析成去重后的分类名列表。

    半角/全角逗号都当分隔符，去首尾空格、丢掉空串、保持用户输入的顺序；
    参数缺失或全是空串时返回空列表（= 不筛选）。
    """
    if not raw:
        return []
    for full_width, half_width in _GENRE_SEPARATORS.items():
        raw = raw.replace(full_width, half_width)

    parsed = []
    for piece in raw.split(","):
        name = piece.strip()
        if name and name not in parsed:
            parsed.append(name)
    return parsed


def _item_ids_for_genres(genres):
    """返回命中任一分类的 Item id 子查询（未命中任何分类则为 None）。

    genres 是 JSONField，SQLite 不支持 contains 查找，所以用 json_each 展开成
    行再比较；分类名走参数绑定，中文不参与拼接。
    """
    if not genres:
        return None
    placeholders = ", ".join(["%s"] * len(genres))
    return Item.objects.extra(
        where=[
            "json_valid(genres)"
            " AND EXISTS (SELECT 1 FROM json_each(genres) je"
            f" WHERE je.value IN ({placeholders}))",
        ],
        params=list(genres),
    ).values("id")


def _genres_in_media_type(media_type, names):
    """从 names 里筛出该媒体类型确实拥有的分类（库级校验）。

    这里故意不看状态 / 搜索：某个分类在当前档位下可能一条都没有（例如
    「竞速」在某个状态下 0 条），但它本身是真实分类，不能被当成脏参数丢掉，
    否则用户选了分类切个状态，筛选会静默失效。真·不存在的分类才返回不到。
    """
    if not names:
        return set()
    placeholders = ", ".join(["%s"] * len(names))
    sql = (
        "SELECT DISTINCT je.value FROM app_item i JOIN json_each(i.genres) je "
        f"WHERE i.media_type = %s AND {_JSON_GENRES_GUARD} "
        f"AND je.value IN ({placeholders})"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, [media_type, *names])
        return {row[0] for row in cursor.fetchall()}


def _genre_options(queryset, media_type):
    """一条 SQL 算出每个分类在「当前状态 + 搜索」下的条数，按数量倒序。

    分类清单完全从当前 media_type 的实际数据里来（不写死任何名单），
    所以书（book）这类没有分类的媒体类型自然得到空列表。
    count 的语义是「只再选中这一个分类会剩多少条」，不叠加已选分类，
    面板上的「动作 77」就是靠这个数字。
    """
    # 取基础筛选（状态 + 搜索）的 SQL，作为分类聚合的白名单。
    # 分类最多 51 个，但这里只发一条聚合查询，不做 51 次往返。
    sql, params = queryset.order_by().values("item_id").query.sql_with_params()
    aggregate_sql = (
        "SELECT je.value AS name, COUNT(DISTINCT i.id) AS c "
        "FROM app_item i "
        "JOIN json_each(i.genres) je "
        f"WHERE i.media_type = %s AND {_JSON_GENRES_GUARD} "
        f"AND i.id IN ({sql}) "
        "GROUP BY je.value ORDER BY c DESC, name ASC"
    )
    with connection.cursor() as cursor:
        cursor.execute(aggregate_sql, [media_type, *params])
        return [{"name": name, "count": count} for name, count in cursor.fetchall()]


@login_not_required
@require_GET
def media_list(request, username, media_type):
    """Return the media list page."""
    target_user = get_object_or_404(User, username=username)

    # 这个库自己的 session 偏好（每个 media_type 一桶，互不污染）。
    # 只有「看自己的库」才有意义；看别人的库一律不读也不写 session。
    is_own_list = request.user == target_user
    saved = _get_list_pref_bucket(request, media_type) if is_own_list else {}

    # if user is looking at own page then update preferences
    if request.user == target_user:
        layout = target_user.update_preference(
            f"{media_type}_layout",
            request.GET.get("layout"),
        )
        sort_filter = target_user.update_preference(
            f"{media_type}_sort",
            request.GET.get("sort"),
        )
        status_filter = target_user.update_preference(
            f"{media_type}_status",
            request.GET.get("status"),
        )
    else:
        # privacy check then media type check
        if target_user.profile_private:
            msg = "User not found"
            raise Http404(msg)

        enabled_media_types = target_user.get_enabled_media_types()
        if not enabled_media_types:
            msg = "User doesn't have any media types enabled"
            raise Http404(msg)

        if media_type not in enabled_media_types:
            return redirect(
                "medialist",
                username=target_user.username,
                media_type=enabled_media_types[0],
            )

        layout = target_user.get_valid_preference(
            f"{media_type}_layout",
            request.GET.get("layout"),
        )
        sort_filter = target_user.get_valid_preference(
            f"{media_type}_sort",
            request.GET.get("sort"),
        )
        status_filter = target_user.get_valid_preference(
            f"{media_type}_status",
            request.GET.get("status"),
        )

    search_query = request.GET.get("search", "")
    page = request.GET.get("page", 1)

    # URL 里明确带了版式/排序 → 存进本库的 session 桶，之后裸进本库就沿用。
    # （状态和密度的写入在下面各自的分支里，因为它们还要校验取值/巨幕。）
    if is_own_list:
        for _name in ("layout", "sort"):
            _clean = _clean_pref_value(_name, request.GET.get(_name), media_type)
            if _clean is not None:
                _set_list_pref(request, media_type, _name, _clean)

    # 读：URL 没带版式/排序时，先看本库 session，再退回库里的偏好。
    # 版式和排序在四个库里都有独立的数据库列，所以以前「分库」本来就是对的；
    # 这里加 session 只是为了和 density / lead 保持同一套优先级。
    if is_own_list:
        if _clean_pref_value("layout", request.GET.get("layout"), media_type) is None:
            _saved_layout = (saved or {}).get("layout")
            if _saved_layout in _VALID_LAYOUTS:
                layout = _saved_layout
        if _clean_pref_value("sort", request.GET.get("sort"), media_type) is None:
            _saved_sort = (saved or {}).get("sort")
            if _saved_sort in _VALID_SORTS:
                sort_filter = _saved_sort

    # 巨屏搜索（density=1 + search=关键词）：URL 里没带 status 时**不沿用**上回存的
    # 状态偏好。用户方案是「搜到一个就在屏幕上铺开那一个的巨幕」，而存下来的偏好
    # 往往是「已暂停 / 全成就」，实测搜 Hades 命中 4 条、筛完剩 0 条 → 屏幕一片空白，
    # 看起来就像搜索坏了。所以这一种请求把状态强行拉到「全部」，其余请求照旧。
    hero_search_mode = (
        media_type == MediaTypes.GAME.value
        and request.GET.get("density") == "1"
        and bool(request.GET.get("search"))
    )
    if hero_search_mode and not request.GET.get("status"):
        status_filter = MediaStatusChoices.ALL

    # 下拉四档里「全成就」是界面造出来的伪状态，库里没有这个取值，
    # 存进 *_status 字段会违反 check constraint，所以只对本次请求生效。
    requested_status = request.GET.get("status")
    if requested_status in status_filters.FILTER_VALUES:
        status_filter = requested_status
        # 伪值 "All achievements" 照存进 session（那边不受 DB 约束），
        # 这样「上次选了全成就」下次裸进来还能回来。
        if is_own_list:
            _set_list_pref(request, media_type, "status", requested_status)
    # 游戏列表的默认档位是「全成就」：URL 里没带 status（从侧栏、详情页返回、
    # 统计页跳过来）就先落到这一档。
    # 现在多一层：先看本库 session 里记的档位，其次看库里存的 game_status，
    # 都没有才落到「全成就」。这就是「状态记忆 100% 失效」的修复点 ——
    # 以前这一支无条件写死 ALL_ACHIEVEMENTS，库里的值等于白存。
    elif (
        not requested_status
        and media_type == MediaTypes.GAME.value
        and not hero_search_mode
    ):
        status_filter = _saved_game_status(saved, target_user)

    # Prepare status filter for database query
    if not status_filter:
        status_filter = MediaStatusChoices.ALL
    # 老偏好里可能还存着 Planning（当年是单独一档），现在并进「已暂停」
    status_filter = status_filters.normalize(status_filter)

    # Fallback for legacy "score" default: when nothing in this library has a
    # score, sorting by score is effectively random. Use playtime progress
    # instead (a real, always-present field) without touching the model default
    # (which would require a migration).
    if sort_filter == MediaSortChoices.SCORE:
        media_model = apps.get_model(app_label="app", model_name=media_type)
        has_any_score = media_model.objects.filter(
            user=target_user,
        ).exclude(score__isnull=True).exists()
        if not has_any_score:
            sort_filter = MediaSortChoices.PROGRESS

    # Get media list with filters applied
    media_queryset = BasicMedia.objects.get_media_list(
        user=target_user,
        media_type=media_type,
        status_filter=status_filter,
        sort_filter=sort_filter,
        search=search_query,
    ).select_related("item", "item__ai_copy")
    # AI 金句是 Item 上的反向 OneToOne（related_name="ai_copy"）。用 select_related
    # 一次 JOIN 带回来，避免每张卡片一次查询（N+1）。没有 AI 文案的条目在模板里
    # 用 {% if item.ai_copy %} 判断，缺对象时是假值，不会抛异常。

    # 分类面板的候选清单：只取「状态 + 搜索」这一层，不含本次已选分类，
    # 这样每个分类的 count 都是「单独再选中它会剩多少条」。
    genre_options = _genre_options(media_queryset, media_type)
    # 传了库里不存在的分类名就忽略；全被忽略后 genres_selected 为空，
    # 等价于不筛选（不会退化成空结果，也不会生成空的 IN ()）。
    requested_genres = _parse_genres_param(request.GET.get("genres"))
    known_genre_names = _genres_in_media_type(media_type, requested_genres)
    genres_selected = [
        name for name in requested_genres if name in known_genre_names
    ]

    # 分类筛选：多选取并集，命中任一即算。位置必须在 Paginator 之前，
    # 否则 num_pages 还是未过滤的总数，翻页会翻出空白页。
    genre_item_ids = _item_ids_for_genres(genres_selected)
    if genre_item_ids is not None:
        media_queryset = media_queryset.filter(item_id__in=genre_item_ids)

    # 每页条数固定 16 条（4×4），密度档位（4/8/12/16）改由前端 CSS 控制：
    # 后端一次渲染满 16 张，浏览器里点密度只改 data-show，隐藏多余卡片 + 换列数，
    # 不跳 URL、不重新请求服务器。所以 density 参数只用来决定「首帧的档位」，
    # 不再决定抓多少条（书籍/影/剧/游戏的翻页页码因此也不再随密度变化）。
    #
    # 巨屏（density=1，18 条）是唯一还走服务端的档位，而且只给游戏库：
    # 书/影/剧没有 3:1 横版大图，铺满 100svh 很难看。
    # density=1 落在别的库上必须干净地退回 12 档，不能沿用上一轮的数字。
    # 密度三级（用户要「选的 4/8/12/16 记住」，而且**每个库各记各的**）：
    #   ① URL 带了合法档位（4/8/12/16，或游戏库的巨幕 1）→ 用它，并记进本库 session + 写库
    #   ② URL 没带 → 先用本库 session 里记的，其次用用户身上的 list_density（空串当没存）
    #   ③ 都没有 → 兜底 12
    # ⚠ 巨幕必须在这里就把 hero_mode / items_per_page 抬上来：下面 Paginator
    #   要靠 items_per_page 抓 18 条，模板要靠 hero_mode 铺满屏。只在 density_show
    #   里写个 "1" 是不够的（那就是用户说的「选了巨幕，点游戏没反应」）。
    # 下面这几条原有特殊规则逐条保留：
    #   · auto → 16     · 巨幕只在游戏库成立
    #   · density=1 落在非游戏库干净退回 12     · 合法只有 {4,8,12,16}，脏值一律 12
    #   · 巨屏 18 条 / 普通 16 条     · 写库只写 4/8/12/16，永不写 "1"（DB 有约束）
    density_param_given = request.GET.get("density") is not None
    density_raw = request.GET.get("density", "")
    if density_raw == "auto":  # 老链接里的 auto 就是 16 档
        density_raw = "16"
    hero_mode = media_type == MediaTypes.GAME.value and density_raw == DENSITY_HERO
    if hero_mode:
        density_show = DENSITY_HERO
        items_per_page = 18
        # 巨幕只进 session（DB 那一列存不了 "1"，有 check constraint）
        if is_own_list:
            _set_list_pref(request, media_type, "density", DENSITY_HERO)
    else:
        # 只认四档；"1"（巨屏档）和任何脏值一律落到 12。
        # 这样「游戏库点巨屏 → 点电影库」时，URL 里残留的 density=1
        # 不会让电影库变成 4 张卡或半截巨屏。
        if density_raw in VALID_DENSITIES:
            # ① URL 给了合法档位：以它为准，顺手记住（只记 4/8/12/16，永不记 "1"）
            density_show = density_raw
            if is_own_list:
                _set_list_pref(request, media_type, "density", density_raw)
            if request.user == target_user:
                target_user.update_preference("list_density", density_raw)
        elif density_param_given:
            # URL 明确给了个不能用的值（"1" 落在非游戏库、脏值）：干净退回 12，
            # 不读记忆 —— 这是原有规则，一个字不能变。
            density_show = "12"
        else:
            # ② URL 根本没带 density：先看本库 session，再看用户身上那一列。
            # session 里可能是巨幕 "1"（只可能是游戏库存的，写入时已按库校验过）。
            cand = (saved or {}).get("density") or ""
            if cand == DENSITY_HERO and media_type == MediaTypes.GAME.value:
                hero_mode = True
                density_show = DENSITY_HERO
                items_per_page = 18
            else:
                if cand not in VALID_DENSITIES:
                    cand = getattr(target_user, "list_density", "") or ""
                # ③ 都没有 → 兜底 12
                density_show = cand if cand in VALID_DENSITIES else "12"
                items_per_page = 16
        if not hero_mode:
            items_per_page = 16
    density_param = density_show
    paginator = Paginator(media_queryset, items_per_page)
    media_page = paginator.get_page(page)

    BasicMedia.objects.annotate_max_progress(
        media_page.object_list,
        media_type,
    )

    # Hide columns that would render as "all dashes" on this page.
    # Pure in-memory checks over the already-fetched page (max 32 rows),
    # no extra queries and no N+1.
    page_items = list(media_page.object_list)
    show_score_column = any(item.score is not None for item in page_items)
    show_start_date_column = any(item.start_date for item in page_items)
    show_end_date_column = any(item.end_date for item in page_items)

    # ---------------------------------------------------------------- 首卡特写
    # lead 是纯前端概念（L 键让第一张卡横向铺开），后端只负责「按库记住这个开关」。
    # 优先级：URL 的 lead=1/0（刚点过那一下）→ 本库 session → 默认关。
    # 模板判断的是 {% if lead_mode == True %}，所以必须传**真正的布尔**，
    # 传字符串 "1" 两个分支都不命中，会掉进 localStorage 兜底（分库就废了）。
    lead_param = _clean_pref_value(PREF_LEAD, request.GET.get(PREF_LEAD), media_type)
    if lead_param is not None:
        if is_own_list:
            _set_list_pref(request, media_type, PREF_LEAD, lead_param)
        lead_mode = lead_param == "1"
    else:
        lead_mode = (saved or {}).get(PREF_LEAD) == "1"

    context = {
        "media_type": media_type,
        "media_type_plural": app_tags.media_type_readable_plural(media_type).lower(),
        "media_list": media_page,
        "current_layout": layout,
        "layout_class": ".media-grid" if layout == "grid" else "tbody",
        "current_sort": sort_filter,
        "current_status": status_filter,
        # 卡片密度档位（4 / 8 / 12 / 16，巨屏另有 1）。
        #   density      = 当前档位，模板写在 data-show 上，CSS 据此隐藏多余卡片 + 换列数
        #   density_data = 这一页实际渲染了几张（固定 16，巨屏 18），写在 data-d 上
        #                  —— CSS 只拿它当「满档版式」的标识，不再代表列数
        "density": density_param,
        "density_data": items_per_page,
        "hero_mode": hero_mode,
        # 首卡特写开关。必须是 Python bool：模板里写的是
        #   {% if lead_mode == True %}true{% elif lead_mode == False %}false{% ... %}
        # 传字符串/数字都会掉进 else 分支。
        "lead_mode": bool(lead_mode),
        "has_prev_page": media_page.has_previous(),
        "has_next_page": media_page.has_next(),
        "prev_page_number": media_page.previous_page_number()
        if media_page.has_previous()
        else None,
        "next_page_number": media_page.next_page_number()
        if media_page.has_next()
        else None,
        "page_number": media_page.number,
        "total_pages": paginator.num_pages,
        "sort_choices": i18n_zh.tr_choices(MediaSortChoices.choices),
        # 下拉只给四档：全成就 / 已通关 / 进行中 / 已暂停。
        # 老值（All / Planning / Dropped）仍要能显示标签，所以另有 status_label_choices。
        # 书和影视没有成就系统，这两类按媒体类型换词（已读完 / 已观看 等），
        # 取值和筛选逻辑完全不变，游戏仍是原来那四档。
        "status_choices": i18n_zh.tr_choices(status_filters.filter_choices_for(media_type)),
        "status_label_choices": i18n_zh.tr_choices(status_filters.label_choices_for(media_type)),
        "target_user": target_user,
        "show_score_column": show_score_column,
        "show_start_date_column": show_start_date_column,
        "show_end_date_column": show_end_date_column,
        # 分类筛选面板（第 2 波前端用）。genres_total 取分页后的总数，
        # 和 total_pages 同源，保证「筛了之后有 N 条」跟翻页对得上。
        "genres_selected": genres_selected,
        "genre_options": genre_options,
        "genres_total": paginator.count,
    }

    # Handle HTMX requests for partial updates. Soft-navigation requests (e.g.
    # after saving from an edit modal) need the full page for the body swap.
    hx_request = bool(request.headers.get("HX-Request"))
    soft_navigation = bool(request.headers.get("X-Soft-Navigation"))
    if hx_request and not soft_navigation:
        # Filtering from empty list
        if request.headers.get("HX-Target") == "empty_list":
            # If still empty, keep user in the same page
            if not media_page.object_list:
                return HttpResponse(status=204)
            response = HttpResponse()
            response["HX-Redirect"] = reverse(
                "medialist", args=[target_user.username, media_type]
            )
            return response
        if layout == "grid":
            template_name = "app/components/media_grid_items.html"
        else:
            template_name = "app/components/media_table_items.html"
        # 局部请求（筛选、翻页、排序按钮）一律原样返回片段，不做 302、
        # 不推 URL —— 这块行为一个字没改。
        return render(request, template_name, context)

    template_name = "app/media_list.html"

    # 走到这里时本轮该记的都已经写进 session 了，重新读一次，
    # 这样拼出来的地址和刚才真正渲染出来的页面是同一套值
    # （用请求开头那份快照会出现「页面已经是 table、地址栏还写 grid」）。
    saved_now = _get_list_pref_bucket(request, media_type) if is_own_list else {}

    # --------------------------------------------------------- 裸地址补全（302）
    # 用户从侧栏 / 首页点进来时 URL 是光秃秃的 /admin/game，不带任何参数，
    # 浏览器地址栏里就看不出「上次选的是巨幕 / 全成就 / 结束日期」。
    # 这里补一次 302，把参数拼回去，地址栏和页面内容就对得上了。
    #
    # 只在**这个库真的记过**的时候跳（session 里有桶），首次访问不跳，
    # 否则新用户每点一次侧栏都要吃一发多余跳转。
    # 跳过去的地址必定带齐参数，所以第二次一定命中「URL 已带某项」→ 不再跳（防循环）。
    skip_redirect = (
        not is_own_list  # 看别人的库不跳
        or soft_navigation  # 软导航不能 302（hx-select 会选不到东西，正文空白）
        or hero_search_mode  # 巨屏搜索
        or bool(request.GET.get("search"))
        or bool(request.GET.get("page"))
        or bool(request.GET.get("genres"))
        or any(name in request.GET for name in _list_param_names())
        or not saved  # 进这个视图之前就没记过 → 不跳
    )
    if not skip_redirect:
        full_params = _full_list_params(saved_now)
        if full_params:
            url = reverse("medialist", args=[target_user.username, media_type])
            return redirect(f"{url}?{urlencode(full_params)}")

    response = render(request, template_name, context)
    # 软导航同样要把参数推回地址栏：整页照常返回（hx-boost 要能选到
    # #main-content），但用 HX-Push-Url 让地址栏和页面内容保持一致。
    # 分页 / 搜索 / 分类不属于「要记住的 5 项」，但它们在这次请求里生效了，
    # 所以原样带上，不能让推回地址栏的链接丢掉用户当前看的页。
    if soft_navigation and saved_now:
        push_params = _full_list_params(saved_now)
        if push_params:
            push_params += [
                (name, request.GET[name])
                for name in ("search", "page", "genres")
                if request.GET.get(name)
            ]
            url = reverse("medialist", args=[target_user.username, media_type])
            response["HX-Push-Url"] = f"{url}?{urlencode(push_params)}"
    return response


def _search_local_items(query, media_type, source, limit):
    """Return local items whose title or stored alias matches ``query``.

    The Steam store search only knows the store title, so a game stored under a
    different Chinese (title_zh) or English (title_en) name never comes back.
    Both aliases live on the Item, so they are matched here and the hit is
    labelled with the name the user actually typed.
    """
    matches = (
        Item.objects.filter(media_type=media_type, source=source)
        .filter(
            Q(title__icontains=query)
            | Q(title_zh__icontains=query)
            | Q(title_en__icontains=query),
        )
        .order_by("title")[:limit]
    )

    needle = query.casefold()
    results = []
    for item in matches:
        title = item.title
        for alias in (item.title_zh, item.title_en):
            if alias and needle in alias.casefold() and alias != title:
                title = f"{title}（{alias}）"
                break
        results.append(
            {
                "media_id": item.media_id,
                "source": item.source,
                "media_type": item.media_type,
                "title": title,
                "image": item.image,
                "release_year": item.release_date.year if item.release_date else None,
            },
        )
    return results


@require_GET
def media_search(request):
    """Return the media search page."""
    media_type = request.user.update_preference(
        "last_search_type",
        request.GET.get("media_type") or MediaTypes.GAME.value,
    )
    query = request.GET.get("q", "").strip()
    if not query:
        messages.info(request, "请输入要搜索的名称。")
    # A hand-edited or stale page param must not crash the page: anything that
    # is not a sane page number falls back to the first page instead of
    # reaching a provider as an absurd offset.
    try:
        page = int(request.GET.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    if page < 1 or page > MAX_SEARCH_PAGE:
        page = 1
    layout = request.GET.get("layout", "grid")

    # only receives source when searching with secondary source
    source = request.GET.get(
        "source",
        config.get_default_source_name(media_type).value,
    )

    # 搜索必须「要么很快出结果，要么很快说清为什么没结果」。
    # store.steampowered.com 从本机连不通（ConnectTimeout），以前一个搜索请求会把
    # 连接超时（120 秒）等满，浏览器那边 45～60 秒就断了，用户看到的就是一直转圈，
    # 运气不好还吃个 500。现在把两种情况分开：
    #   · 服务连不上 → 渲染降级页（200 + 中文提示），几秒内返回
    #   · 真没搜到   → 还是原来的空结果页
    # ⚠ 只接 provider 的异常。KeyError / AttributeError 这类真 bug 必须照常冒出去。
    degraded = None
    try:
        data = services.search(media_type, query, page, source)
    except services.ProviderUnavailableError as exc:
        degraded = {
            "kind": "unavailable",
            "message": f"{exc.provider_label} 现在连不上（网络超时），不是关键词搜错了。",
            "hint": "稍等一两分钟再搜一次。想立刻用的话，可以换个数据源试试。",
        }
        data = helpers.format_search_response(page, settings.PER_PAGE, 0, [])
    except services.ProviderAPIError as exc:
        code = f"HTTP {exc.status_code}" if exc.status_code else "网络异常"
        degraded = {
            "kind": "error",
            "message": f"{exc.provider_label} 返回了错误（{code}），这次搜索没做完。",
            "hint": "换一个数据源，或过一会儿再试。",
        }
        data = helpers.format_search_response(page, settings.PER_PAGE, 0, [])

    # The provider only knows the store titles, so a game stored under a
    # different Chinese (title_zh) or English (title_en) name is invisible.
    # Both aliases live on the local Item, so their matches are merged in ahead
    # of the provider results; anything the provider already returned is kept
    # once, and only the first page is topped up.
    if query and page == 1 and media_type == MediaTypes.GAME.value:
        known = {(result["source"], str(result["media_id"])) for result in data["results"]}
        local = [
            result
            for result in _search_local_items(
                query, media_type, source, settings.PER_PAGE
            )
            if (result["source"], str(result["media_id"])) not in known
        ]
        if local:
            data["results"] = (local + data["results"])[: settings.PER_PAGE]
            data["total_results"] += len(local)
            data["total_pages"] = data["total_results"] // settings.PER_PAGE + 1
            # 线上挂了但本地库里有这款游戏：那几条照样显示，只是页头多一句说明。
            # 这样断网时「搜我自己的游戏」依然能用，也不会让人以为库里数据丢了。
            if degraded is not None:
                degraded["local_count"] = len(local)

    # Enrich search results with user tracking data
    if data.get("results"):
        data["results"] = helpers.enrich_items_with_user_data(
            request, data["results"], "search"
        )

    context = {
        "data": data,
        "source": source,
        "media_type": media_type,
        "layout": layout,
        "degraded": degraded,
    }

    return render(request, "app/search.html", context)


# How many rows the detail page shows for the reading lists (highlights and
# notes). The whole library only holds a couple hundred, but the page never
# needs the full history, so it is capped instead of growing with the data.
DETAIL_LIST_LIMIT = 30


def _book_reading_context(current_instance):
    """Return ``(highlights, notes)`` for a tracked book.

    Both come from WeRead imports, so anything that is not a tracked book gets
    two empty lists and the template simply renders nothing.
    """
    if current_instance is None:
        return [], []

    try:
        highlights = list(
            Highlight.objects.filter(item=current_instance.item).order_by(
                "-created_at",
            )[:DETAIL_LIST_LIMIT],
        )
        notes = list(
            Note.objects.filter(item=current_instance.item).order_by(
                "-created_at",
            )[:DETAIL_LIST_LIMIT],
        )
    except Exception:  # a bad read must never 500 the page
        logger.warning(
            "reading extras failed for %s",
            getattr(current_instance, "pk", None),
            exc_info=True,
        )
        return [], []

    return highlights, notes


def _tv_progress_context(current_instance):
    """Return ``(seasons, episodes)`` for a tracked show.

    The show itself has no direct link to episodes: they hang off each season,
    so the seasons are walked once and their episodes read in a single query.
    Untracked shows, and any error, get two empty lists.
    """
    if current_instance is None:
        return [], []

    try:
        seasons = list(
            Season.objects.filter(related_tv=current_instance)
            .select_related("item")
            .order_by("item__season_number"),
        )
        if not seasons:
            return [], []

        episodes = list(
            Episode.objects.filter(related_season__in=seasons)
            .select_related("item", "related_season__item")
            .order_by("related_season__item__season_number", "item__episode_number"),
        )
    except Exception:  # a bad read must never 500 the page
        logger.warning(
            "tv progress failed for %s",
            getattr(current_instance, "pk", None),
            exc_info=True,
        )
        return [], []

    watched_by_season = {}
    episode_rows = []
    for episode in episodes:
        watched = episode.end_date is not None
        season_id = episode.related_season_id
        counts = watched_by_season.setdefault(season_id, [0, 0])
        counts[0] += 1
        if watched:
            counts[1] += 1
        episode_rows.append(
            {
                "number": episode.item.episode_number,
                "title": episode.item.title,
                "season_number": episode.related_season.item.season_number,
                "end_date": episode.end_date,
                "notes": episode.notes,
                "watched": watched,
            },
        )

    season_rows = []
    for season in seasons:
        total, watched_total = watched_by_season.get(season.id, [0, 0])
        season_rows.append(
            {
                "number": season.item.season_number,
                "episode_count": total,
                "watched_count": watched_total,
            },
        )

    return season_rows, episode_rows


def _detail_extras(media_type, current_instance, media_metadata):
    """Return the per-type extras the redesigned detail pages read.

    Each key is only filled for the media type that can actually have it, so a
    template can test the list itself instead of repeating the media_type
    condition. Unknown types simply get empty values.
    """
    extras = {
        "highlights": [],
        "book_notes": [],
        "seasons": [],
        "episodes": [],
        "runtime_minutes": None,
    }

    if media_type == MediaTypes.BOOK.value:
        extras["highlights"], extras["book_notes"] = _book_reading_context(
            current_instance,
        )
    elif media_type == MediaTypes.TV.value:
        extras["seasons"], extras["episodes"] = _tv_progress_context(
            current_instance,
        )
        # One episode in minutes, which is the only runtime a show knows.
        extras["runtime_minutes"] = media_metadata.get("episode_runtime")
    elif media_type == MediaTypes.MOVIE.value:
        # The tracked movie row carries the minutes; the provider copy is the
        # fallback for a film the user has not started yet.
        if current_instance is not None:
            extras["runtime_minutes"] = current_instance.runtime
        if extras["runtime_minutes"] is None:
            extras["runtime_minutes"] = media_metadata.get("runtime_minutes")

    return extras


@require_GET
def media_details(request, source, media_type, media_id, title):  # noqa: ARG001 title for URL
    """Return the details page for a media item."""
    try:
        media_metadata = services.get_media_metadata(media_type, media_id, source)
    except Exception:
        # A dead provider must not take the whole page down with it: the page
        # is still worth rendering with just the id in the title.
        logger.warning(
            "detail metadata fallback for %s/%s/%s",
            source,
            media_type,
            media_id,
            exc_info=True,
        )
        media_metadata = {
            "media_id": media_id,
            "media_type": media_type,
            "source": source,
            "title": str(media_id),
        }
    user_medias = BasicMedia.objects.filter_media_prefetch(
        request.user,
        media_id,
        media_type,
        source,
    )
    current_instance = user_medias[0] if user_medias else None

    # Steam achievements are stored per user on the game itself, the metadata
    # of the item can only guess which tracked game to read them from
    total_achievements = getattr(current_instance, "achievements_total", 0)
    if total_achievements:
        media_metadata["achievements"] = {
            "total": total_achievements,
            "unlocked": current_instance.achievements_unlocked,
            "percent": current_instance.achievement_percent,
        }

    if current_instance is not None:
        helpers.refresh_item_image_if_missing(
            current_instance.item, media_metadata.get("image")
        )

    # Enrich related items with user tracking data
    if media_metadata.get("related"):
        for section_name, related_items in media_metadata["related"].items():
            if related_items:
                media_metadata["related"][section_name] = (
                    helpers.enrich_items_with_user_data(
                        request, related_items, section_name
                    )
                )

    if media_type in ["tv", "movie"]:
        watch_providers = tmdb.filter_providers(
            media_metadata.get("providers"), request.user.watch_provider_region
        )
    else:
        watch_providers = None

    # Steam captures live only in the client cache, so they are read per
    # request instead of being imported. Anything but a Steam game gets an
    # empty list, which the template treats as "no screenshot section".
    screenshots = []
    if current_instance is not None:
        appid = steam_screenshots.appid_for_item(current_instance.item)
        if appid:
            screenshots = steam_screenshots.list_screenshots(appid)

    # Extras the redesigned detail pages read: reading lists for a book, season
    # and episode progress for a show, runtime in minutes for both.
    extras = _detail_extras(media_type, current_instance, media_metadata)

    context = {
        "media": media_metadata,
        "media_type": media_type,
        "user_medias": user_medias,
        "current_instance": current_instance,
        "watch_providers": watch_providers,
        "watch_provider_region": request.user.watch_provider_region,
        "screenshots": screenshots,
        "screenshot_count": len(screenshots),
        **extras,
    }
    return render(request, "app/media_details.html", context)


@require_GET
def season_details(request, source, media_id, title, season_number):  # noqa: ARG001 For URL
    """Return the details page for a season."""
    tv_with_seasons_metadata = services.get_media_metadata(
        "tv_with_seasons",
        media_id,
        source,
        [season_number],
    )
    season_metadata = tv_with_seasons_metadata[f"season/{season_number}"]

    user_medias = BasicMedia.objects.filter_media_prefetch(
        request.user,
        media_id,
        MediaTypes.SEASON.value,
        source,
        season_number=season_number,
    )

    current_instance = user_medias[0] if user_medias else None
    episodes_in_db = current_instance.episodes.all() if current_instance else []

    if current_instance is not None:
        helpers.refresh_item_image_if_missing(
            current_instance.item, season_metadata.get("image")
        )

    if source == Sources.MANUAL.value:
        season_metadata["episodes"] = manual.process_episodes(
            season_metadata,
            episodes_in_db,
        )
    else:
        season_metadata["episodes"] = tmdb.process_episodes(
            season_metadata,
            episodes_in_db,
        )

    # Enrich related items with user tracking data
    if season_metadata.get("related"):
        for section_name, related_items in season_metadata["related"].items():
            if related_items:
                season_metadata["related"][section_name] = (
                    helpers.enrich_items_with_user_data(
                        request,
                        related_items,
                        section_name,
                    )
                )

    context = {
        "media": season_metadata,
        "tv": tv_with_seasons_metadata,
        "media_type": MediaTypes.SEASON.value,
        "user_medias": user_medias,
        "current_instance": current_instance,
        "watch_providers": tmdb.filter_providers(
            season_metadata.get("providers"), request.user.watch_provider_region
        ),
        "watch_provider_region": request.user.watch_provider_region,
    }
    return render(request, "app/media_details.html", context)


@require_POST
def update_media_score(request, media_type, instance_id):
    """Update the user's score for a media item."""
    media = helpers.get_owned_media_or_404(request, media_type, instance_id)

    try:
        score = float(request.POST.get("score"))
    except (TypeError, ValueError):
        return JsonResponse(
            {"success": False, "error": "invalid score"},
            status=400,
        )
    if not 0 <= score <= 10:
        return JsonResponse(
            {"success": False, "error": "score out of range"},
            status=400,
        )
    media.score = score
    media.save()
    logger.info(
        "%s score updated to %s",
        media,
        score,
    )

    return JsonResponse(
        {
            "success": True,
            "score": score,
        },
    )


@require_POST
def sync_metadata(request, source, media_type, media_id, season_number=None):
    """Refresh the metadata for a media item."""
    if source == Sources.MANUAL.value:
        msg = "Manual items cannot be synced."
        messages.error(request, msg)
        return HttpResponse(
            msg,
            status=400,
            headers={"HX-Redirect": request.POST.get("next", "/")},
        )

    cache_key = f"{source}_{media_type}_{media_id}"
    if media_type == MediaTypes.SEASON.value:
        cache_key += f"_{season_number}"

    ttl = cache.ttl(cache_key)
    logger.debug("%s - Cache TTL for: %s", cache_key, ttl)

    if ttl is not None and ttl > (settings.CACHE_TIMEOUT - 3):
        msg = "The data was recently synced, please wait a few seconds."
        messages.error(request, msg)
        logger.error(msg)
    else:
        deleted = cache.delete(cache_key)
        logger.debug("%s - Old cache deleted: %s", cache_key, deleted)

        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
        )
        item, _ = Item.objects.update_or_create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            defaults={
                "title": metadata["title"],
                "image": metadata["image"],
            },
        )
        title = metadata["title"]
        if season_number:
            title += f" - Season {season_number}"

        if media_type == MediaTypes.SEASON.value:
            metadata["episodes"] = tmdb.process_episodes(
                metadata,
                [],
            )

            # Create a dictionary of existing episodes keyed by episode number
            existing_episodes = {
                ep.episode_number: ep
                for ep in Item.objects.filter(
                    source=source,
                    media_type=MediaTypes.EPISODE.value,
                    media_id=media_id,
                    season_number=season_number,
                )
            }

            episodes_to_update = []
            episode_count = 0

            for episode_data in metadata["episodes"]:
                episode_number = episode_data["episode_number"]
                if episode_number in existing_episodes:
                    episode_item = existing_episodes[episode_number]
                    episode_item.title = metadata["title"]
                    episode_item.image = episode_data["image"]
                    episodes_to_update.append(episode_item)
                    episode_count += 1

            logger.info(
                "Found %s existing episodes to update for %s",
                episode_count,
                title,
            )

            if episodes_to_update:
                updated_count = Item.objects.bulk_update(
                    episodes_to_update,
                    ["title", "image"],
                    batch_size=100,
                )
                logger.info(
                    "Successfully updated %s episodes for %s",
                    updated_count,
                    title,
                )

        item.fetch_releases(delay=False)

        msg = f"{title} was synced to {Sources(source).label} successfully."
        messages.success(request, msg)

    if request.headers.get("HX-Request"):
        return HttpResponse(
            status=204,
            headers={
                "HX-Redirect": request.POST["next"],
            },
        )
    return helpers.redirect_back(request)


@require_GET
def track_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
):
    """Return the tracking form for a media item."""
    instance_id = request.GET.get("instance_id")
    if instance_id:
        media = BasicMedia.objects.get_media(
            request.user,
            media_type,
            instance_id,
        )
    elif request.GET.get("is_create"):
        media = None
    else:
        # no specific instance, try to find the first one
        user_medias = BasicMedia.objects.filter_media(
            request.user,
            media_id,
            media_type,
            source,
            season_number=season_number,
        )
        media = user_medias.first()
        if media:
            instance_id = media.id

    initial_data = {
        "media_id": media_id,
        "source": source,
        "media_type": media_type,
        "season_number": season_number,
        "instance_id": instance_id,
    }

    if media:
        title = media.item
        if media_type == MediaTypes.GAME.value:
            initial_data["progress"] = helpers.minutes_to_hhmm(media.progress)
    else:
        title = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
        )["title"]
        if media_type == MediaTypes.SEASON.value:
            title += f" S{season_number}"

    form = i18n_zh.tr_form_labels(
        get_form_class(media_type)(instance=media, initial=initial_data),
    )

    return render(
        request,
        "app/components/fill_track.html",
        {
            "title": title,
            "form": form,
            "media": media,
            "return_url": request.GET["return_url"],
        },
    )


@require_POST
def media_save(request):
    """Save or update media data to the database."""
    media_id = request.POST["media_id"]
    source = request.POST["source"]
    media_type = request.POST["media_type"]
    season_number = request.POST.get("season_number")
    instance_id = request.POST.get("instance_id")

    if instance_id:
        instance = helpers.get_owned_media_or_404(request, media_type, instance_id)
    else:
        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
        )
        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            defaults={
                "title": metadata["title"],
                "image": metadata["image"],
            },
        )
        model = apps.get_model(app_label="app", model_name=media_type)
        instance = model(item=item, user=request.user)

    # Validate the form and save the instance if it's valid
    form_class = get_form_class(media_type)
    form = form_class(request.POST, instance=instance)
    if form.is_valid():
        form.save()
        logger.info("%s saved successfully.", form.instance)
    else:
        logger.error(form.errors.as_json())
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(
                    request,
                    f"{field.replace('_', ' ').title()}: {error}",
                )

    return helpers.redirect_back(request)


@require_POST
def media_delete(request):
    """Delete media data from the database."""
    instance_id = request.POST["instance_id"]
    media_type = request.POST["media_type"]
    media = helpers.get_owned_media_or_404(request, media_type, instance_id)
    media.delete()
    logger.info("%s deleted successfully.", media)

    return helpers.redirect_back(request)


@require_POST
def mark_user_messages_shown(request):
    """Mark all unseen persistent messages for the user as shown."""
    message_ids = [
        int(message_id)
        for message_id in request.POST.getlist("message_ids")
        if message_id.isdigit()
    ]
    if not message_ids:
        return HttpResponse(status=204)

    UserMessage.objects.filter(
        id__in=message_ids,
        user=request.user,
        shown_at__isnull=True,
    ).update(shown_at=timezone.now())
    return HttpResponse(status=204)


@require_POST
def episode_save(request):
    """Handle the creation, deletion, and updating of episodes for a season."""
    media_id = request.POST["media_id"]
    season_number = int(request.POST["season_number"])
    episode_number = int(request.POST["episode_number"])
    source = request.POST["source"]

    form = EpisodeForm(request.POST)
    if not form.is_valid():
        logger.error("Form validation failed: %s", form.errors)
        return HttpResponseBadRequest("Invalid form data")

    try:
        related_season = Season.objects.get(
            item__media_id=media_id,
            item__source=source,
            item__season_number=season_number,
            item__episode_number=None,
            user=request.user,
        )
    except Season.DoesNotExist:
        tv_with_seasons_metadata = services.get_media_metadata(
            "tv_with_seasons",
            media_id,
            source,
            [season_number],
        )
        season_metadata = tv_with_seasons_metadata[f"season/{season_number}"]

        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=season_number,
            defaults={
                "title": tv_with_seasons_metadata["title"],
                "image": season_metadata["image"],
            },
        )
        related_season = Season.objects.create(
            item=item,
            user=request.user,
            score=None,
            status=Status.IN_PROGRESS.value,
            notes="",
        )

        logger.info("%s did not exist, it was created successfully.", related_season)

    related_season.watch(
        episode_number,
        form.cleaned_data["end_date"],
        form.cleaned_data["notes"],
    )

    return helpers.redirect_back(request)


@require_http_methods(["GET", "POST"])
def create_entry(request):
    """Return the form for manually adding media items."""
    if request.method == "GET":
        # Honour the user's media type switches: a type turned off in the
        # preferences (comic, boardgame, ...) should not be offered here either.
        enabled_types = request.user.get_enabled_media_types()
        media_types = [
            media_type
            for media_type in MediaTypes.values
            if media_type in enabled_types
            or media_type == MediaTypes.EPISODE.value
        ]
        return render(request, "app/create_entry.html", {"media_types": media_types})

    # Process the form submission
    form = ManualItemForm(request.POST, user=request.user)
    if not form.is_valid():
        # Handle form validation errors
        logger.error(form.errors.as_json())
        helpers.form_error_messages(form, request)
        return redirect("create_entry")

    # Try to save the item
    try:
        item = form.save()
    except IntegrityError:
        # Handle duplicate item
        media_name = form.cleaned_data["title"]
        if form.cleaned_data.get("season_number"):
            media_name += f" - Season {form.cleaned_data['season_number']}"
        if form.cleaned_data.get("episode_number"):
            media_name += f" - Episode {form.cleaned_data['episode_number']}"

        logger.exception("%s already exists in the database.", media_name)
        messages.error(request, f"{media_name} already exists in the database.")
        return redirect("create_entry")

    # Prepare and validate the media form
    updated_request = request.POST.copy()
    updated_request.update({"source": item.source, "media_id": item.media_id})
    media_form = get_form_class(item.media_type)(updated_request)

    if not media_form.is_valid():
        # Handle media form validation errors
        logger.error(media_form.errors.as_json())
        helpers.form_error_messages(media_form, request)

        # Delete the item since the media creation failed
        item.delete()
        logger.info("%s was deleted due to media form validation failure", item)
        return redirect("create_entry")

    # Save the media instance
    media_form.instance.user = request.user
    media_form.instance.item = item

    # Handle relationships based on media type
    if item.media_type == MediaTypes.SEASON.value:
        media_form.instance.related_tv = form.cleaned_data["parent_tv"]
    elif item.media_type == MediaTypes.EPISODE.value:
        media_form.instance.related_season = form.cleaned_data["parent_season"]

    media_form.save()

    # Success message
    msg = f"{item} added successfully."
    messages.success(request, msg)
    logger.info(msg)

    return redirect("create_entry")


@require_GET
def search_parent_tv(request):
    """Return the search results for parent TV shows."""
    query = request.GET.get("q", "").strip()

    if len(query) <= 1:
        return render(request, "app/components/search_parent_tv.html")

    logger.debug(
        "%s - Searching for TV shows with query: %s",
        request.user.username,
        query,
    )

    parent_tvs = TV.objects.filter(
        user=request.user,
        item__source=Sources.MANUAL.value,
        item__media_type=MediaTypes.TV.value,
        item__title__icontains=query,
    )[:5]

    return render(
        request,
        "app/components/search_parent_tv.html",
        {"results": parent_tvs, "query": query},
    )


@require_GET
def search_parent_season(request):
    """Return the search results for parent seasons."""
    query = request.GET.get("q", "").strip()

    if len(query) <= 1:
        return render(request, "app/components/search_parent_tv.html")

    logger.debug(
        "%s - Searching for seasons with query: %s",
        request.user.username,
        query,
    )

    parent_seasons = Season.objects.filter(
        user=request.user,
        item__source=Sources.MANUAL.value,
        item__media_type=MediaTypes.SEASON.value,
        item__title__icontains=query,
    )[:5]

    return render(
        request,
        "app/components/search_parent_season.html",
        {"results": parent_seasons, "query": query},
    )


@require_GET
def history_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
    episode_number=None,
):
    """Return the history page for a media item."""
    user_medias = BasicMedia.objects.filter_media(
        request.user,
        media_id,
        media_type,
        source,
        season_number=season_number,
        episode_number=episode_number,
    )

    total_medias = user_medias.count()
    timeline_entries = []
    for index, media in enumerate(user_medias, start=1):
        if history := media.history.all():
            media_entry_number = total_medias - index + 1
            timeline_entries.extend(
                history_processor.process_history_entries(
                    history,
                    media_type,
                    media_entry_number,
                    request.user,
                ),
            )
    return render(
        request,
        "app/components/fill_history.html",
        {
            "media_type": media_type,
            "timeline": timeline_entries,
            "total_medias": total_medias,
            "return_url": request.GET["return_url"],
        },
    )


@require_http_methods(["DELETE"])
def delete_history_record(request, media_type, history_id):
    """Delete a specific history record."""
    try:
        historical_model = apps.get_model(
            app_label="app",
            model_name=f"historical{media_type.lower()}",
        )

        historical_model.objects.get(
            history_id=history_id,
            history_user=request.user,
        ).delete()

        logger.info(
            "Deleted history record %s",
            str(history_id),
        )

        # Return empty 200 response - the element will be removed by HTMX
        return HttpResponse()

    except historical_model.DoesNotExist:
        logger.exception(
            "History record %s not found for user %s",
            str(history_id),
            str(request.user),
        )
        return HttpResponse("Record not found", status=404)


@require_GET
def statistics(request):
    """Return the statistics page."""
    # 年度报告 / 临门一脚已并入统计页做成子页签（顶栏太长的老问题）。
    # 这里只做分流：URL 是 /statistics?tab=year 就交给原来的视图函数整页渲染。
    # 旧地址 /year-in-review、/near-complete 仍然各自独立可用（老书签不失效）。
    tab = request.GET.get("tab") or "main"
    if tab == "year":
        return year_in_review(request)
    if tab == "near":
        return near_complete(request)
    tab = "main"

    # 时间范围三级（用户要「记住上次选的」，但老行为不许变）：
    #   ① URL 明确带了 start-date / end-date → 以 URL 为准，并写回用户，下次还能用
    #   ② URL 没带 → 读用户身上存着的上次选择
    #   ③ 库里也没存过（从没选过）→ 全部时间，和以前裸访问一模一样
    # 所以裸访问 /statistics + 从没选过的用户，仍然看到全部时间（这条是硬要求）。
    date_range_from_memory = False
    if request.GET.get("start-date") or request.GET.get("end-date"):
        start_date, end_date = stats.parse_activity_date_range(request)
        stats.save_date_range(request.user, start_date, end_date)
    else:
        remembered = stats.load_date_range(request.user)
        if remembered is not None:
            start_date, end_date = remembered
            date_range_from_memory = True
        else:
            start_date, end_date = None, None

    # 给日期选择器用的字符串形式（另一个代理接线用）：
    # 有范围给 "YYYY-MM-DD"，不限给 "all"。
    date_range_start = start_date.strftime("%Y-%m-%d") if start_date else "all"
    date_range_end = end_date.strftime("%Y-%m-%d") if end_date else "all"

    # Get all user media data in a single operation
    user_media, media_count = stats.get_user_media(
        request.user,
        start_date,
        end_date,
    )

    # Calculate all statistics from the retrieved data
    media_type_distribution = stats.get_media_type_distribution(
        media_count,
    )
    score_distribution, top_rated = stats.get_score_distribution(user_media)
    status_distribution = stats.get_status_distribution(user_media)
    status_pie_chart_data = stats.get_status_pie_chart_data(
        status_distribution,
    )
    consumption_stats = stats.get_consumption_stats(user_media, media_count)

    total = media_count["total"]
    in_progress_count = stats.get_status_total(
        status_distribution,
        Status.IN_PROGRESS.value,
    )
    rated_percent = (
        round(score_distribution["total_scored"] / total * 100) if total else None
    )
    # 4 张新图（年度完成趋势 / 时长 Top10 / 成就分布 / 发行年份）
    extra_charts = stats.get_extra_charts(
        request.user,
        user_media,
        start_date,
        end_date,
    )
    # 第 9 张图：月度游玩趋势。数据只能向前积累，本月通常是基线
    # （has_data=False），模板据此显示说明卡片而不是画一排 0。
    monthly_playtime = stats_playtime.get_monthly_playtime(request.user)
    # 说明卡片要写「从哪个月开始记录、哪个月起才有柱子」。
    # 月份文字在视图里算好，模板不碰日期运算（返回结构不动）。
    _this_month = stats_playtime.month_start()
    monthly_this_label = _this_month.strftime("%Y-%m")
    monthly_next_label = stats_playtime.shift_month(_this_month, 1).strftime("%Y-%m")
    completed_percent = (
        round(status_distribution["total_completed"] / total * 100) if total else None
    )

    # 时长卡片（读书 / 观影）：走同一个时间范围，跟统计页的筛选保持一致。
    # 传的是上面解析出来的 aware datetime，duration_stats 内部按 end_date 的日期口径过滤。
    # 注意区间口径的固有行为：传了区间之后，读书的 unknown_date_hours 恒为 0
    # （还在读的书没有 end_date，进不了区间），模板据此自动隐藏那行小字。
    reading_stats = duration_stats.get_reading_stats(
        request.user,
        start_date,
        end_date,
    )
    watch_stats = duration_stats.get_watch_stats(
        request.user,
        start_date,
        end_date,
    )
    # 不筛时间时，读书的 total_hours 含「还在读」的书（unknown_date_hours）。
    # 卡片要同时给出「已读完」的口径，否则 609.0 和 101.5 容易让人以为是两拨数。
    # 已读完 = 只算有 end_date 的书，传一个足够早的下界就能拿到，一次 2 条查询。
    # 筛了范围时 total 本身就是「区间内读完的」，不需要再算。
    reading_stats["finished_hours"] = None
    reading_stats["finished_books"] = None
    if start_date is None and end_date is None:
        _finished = duration_stats.get_reading_stats(
            request.user,
            datetime.date(1970, 1, 1),
            None,
        )
        reading_stats["finished_hours"] = _finished["total_hours"]
        reading_stats["finished_books"] = _finished["book_count"]
    # 读书卡片的逐年小柱状条要按最大值缩放；by_year 是按年份降序排的，
    # 最大小时数不一定在第一行，这里算一次画进数据里，模板只管用。
    _reading_max = max(
        (row["hours"] for row in reading_stats["by_year"]),
        default=0,
    )
    for _row in reading_stats["by_year"]:
        _row["bar_percent"] = (
            round(_row["hours"] / _reading_max * 100) if _reading_max else 0
        )
    # 观影卡片的逐年条：整条宽度按 total_hours 占最大年的比例，
    # 条内再按电影 / 剧集堆叠，两个百分比相加恒为 100（剧集用 100 减出来，避免凑不齐）。
    _watch_max = max(
        (row["total_hours"] for row in watch_stats["by_year"]),
        default=0,
    )
    for _row in watch_stats["by_year"]:
        _row["bar_percent"] = (
            round(_row["total_hours"] / _watch_max * 100) if _watch_max else 0
        )
        _row_total = _row["total_hours"]
        _row["movie_percent"] = (
            round(_row["movie_hours"] / _row_total * 100) if _row_total else 0
        )
        _row["tv_percent"] = (100 - _row["movie_percent"]) if _row_total else 0

    context = {
        "tab": "main",
        "start_date": start_date,
        "end_date": end_date,
        # 日期选择器要显示的初值：有范围给 "YYYY-MM-DD"，不限给 "all"
        "date_range_start": date_range_start,
        "date_range_end": date_range_end,
        # True = 这次范围是从用户记忆里读回来的（不是 URL 带进来的）
        "date_range_from_memory": date_range_from_memory,
        "media_count": media_count,
        "media_type_distribution": media_type_distribution,
        "score_distribution": score_distribution,
        "top_rated": top_rated,
        "status_distribution": status_distribution,
        "status_pie_chart_data": status_pie_chart_data,
        "consumption_stats": consumption_stats,
        "in_progress_count": in_progress_count,
        "rated_percent": rated_percent,
        "date_format_values": DateFormatChoices.values,
        "extra_charts": extra_charts,
        # 月度游玩趋势（app/playtime.py）。has_data=False 时模板走说明卡片。
        "monthly_playtime": monthly_playtime,
        "monthly_this_label": monthly_this_label,
        "monthly_next_label": monthly_next_label,
        "completed_percent": completed_percent,
        # 时长卡片：reading = 微信读书阅读时长，watch = 电影实测 + 剧集估算。
        # 两者 has_data=False 时模板不渲染对应卡片（匿名/无数据都走这条）。
        "reading": reading_stats,
        "watch": watch_stats,
    }

    return render(request, "app/statistics.html", context)


@require_GET
def journal(request):
    """Return the journal page: a global feed of the user's tracking activity."""
    start_date, end_date = stats.parse_activity_date_range(request)

    items_per_page = 20
    # Keyset pagination: the cursor points just past the previous page's last
    # row, so each request reads at most one page per media type regardless of
    # scroll depth (never re-scanning everything above the current page).
    cursor = history_processor.parse_journal_cursor(request)
    page_rows, has_next = history_processor.get_journal_page(
        request.user,
        start_date,
        end_date,
        limit=items_per_page,
        cursor=cursor,
    )
    entries = history_processor.build_journal_entries(page_rows, request.user)
    journal_days = history_processor.build_journal_days(entries, request.user)

    # Preserve the active date range when the feed paginates via HTMX.
    date_params = {
        key: request.GET[key]
        for key in ("start-date", "end-date")
        if key in request.GET
    }

    # Cursor for the next page: the last row rendered on this one.
    next_params = dict(date_params)
    if page_rows:
        last_date, last_type, last_id = page_rows[-1]
        next_params["cursor_date"] = last_date.isoformat()
        next_params["cursor_type"] = last_type
        next_params["cursor_id"] = last_id

    prev_day = request.GET.get("last_day", "")

    context = {
        "entries": entries,
        "journal_days": journal_days,
        # The previous page's last day, so a day split across pages isn't
        # relabelled; the last day on this page, forwarded to the next page.
        # Falls back to prev_day when this page rendered no days, so a day that
        # spans an all-filtered page isn't shown twice.
        "prev_day": prev_day,
        "last_day": journal_days[-1]["day_iso"] if journal_days else prev_day,
        "has_next": has_next,
        "next_query": urlencode(next_params),
        "filter_query": urlencode(date_params),
        "start_date": start_date,
        "end_date": end_date,
    }

    # The activity dashboard only appears on the full page, so skip its queries
    # on the HTMX partial requests that load additional feed pages. Soft
    # navigations (body swaps) still need the full page.
    if request.headers.get("HX-Request") and not request.headers.get(
        "X-Soft-Navigation"
    ):
        return render(request, "app/components/journal_items.html", context)

    context.update(
        {
            "activity_data": stats.get_activity_data(
                request.user,
                start_date,
                end_date,
            ),
            "activity_total": history_processor.get_journal_count(
                request.user,
                start_date,
                end_date,
            ),
            "date_format_values": DateFormatChoices.values,
        },
    )
    return render(request, "app/journal.html", context)


@require_GET
def service_worker():
    """Serve the service worker file."""
    sw_path = Path(settings.STATICFILES_DIRS[0]) / "js" / "serviceworker.js"
    with sw_path.open() as f:
        response = HttpResponse(f.read(), content_type="application/javascript")
        response["Service-Worker-Allowed"] = "/"
        return response


@require_GET
@login_not_required
def relay_external_image(request):
    """Serve cover images that their CDN only hands out with a referer."""
    return image_relay.relay(request)


@require_GET
@login_not_required
def steam_screenshot(request, appid, kind, filename):
    """Serve one Steam screenshot straight from the local client cache.

    Same access rule as :func:`relay_external_image`: no ``@login_required``.
    The page that embeds these images is already behind the login, and a
    browser request for an ``<img>`` source carries the session cookie anyway,
    so nothing is exposed that the page did not already show. Keeping the two
    endpoints consistent also means a cached image URL never turns into a
    redirect to the login page, which would render as a broken image.

    Every failure is a plain 404: a bad appid, a bad filename, a path that
    escapes the screenshot root, or a file that has since been deleted. The
    body is never generated, so an unknown URL costs one directory lookup.
    """
    try:
        path = steam_screenshots.resolve_screenshot_path(appid, kind, filename)
    except (OSError, ValueError) as error:
        # Deliberately broad: this view is embedded in a page, and a surprise
        # here would turn one odd filename into a 500 on someone's item page.
        logger.warning(
            "Steam screenshot %s/%s/%s failed: %s",
            appid,
            kind,
            filename,
            error,
        )
        return HttpResponseNotFound()

    if path is None:
        return HttpResponseNotFound()

    try:
        # FileResponse streams from disk and closes the handle itself, so a
        # 685 KB original never has to sit in memory whole.
        response = FileResponse(
            path.open("rb"),
            content_type=steam_screenshots.content_type_for(filename),
        )
    except OSError as error:
        logger.warning("Steam screenshot could not open %s: %s", path, error)
        return HttpResponseNotFound()

    # A capture never changes after Steam writes it, so a day of browser cache
    # costs nothing and saves the whole file on every back navigation.
    response["Cache-Control"] = "public, max-age=86400"
    return response


@require_GET
def year_in_review(request):
    """年度游玩报告页：默认今年，``?year=`` 可切年份。

    统计全部交给 ``app.year_in_review``，视图只负责解析参数、取缓存结果和渲染。
    整页一次性渲染（不走 HX-Request 片段分支），所以 hx-boost 软导航和直接
    访问拿到的是同一份完整 HTML，不存在「正文空白」那类问题。
    """
    year = year_review.resolve_year(request.GET.get("year"))
    payload = year_review.get_year_in_review(request.user.id, year)
    return render(
        request,
        "app/year_in_review.html",
        {
            "tab": "year",
            "report": payload,
            "year": year,
            "year_options": payload["years"],
        },
    )


@require_GET
def near_complete(request):
    """临门一脚：差几个成就就全成就的游戏清单。

    聚合、排序和缓存全在 ``app.near_complete`` 里，视图只取出结果渲染。
    整页一次性渲染（不走 HX-Request 片段分支），所以软导航和直接访问拿到
    的是同一份完整 HTML。
    """
    games = near_complete_agg.get_near_complete(request.user.id)
    return render(
        request,
        "app/near_complete.html",
        {
            "tab": "near",
            "games": games,
            "count": len(games),
        },
    )
