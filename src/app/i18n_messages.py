# -*- coding: utf-8 -*-
"""把常用提示文案翻成中文（用于 flash message / 表单错误等 Python 生成的文案）。"""
import re

from django.utils import translation

PATTERNS = [
    (r"^(.+) added successfully\.$", r"\1 添加成功。"),
    (r"^(.+) saved successfully\.$", r"\1 保存成功。"),
    (r"^(.+) was synced to (.+) successfully\.$", r"\1 已成功同步到 \2。"),
    (r"^(.+) already exists in the database\.$", r"\1 已经存在了。"),
    (r"^(.+) was deleted\.$", r"\1 已删除。"),
    (r"^(.+) deleted successfully\.$", r"\1 删除成功。"),
    (r"^(.+) was removed from the list\.$", r"\1 已从列表中移除。"),
    (r"^(.+) was added to the list\.$", r"\1 已加入列表。"),
    (r"^(.+) has been added to your list\.$", r"\1 已加入你的列表。"),
    (r"^(.+) was marked as (.+)\.$", r"\1 已标记为 \2。"),
    (r"^(.+) not found\.$", r"找不到 \1。"),
    (r"^(.+): (.+)$", r"\1：\2"),
]

EXACT = {
    "Manual items cannot be synced.": "手动添加的条目不能同步元数据。",
    "The data was recently synced, please wait a few seconds.": "数据刚刚同步过，请稍等几秒。",
    "Your profile has been updated!": "个人资料已更新！",
    "Your password has been updated!": "密码已更新！",
    "Notification settings updated successfully!": "通知设置已保存！",
    "No notification URLs configured.": "还没有配置通知地址。",
    "Preferences updated successfully!": "偏好设置已保存！",
    "Import started.": "导入已开始。",
    "Import cancelled.": "导入已取消。",
    "List created successfully!": "列表创建成功！",
    "List updated successfully!": "列表更新成功！",
    "List deleted successfully!": "列表已删除。",
    "Item added to list.": "已加入列表。",
    "Item removed from list.": "已从列表移除。",
    "Search cache cleared.": "搜索缓存已清空。",
    "Invalid request.": "请求无效。",
    "Invalid time format. Provide duration in hours (e.g., '5', '1.5'), hours and minutes (e.g., '5:30', '5h 30min'), or just minutes (e.g., '30min').": (
        "时间格式不对。可以填小时数（如 5、1.5）、时和分（如 5:30、5h 30min），"
        "或者只填分钟（如 30min）。"
    ),
    "This field is required.": "这一项必须填写。",
    "Enter a whole number.": "请输入整数。",
    "Enter a number.": "请输入数字。",
    "Enter a valid URL.": "请输入有效的网址。",
    "Enter a valid date.": "请输入有效的日期。",
    "Ensure this value is less than or equal to 10.": "这个值不能大于 10。",
    "Ensure this value is greater than or equal to 0.": "这个值不能小于 0。",
}


def tr_message(message):
    """翻译一条提示文案，中文界面之外原样返回。"""
    if not message:
        return message
    if not str(translation.get_language() or "").startswith("zh"):
        return message
    text = str(message).strip()
    if text in EXACT:
        return EXACT[text]
    for pattern, repl in PATTERNS:
        if re.match(pattern, text):
            return re.sub(pattern, repl, text)
    return message
