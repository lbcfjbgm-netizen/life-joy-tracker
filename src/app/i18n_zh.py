# -*- coding: utf-8 -*-
"""界面文案中文映射。

模板里的静态文案走 Django 的 {% trans %}（src/locale/zh_Hans）。
这里负责模板管不到的部分：由 Python 生成的枚举 label、动词、单位、表单字段名等。
只影响显示，不改任何业务取值（Status/MediaTypes 的 value 保持英文）。
"""

# 状态
# 界面按用户拍板的 5 档显示（Dropped 库里没有数据，保留原样）：
#   全成就 = Completed 且 is_all_achievements（靠 app_tags.media_status_readable 按对象判断）
#   已通关 = Completed（含全成就，全成就是通关的子集）
#   仅通关 = Completed 且没拿满成就（全成就的补集，只对游戏有意义）
#   进行中 = In progress
#   已暂停 = Paused 或 Planning（这两个在界面上合并成一档）
STATUS = {
    "Completed": "已通关",
    "In progress": "进行中",
    "In Progress": "进行中",
    "Planning": "已暂停",
    "Paused": "已暂停",
    "Dropped": "已放弃",
    "Repeating": "重看中",
    "All": "全部",
    # 只有 media_status_readable 用到：Completed + is_all_achievements
    "All achievements": "全成就",
    # 筛选下拉专用：Completed + not is_all_achievements
    "Completed only": "仅通关",
}

# 同一个状态值在不同媒体类型下说人话不一样：
# 游戏叫「已通关」，电影/剧集叫「已观看」，书叫「已读完」。
# 只有在拿得到媒体类型时才用这张表，拿不到（下拉选项、筛选标签、统计图）
# 就退回 STATUS 里那套统一说法。
STATUS_BY_MEDIA_TYPE = {
    "movie": {"Completed": "已观看", "In progress": "在看", "Planning": "待看", "Paused": "已搁置", "Dropped": "已弃看"},
    "tv": {"Completed": "已看完", "In progress": "在追", "Planning": "待看", "Paused": "已搁置", "Dropped": "已弃剧"},
    "season": {"Completed": "已看完", "In progress": "在追", "Planning": "待看", "Paused": "已搁置", "Dropped": "已弃剧"},
    "episode": {"Completed": "已看", "In progress": "在看", "Planning": "待看", "Paused": "已搁置", "Dropped": "已弃"},
    "anime": {"Completed": "已看完", "In progress": "在追", "Planning": "待看", "Paused": "已搁置", "Dropped": "已弃"},
    "book": {"Completed": "已读完", "In progress": "在读", "Planning": "待读", "Paused": "已搁置", "Dropped": "已弃读"},
}

# 统计图用的状态名。
# 图是按状态值聚合出来的，拿不到 is_all_achievements，所以 Completed 在图上是一整块
# （全成就也在里面，全成就本来就属于通关），统一叫「已通关」，不叫「全成就」，
# 免得把没拿满成就的 8 条也算成全成就。
STATUS_AGGREGATE = {
    "Completed": "已通关",
    "In progress": "进行中",
    "In Progress": "进行中",
    "Planning": "已暂停",
    "Paused": "已暂停",
    "Dropped": "已放弃",
}

# 媒体类型（单数 / 复数）
MEDIA_TYPES = {
    "TV Show": ("剧集", "剧集"),
    "TV Season": ("季", "季"),
    "Episode": ("分集", "分集"),
    "Movie": ("电影", "电影"),
    "Anime": ("动画", "动画"),
    "Manga": ("漫画", "漫画"),
    "Game": ("游戏", "游戏"),
    "Book": ("书籍", "书籍"),
    "Comic": ("漫画", "漫画"),
    "Boardgame": ("桌游", "桌游"),
}

# 单位（短 / 长）
UNITS = {
    "E": "集",
    "Episode": "集",
    "Chapter": "章",
    "#": "#",
    "Page": "页",
    "Issue": "期",
    "Play": "局",
}

# 动词（现在时 / 过去时）
VERBS = {
    "watch": ("观看", "看过"),
    "watched": ("观看", "看过"),
    "read": ("阅读", "读过"),
    "play": ("游玩", "玩过"),
    "played": ("游玩", "玩过"),
}

# 来源
SOURCES = {
    "The Movie Database": "The Movie Database",
    "MyAnimeList": "MyAnimeList",
    "MangaUpdates": "MangaUpdates",
    "Internet Game Database": "Internet Game Database",
    "Open Library": "Open Library",
    "Hardcover": "Hardcover",
    "Comic Vine": "Comic Vine",
    "BoardGameGeek": "BoardGameGeek",
    "Steam": "Steam",
    "Douban": "豆瓣读书",
    "WeRead": "微信读书",
    "Manual": "手动",
}

