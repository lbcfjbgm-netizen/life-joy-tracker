"""列表页状态下拉的五个档位。

models 里的 Status / MediaStatusChoices 保持原样（改 choices 会生成一堆无用的
AlterField migration），这里只在「筛选」这一层加一层映射：

- 全成就 = Completed 且 is_all_achievements=True
- 已通关 = Completed（包含全成就）
- 仅通关 = Completed 且 is_all_achievements=False（全成就被排除在外）
- 进行中 = In progress
- 已暂停 = Paused 或 Planning（Planning 不再单独出现在下拉里）

「已通关」故意包含「全成就」：日常话说「我通关了」时，全成就的人当然算通关，
全成就是通关的子集。早先这里写成「Completed 且没拿满成就」，导致 257 个
全成就游戏在「已通关」里一条都看不到，用户会以为自己的记录丢了。
现在两档是包含关系：全成就 268 条 ⊂ 已通关 301 条。

「仅通关」是反过来的那一半：只要没拿满成就的通关记录（301 − 268 = 33 条），
用来把「玩了但差几个成就」的游戏从全成就堆里挑出来。它只对游戏有意义，
所以不会出现在书/影/剧的下拉里（见 _FILTER_LABELS_BY_MEDIA_TYPE）。

不会和统计页打架：统计页（app/statistics.py）按 status 值直接聚合，
根本不过 status_q()，它的「已通关」本来就是 Completed 全量 279 条。
所以这个改动是把筛选页对齐到统计页，而不是制造分歧。

四个档位之外的老取值（All / Planning / Dropped）照旧当普通状态值处理，
这样首页「计划中」区块、旧链接都不会被顺手改坏。
"""

from __future__ import annotations

from django.db.models import Q

# 故意不 import app.models：models.py 顶层就 `from app import status_filters`，
# 反过来再 import 会变成循环导入（MediaTypes 那时还没定义）。
# 这三个就是 MediaTypes.MOVIE / TV / SEASON 的 value，写死没有维护风险。
ALL = "All"
ALL_ACHIEVEMENTS = "All achievements"
COMPLETED = "Completed"
# 界面造就的第二个伪状态（库里同样没有这个取值）：Completed 且没拿满成就。
# 拼法故意跟 "All achievements" 对称，也和老链接里不存在的值不冲突。
COMPLETED_ONLY = "Completed only"
IN_PROGRESS = "In progress"
PAUSED = "Paused"
PLANNING = "Planning"
DROPPED = "Dropped"

# 下拉里实际渲染的选项（顺序即展示顺序）。
# 「全成就」「仅通关」是同一刀切出来的两半，紧挨着放；「已通关」在中间是全量。
FILTER_CHOICES = [
    (ALL_ACHIEVEMENTS, "All achievements"),
    (COMPLETED, "Completed"),
    (COMPLETED_ONLY, "Completed only"),
    (IN_PROGRESS, "In progress"),
    (PAUSED, "Paused"),
]

# 仅供界面显示当前值：老偏好里可能还存着 All / Planning / Dropped，
# 这些不再作为下拉按钮出现，但标签要能翻成中文。
LABEL_CHOICES = [
    (ALL, "All"),
    (ALL_ACHIEVEMENTS, "All achievements"),
    (COMPLETED, "Completed"),
    (COMPLETED_ONLY, "Completed only"),
    (IN_PROGRESS, "In progress"),
    (PAUSED, "Paused"),
    (PLANNING, "Planning"),
    (DROPPED, "Dropped"),
]

FILTER_VALUES = {value for value, _ in FILTER_CHOICES}


def normalize(value):
    """把老取值归一到现在的四档，让筛选和按钮高亮保持一致。

    以前 Planning 是单独一档，如今并进「已暂停」。用户偏好字段里可能还存着
    Planning（那是合法的 choices 值），所以只在这里归到 Paused，
    下拉才会正确高亮「已暂停」并且给出 Paused + Planning 的合计数。
    """
    if value == PLANNING:
        return PAUSED
    return value


# 追踪表单（/create 页面 + 各处的追踪弹层）状态下拉用的档位。
# 和 FILTER_CHOICES 不是一回事：那边是筛选用的伪状态（全成就 / 仅通关），
# 这边是库里真实存在的 Status 取值，只是把 Planning 并进「已暂停」显示。
FORM_STATUS_CHOICES = [
    (COMPLETED, "Completed"),
    (IN_PROGRESS, "In progress"),
    (PLANNING, "Planning"),
    (PAUSED, "Paused"),
    (DROPPED, "Dropped"),
]

# 新建记录时下拉里能选到的四个值（已通关 / 进行中 / 已暂停 / 已放弃）。
FORM_STATUS_VALUES = [COMPLETED, IN_PROGRESS, PAUSED, DROPPED]


