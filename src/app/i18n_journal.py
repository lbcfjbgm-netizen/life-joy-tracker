# -*- coding: utf-8 -*-
"""中文界面下的日志变更描述。

只在语言为中文时被调用；英文路径仍走 history_processor 里的原逻辑。
"""

from django.utils.translation import get_language

from app import config, helpers, i18n_zh
from app.models import MediaTypes, Status


def today_label():
    """今天的日期分组标题。"""
    if str(get_language() or "").startswith("zh"):
        return "今天"
    return "Today"


def yesterday_label():
    """昨天的日期分组标题。"""
    if str(get_language() or "").startswith("zh"):
        return "昨天"
    return "Yesterday"


def describe(field_name, old_value, new_value, media_type=None):  # noqa: C901, PLR0911
    """把一次变更描述成中文。"""
    if old_value is None:
        if field_name == "status":
            initial = {
                Status.IN_PROGRESS.value: "标记为进行中",
                Status.COMPLETED.value: "标记为已通关",
                Status.PLANNING.value: "标记为已暂停",
                Status.DROPPED.value: "标记为已放弃",
                Status.PAUSED.value: "标记为已暂停",
            }
            if new_value in initial:
                return initial[new_value]

        if field_name == "score":
            return f"评分 {new_value}/10"

        if field_name == "progress" and media_type:
            if media_type == MediaTypes.GAME.value:
                return f"游玩 {helpers.minutes_to_hhmm(new_value)}"
            unit = i18n_zh.tr(config.get_unit(media_type, short=False).lower())
            return f"进度到 {new_value} {unit}"

        if field_name in ["start_date", "end_date"]:
            label = "开始" if field_name == "start_date" else "结束"
            if new_value:
                return f"{label}于 {new_value}"
            return f"{label}（未填日期）"

        if field_name == "notes":
            return "添加了备注"

        return f"把 {field_name.replace('_', ' ').lower()} 设为 {new_value}"

    if field_name == "status":
        transitions = {
            (Status.PLANNING.value, Status.IN_PROGRESS.value): "开始进行中",
            (Status.IN_PROGRESS.value, Status.COMPLETED.value): "标记为已通关",
            (Status.IN_PROGRESS.value, Status.PAUSED.value): "标记为已暂停",
            (Status.PAUSED.value, Status.IN_PROGRESS.value): "恢复进行中",
            (Status.IN_PROGRESS.value, Status.DROPPED.value): "标记为已放弃",
        }
        return transitions.get(
            (old_value, new_value),
            f"状态从 {i18n_zh.tr(old_value)} 改为 {i18n_zh.tr(new_value)}",
        )

    if field_name == "score":
        if old_value == 0:
            return f"评分 {new_value}/10"
        return f"评分从 {old_value} 改为 {new_value}"

    if field_name == "progress":
        diff = new_value - old_value
        diff_abs = abs(diff)

        if media_type == MediaTypes.GAME.value:
            if diff > 0:
                return f"增加 {helpers.minutes_to_hhmm(diff_abs)} 游玩时长"
            return f"减少 {helpers.minutes_to_hhmm(diff_abs)} 游玩时长"

        unit = i18n_zh.tr(config.get_unit(media_type, short=False).lower())
        return f"进度设为 {new_value} {unit}"

    if field_name in ["start_date", "end_date"]:
        label = "开始" if field_name == "start_date" else "结束"
        if not new_value:
            return f"清除{label}日期"
        if not old_value:
            return f"{label}于 {new_value}"
        return f"{label}日期改为 {new_value}"

    if field_name == "notes":
        if not old_value:
            return "添加了备注"
        if not new_value:
            return "删除了备注"
        return "更新了备注"

    return f"把 {field_name.replace('_', ' ').lower()} 从 {old_value} 改成 {new_value}"
