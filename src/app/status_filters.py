"""列表页状态下拉的四个档位。

models 里的 Status / MediaStatusChoices 保持原样（改 choices 会生成一堆无用的
AlterField migration），这里只在「筛选」这一层加一层映射：

- 全成就 = Completed 且 is_all_achievements=True
- 已通关 = Completed（包含全成就）
- 进行中 = In progress
- 已暂停 = Paused 或 Planning（Planning 不再单独出现在下拉里）

「已通关」故意包含「全成就」：日常话说「我通关了」时，全成就的人当然算通关，
全成就是通关的子集。早先这里写成「Completed 且没拿满成就」，导致 257 个
全成就游戏在「已通关」里一条都看不到，用户会以为自己的记录丢了。
现在两档是包含关系：全成就 257 条 ⊂ 已通关 279 条。

不会和统计页打架：统计页（app/statistics.py）按 status 值直接聚合，
根本不过 status_q()，它的「已通关」本来就是 Completed 全量 279 条。
所以这个改动是把筛选页对齐到统计页，而不是制造分歧。

四个档位之外的老取值（All / Planning / Dropped）照旧当普通状态值处理，
这样首页「计划中」区块、旧链接都不会被顺手改坏。
"""

from __future__ import annotations

from django.db.models import Q

ALL = "All"
ALL_ACHIEVEMENTS = "All achievements"
COMPLETED = "Completed"
IN_PROGRESS = "In progress"
PAUSED = "Paused"
PLANNING = "Planning"
DROPPED = "Dropped"

# 下拉里实际渲染的四个选项（顺序即展示顺序）。
FILTER_CHOICES = [
    (ALL_ACHIEVEMENTS, "All achievements"),
    (COMPLETED, "Completed"),
    (IN_PROGRESS, "In progress"),
    (PAUSED, "Paused"),
]

# 仅供界面显示当前值：老偏好里可能还存着 All / Planning / Dropped，
# 这些不再作为下拉按钮出现，但标签要能翻成中文。
LABEL_CHOICES = [
    (ALL, "All"),
    (ALL_ACHIEVEMENTS, "All achievements"),
    (COMPLETED, "Completed"),
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


def is_all_achievements(value) -> bool:
    """当前拖的值是不是「全成就」这个伪状态。"""
    return value == ALL_ACHIEVEMENTS


# 书目和影视没有成就系统，「全成就」这一档在那边没有任何意义：
# Book 的 status_q 会把「全成就」退化成整块 Completed，就等于「已读完」，
# 下拉里再出现一个「全成就」纯属误导。所以按媒体类型换一套词：
# 非游戏类型直接**去掉「全成就」这一档**，只留三档，避免出现两个一样的选项。
_FILTER_LABELS_BY_MEDIA_TYPE = {
    "movie": {COMPLETED: "已观看", IN_PROGRESS: "在看", PAUSED: "已搁置"},
    "tv": {COMPLETED: "已看完", IN_PROGRESS: "在追", PAUSED: "已搁置"},
    "season": {COMPLETED: "已看完", IN_PROGRESS: "在追", PAUSED: "已搁置"},
    "anime": {COMPLETED: "已看完", IN_PROGRESS: "在追", PAUSED: "已搁置"},
    "book": {COMPLETED: "已读完", IN_PROGRESS: "在读", PAUSED: "已搁置"},
}

# 非游戏类型里，「全成就」不该出现在下拉，但如果用户偏好里还存着这个老值，
# 得能显示成一个能看懂的字，所以只进回显用的 label 表，不进 FILTER_CHOICES。
_LABEL_ONLY_LABELS_BY_MEDIA_TYPE = {
    "movie": "已观看",
    "tv": "已看完",
    "season": "已看完",
    "anime": "已看完",
    "book": "已读完",
}


def filter_choices_for(media_type):
    """按媒体类型给出状态下拉的显示文字，取值不变。

    游戏没有对应映射，原样返回 FILTER_CHOICES（全成就 / 已通关 / 进行中 / 已暂停），
    2783 个游戏的筛选行为一个字都不动。
    书和影视去掉「全成就」档，只留「完成 / 进行中 / 已搁置」三档。
    """
    labels = _FILTER_LABELS_BY_MEDIA_TYPE.get(media_type)
    if not labels:
        return FILTER_CHOICES
    return [(value, labels[value]) for value, _ in FILTER_CHOICES if value in labels]


def label_choices_for(media_type):
    """同 filter_choices_for，但覆盖全部可能的历史取值，供当前值回显用。"""
    labels = _FILTER_LABELS_BY_MEDIA_TYPE.get(media_type)
    if not labels:
        return LABEL_CHOICES
    fallback = _LABEL_ONLY_LABELS_BY_MEDIA_TYPE.get(media_type)
    all_labels = dict(labels)
    if fallback:
        # 老偏好里的 All achievements 归到「已完成」同一个词，回显才对得上
        all_labels[ALL_ACHIEVEMENTS] = fallback
    return [(value, all_labels.get(value, label)) for value, label in LABEL_CHOICES]


def status_q(status_filter, model, prefix: str = ""):
    """把筛选档位翻译成查询条件；不需要过滤时返回 None。

    ``prefix`` 用于跨关系过滤（自定义列表里的分集走 related_season__status）。
    ``model`` 用来判断有没有 is_all_achievements：只有 Game 有，Book 之类的媒体
    类型没有这个字段，硬过滤会 FieldError，所以「全成就」退化成整块 Completed。
    「已通关」不看这个字段，本来就不受影响。
    """
    if not status_filter or status_filter == ALL:
        return None

    field_names = {field.name for field in model._meta.get_fields()}
    has_all_achievements = "is_all_achievements" in field_names

    if status_filter == ALL_ACHIEVEMENTS:
        condition = Q(**{f"{prefix}status": COMPLETED})
        if has_all_achievements:
            condition &= Q(**{f"{prefix}is_all_achievements": True})
        return condition

    if status_filter == COMPLETED:
        # 只按 status 过滤，不再排除 is_all_achievements：全成就也是通关，
        # 「已通关」要把它们一起算进来（全成就 257 ⊂ 已通关 279）。
        return Q(**{f"{prefix}status": COMPLETED})

    if status_filter == PAUSED:
        return Q(**{f"{prefix}status__in": [PAUSED, PLANNING]})

    return Q(**{f"{prefix}status": status_filter})