def form_status_choices(choices=None, current=None, media_type=None):
    """把 Planning 并进 Paused，给出追踪表单下拉的档位。

    ``choices`` 是原始 (value, label) 列表（默认就是模型的五个 Status）；
    ``current`` 是这条记录现在存的值，用来决定「已暂停」那一档的 value：

    - 现状是 Paused → 这一档的 value 就用 Paused，保存后还是 Paused；
    - 现状是 Planning → 这一档的 value 就用 Planning，用户没动下拉时
      保存后仍然是 Planning，不会把 2074 条计划中的记录偷偷改写成已暂停；
    - 新建或其它状态 → 用 Paused（新记录本来就该落在主档）。

    两档共用同一个 label（i18n_zh 里 Planning 和 Paused 都译作「已暂停」），
    所以下拉里只会出现一个「已暂停」。

    ``media_type`` 是电影 / 剧集 / 季时只留「已观看」（Completed）一档：
    这三类数据全部来自豆瓣，那边只有「已看过」一种标记，库里一条
    在看 / 已搁置 / 已放弃 / 待看都没有，多给的档位只会让人手滑选错。
    """
    if choices is None:
        choices = FORM_STATUS_CHOICES

    if is_fixed_status_type(media_type):
        # 现状是这三类之外的脏值（历史遗留）也统一回落到「已观看」，
        # 免得下拉里一个选中项都没有、或者把脏值原样提交回去。
        for value, label in choices:
            if value == COMPLETED:
                return [(COMPLETED, label)]
        return [(COMPLETED, "Completed")]

    keep = current if current in (PLANNING, PAUSED) else PAUSED
    labels = dict(choices)
    merged = []
    placed = False
    for value, label in choices:
        if value in (PLANNING, PAUSED):
            if placed:
                continue
            merged.append((keep, labels.get(keep) or label))
            placed = True
            continue
        merged.append((value, label))
    return merged


def is_all_achievements(value) -> bool:
    """当前拖的值是不是「全成就」这个伪状态。"""
    return value == ALL_ACHIEVEMENTS


def is_completed_only(value) -> bool:
    """当前拖的值是不是「仅通关」这个伪状态。"""
    return value == COMPLETED_ONLY


# 书目和影视没有成就系统，「全成就」这一档在那边没有任何意义：
# Book 的 status_q 会把「全成就」退化成整块 Completed，就等于「已读完」，
# 下拉里再出现一个「全成就」纯属误导。所以按媒体类型换一套词：
# 非游戏类型直接**去掉「全成就」这一档**，只留三档，避免出现两个一样的选项。
# 「仅通关」同理：没有成就系统就谈不上「没拿满成就」，所以这几张词表里
# 一个字都不写它，filter_choices_for() 的 `if value in labels` 会自动过滤掉。
_FILTER_LABELS_BY_MEDIA_TYPE = {
    "movie": {COMPLETED: "已观看", IN_PROGRESS: "在看", PAUSED: "已搁置"},
    "tv": {COMPLETED: "已看完", IN_PROGRESS: "在追", PAUSED: "已搁置"},
    "season": {COMPLETED: "已看完", IN_PROGRESS: "在追", PAUSED: "已搁置"},
    "anime": {COMPLETED: "已看完", IN_PROGRESS: "在追", PAUSED: "已搁置"},
    "book": {COMPLETED: "已读完", IN_PROGRESS: "在读", PAUSED: "已搁置"},
}

# 非游戏类型里，这两个成就相关的伪状态不该出现在下拉，但如果用户偏好里还存着
# 它们（换库之前拖过、或者是脏 URL），得能显示成一句人话，所以只进回显用的
# label 表，不进 FILTER_CHOICES。
_LABEL_ONLY_LABELS_BY_MEDIA_TYPE = {
    "movie": {ALL_ACHIEVEMENTS: "已观看", COMPLETED_ONLY: "已观看"},
    "tv": {ALL_ACHIEVEMENTS: "已看完", COMPLETED_ONLY: "已看完"},
    "season": {ALL_ACHIEVEMENTS: "已看完", COMPLETED_ONLY: "已看完"},
    "anime": {ALL_ACHIEVEMENTS: "已看完", COMPLETED_ONLY: "已看完"},
    "book": {ALL_ACHIEVEMENTS: "已读完", COMPLETED_ONLY: "已读完"},
}