# 表单字段与排序等零散文案
MISC = {
    "Title": "标题",
    "Score": "评分",
    "Status": "状态",
    "Progress": "进度",
    "Notes": "备注",
    "Start date": "开始日期",
    "End date": "结束日期",
    "Start Date": "开始日期",
    "End Date": "结束日期",
    "Image": "图片",
    "Season number": "季数",
    "Episode number": "集数",
    "Parent TV Show": "所属剧集",
    "Parent Season": "所属季",
    "Progress (Chapters)": "进度（章）",
    "Progress (Pages)": "进度（页）",
    "Progress (Issues)": "进度（期）",
    "Progress (Plays)": "进度（局）",
    "Progress (Episodes)": "进度（集）",
    "Progress (Time Played)": "进度（游玩时长）",
    "Upcoming": "即将上映",
    "Recent": "最近",
    "Completion": "完成度",
    "Episodes Left": "剩余集数",
    "Rating": "评分",
    "Added": "添加时间",
    "Last Updated": "最近更新",
    "Last Item Added": "最近添加",
    "Items Count": "条目数量",
    "Newest First": "最新优先",
    "Date Added": "添加日期",
    "Media Type": "媒体类型",
    "Grid": "网格",
    "Table": "表格",
    "List": "列表",
    "Today": "今天",
    "Yesterday": "昨天",
    "All": "全部",
    "Select": "请选择",
    "Name": "名称",
    "Description": "简介",
    "Public": "公开",
    "Private": "私密",
    "Yes": "是",
    "No": "否",
    "Monday": "周一",
    "Sunday": "周日",
    "Current Date": "当前日期",
    "Release Date": "发行日期",
    "No Date": "不填日期",
    "14:30 (24-hour)": "14:30（24 小时制）",
    "2:30 PM (12-hour)": "下午 2:30（12 小时制）",
    "2026-01-18 (ISO)": "2026-01-18（ISO）",
    "18/01/2026 (EU)": "18/01/2026（欧式）",
    "01/18/2026 (US)": "01/18/2026（美式）",
    "Jan 18, 2026": "2026年1月18日",
    "18 Jan, 2026": "2026年1月18日",
    "Monthly": "每月",
    "Weekly": "每周",
    "Yearly": "每年",
    "Daily": "每天",
    "TV": "剧集",
    "hour": "小时",
    "TV Season": "季",
    "Season": "季",
    "Episode": "分集",
    "Add any notes or comments...": "写点备注或评论……",
    "0-10": "0-10",
}


def tr_choices(choices):
    """Translate a choices list of (value, label) pairs for display."""
    return [(value, tr(label)) for value, label in choices]


def tr_status_choices(choices, media_type=None):
    """状态下拉的 (value, label)，按媒体类型换成该类型下的说法。

    只有「状态写死一档」的三类库（电影 / 剧集 / 季）才换：
    Completed 在电影叫「已观看」、在剧集和季叫「已看完」，而不是游戏的「已通关」。
    其它媒体类型（游戏 / 书籍 / 动画 / 分集 …）维持原来的 tr_choices 行为，
    一个字都不变 —— 游戏的下拉是 5 档真实状态，不能因为这次改动换词。
    """
    from app import status_filters

    if not status_filters.is_fixed_status_type(media_type):
        return tr_choices(choices)

    if not _is_chinese():
        return list(choices)

    labels = STATUS_BY_MEDIA_TYPE.get(media_type) or {}
    return [(value, labels.get(value, tr(label))) for value, label in choices]


def tr_form_labels(form, media_type=None):
    """Translate a bound form's labels, choices and placeholders for display.

    ``media_type`` 只影响状态下拉的措辞和档位：电影 / 剧集 / 季这三类库只留
    「已观看」一档（见 status_filters.is_fixed_status_type），游戏和书籍照旧。
    """
    for name, field in form.fields.items():
        field.label = tr(field.label)
        if getattr(field, "choices", None):
            if name == "status":
                # 状态下拉里 Planning 并进「已暂停」一档，否则会出现两个同名选项。
                # 传当前值进去，是为了让现状是 Planning 的记录保存后仍是 Planning。
                from app import status_filters

                current = getattr(getattr(form, "instance", None), "status", None)
                if not current:
                    current = form.initial.get(name)
                choices = status_filters.form_status_choices(
                    field.choices,
                    current,
                    media_type,
                )
                field.choices = tr_status_choices(choices, media_type)
            else:
                field.choices = tr_choices(field.choices)
        placeholder = field.widget.attrs.get("placeholder")
        if placeholder:
            field.widget.attrs["placeholder"] = tr(placeholder)
    return form

ALL = {}
ALL.update(STATUS)
ALL.update({k: v[0] for k, v in MEDIA_TYPES.items()})
ALL.update(UNITS)
ALL.update({k: v[1] for k, v in VERBS.items()})
ALL.update(SOURCES)
ALL.update(MISC)


def _is_chinese():
    """当前界面语言是不是中文。"""
    from django.utils import translation

    return str(translation.get_language() or "").startswith("zh")


def tr(value, default=None):
    """把英文界面文案换成中文，查不到或非中文界面就原样返回。"""
    if value is None:
        return default if default is not None else value
    if not _is_chinese():
        return value
    return ALL.get(str(value), value)


def media_type_label(media_type, plural=False):
    """媒体类型的中文名。"""
    from app.models import MediaTypes

    label = MediaTypes(media_type).label if media_type in MediaTypes.values else media_type
    if not _is_chinese():
        if not plural:
            return label
        # 英文复数规则保持原样（Anime / Manga 不变复数）
        if label.lower() in [MediaTypes.ANIME.value, MediaTypes.MANGA.value]:
            return label
        return f"{label}s"
    pair = MEDIA_TYPES.get(label)
    if pair is None:
        return label
    return pair[1] if plural else pair[0]