# 电影 / 剧集 / 季这三个库没有「状态」这个概念：它们的数据全部从豆瓣导入，
# 豆瓣那边只有「已看过」一种标记，库里 476 部电影 + 180 部剧集 + 180 季
# 全部是 Completed，永远不会出现「在看 / 已搁置 / 已放弃 / 待看」。
# 所以这三类库的状态筛选整块不提供（列表永远全量），追踪表单的状态也只留一档。
# 游戏（5 档真实状态 + 伪状态）和书籍（在读 / 已读完 …）不受影响。
FIXED_STATUS_MEDIA_TYPES = ("movie", "tv", "season")


def is_fixed_status_type(media_type) -> bool:
    """这个媒体类型的状态是不是「写死一档」，即列表不能被状态筛空。"""
    return media_type in FIXED_STATUS_MEDIA_TYPES


def filter_choices_for(media_type):
    """按媒体类型给出状态下拉的显示文字，取值不变。

    游戏没有对应映射，原样返回 FILTER_CHOICES
    （全成就 / 已通关 / 仅通关 / 进行中 / 已暂停），2783 个游戏的筛选行为
    只在新增的「仅通关」那一档上有变化，其余一个字都不动。
    书去掉「全成就」「仅通关」两档，只留「完成 / 进行中 / 已搁置」三档。
    电影 / 剧集 / 季返回空列表：这三类库的状态筛选控件整个不渲染
    （见 media_list.html 里的 `{% if status_choices %}`），列表永远全量，
    老的脏链接 ?status=Dropped 也筛不空 —— 后端的兜底在 views.media_list。
    """
    if is_fixed_status_type(media_type):
        return []
    labels = _FILTER_LABELS_BY_MEDIA_TYPE.get(media_type)
    if not labels:
        return FILTER_CHOICES
    return [(value, labels[value]) for value, _ in FILTER_CHOICES if value in labels]


def label_choices_for(media_type):
    """同 filter_choices_for，但覆盖全部可能的历史取值，供当前值回显用。

    电影 / 剧集 / 季返回空列表：这三类库的状态控件整块不渲染，回显用的
    label 字典也就没人消费了。留着它反而会把「在看 / 已搁置 / 已放弃」
    这些词塞进页面的 x-data 里，用户搜到又以为能选。
    """
    if is_fixed_status_type(media_type):
        return []
    labels = _FILTER_LABELS_BY_MEDIA_TYPE.get(media_type)
    if not labels:
        return LABEL_CHOICES
    fallback = _LABEL_ONLY_LABELS_BY_MEDIA_TYPE.get(media_type) or {}
    all_labels = dict(labels)
    # 老偏好里的 All achievements / Completed only 归到「已完成」同一个词，回显才对得上
    all_labels.update(fallback)
    return [(value, all_labels.get(value, label)) for value, label in LABEL_CHOICES]


def status_q(status_filter, model, prefix: str = ""):
    """把筛选档位翻译成查询条件；不需要过滤时返回 None。

    ``prefix`` 用于跨关系过滤（自定义列表里的分集走 related_season__status）。
    ``model`` 用来判断有没有 is_all_achievements：只有 Game 有，Book 之类的媒体
    类型没有这个字段，硬过滤会 FieldError，所以「全成就」退化成整块 Completed，
    而「仅通关」退化成「永远不匹配」（Q(pk__in=[])）—— 没有成就系统就分不出
    「拿满了」和「没拿满」，硬造一个结果只会误导。
    「已通关」不看这个字段，本来就不受影响。
    """
    if not status_filter or status_filter == ALL:
        return None

    field_names = {field.name for field in model._meta.get_fields()}
    has_all_achievements = "is_all_achievements" in field_names and not prefix

    if status_filter == ALL_ACHIEVEMENTS:
        condition = Q(**{f"{prefix}status": COMPLETED})
        if has_all_achievements:
            condition &= Q(**{f"{prefix}is_all_achievements": True})
        return condition

    if status_filter == COMPLETED:
        # 只按 status 过滤，不再排除 is_all_achievements：全成就也是通关，
        # 「已通关」要把它们一起算进来（全成就 268 ⊂ 已通关 301）。
        return Q(**{f"{prefix}status": COMPLETED})

    if status_filter == COMPLETED_ONLY:
        # 「仅通关」= 通关了但没拿满成就，正好是「全成就」的补集（33 条）。
        # 没有成就系统的媒体类型：这一档本来就不该出现（见 filter_choices_for），
        # 万一老链接/脏参数打进来，给一条永远为假的袖子，宁可空列表也不给全量。
        extra = (
            Q(**{f"{prefix}is_all_achievements": False})
            if has_all_achievements
            else Q(pk__in=[])
        )
        return Q(**{f"{prefix}status": COMPLETED}) & extra

    if status_filter == PAUSED:
        return Q(**{f"{prefix}status__in": [PAUSED, PLANNING]})

    return Q(**{f"{prefix}status": status_filter})
