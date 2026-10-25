# -*- coding: utf-8 -*-
"""Fill zh_Hans django.po from translations.py, warning on untranslated entries."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import po  # noqa: E402
from translations import ZH, ZH_ATTRS  # noqa: E402

PO = "src/locale/zh_Hans/LC_MESSAGES/django.po"
HEADER = '''# Yamtrack 简体中文翻译
# 由本地汉化脚本生成，供个人使用。
msgid ""
msgstr ""
"Project-Id-Version: Yamtrack\\n"
"Report-Msgid-Bugs-To: \\n"
"POT-Creation-Date: 2026-10-20 18:54+0800\\n"
"PO-Revision-Date: 2026-10-20 19:00+0800\\n"
"Last-Translator: local\\n"
"Language-Team: Chinese (Simplified)\\n"
"Language: zh_Hans\\n"
"MIME-Version: 1.0\\n"
"Content-Type: text/plain; charset=UTF-8\\n"
"Content-Transfer-Encoding: 8bit\\n"
"Plural-Forms: nplurals=1; plural=0;\\n"'''

# msgid 之外，脚本额外补的翻译（模板文本里没被 trans 覆盖的部分）
EXTRA = {
    "Forbidden - Yamtrack": "禁止访问 - Yamtrack",
    "Page not found - Yamtrack": "页面不存在 - Yamtrack",
    "Server Error - Yamtrack": "服务器错误 - Yamtrack",
    "Login": "登录",
    "Sign in to your account": "登录你的账户",
    "Register": "注册",
    "Create your account": "创建你的账户",
    "Create Account": "创建账户",
    "- Yamtrack": " - Yamtrack",
    "Instance #": "记录 #",
    "Update": "更新",
    "Episode": "剧集",
    "Watched": "已看",
    "times": "次",
    "No date provided": "未提供日期",
    "Nothing is actively moving right now": "目前没有正在追的内容",
    "Your planning shelf is still empty": "你的计划清单还是空的",
    "Start a title and mark it as in progress to see quick updates and release reminders here.": "开始追一部作品并标记为进行中，这里就会显示最新动态和上映提醒。",
    "Add a few titles to planning to see them here.": "添加几部作品到计划中，这里就会显示。",
    "Browse media": "浏览媒体",
    "This removes “": "这会从历史记录中移除 “",
    "” for": "” （",
    "from your history. This can't be undone.": "），此操作无法撤销。",
    "DLCs": "DLC",
    "- Season": " - 第",
    'No seasons found matching \\"': '没有找到匹配 \\"',
    '\\". Please try a different search term.': '\\" 的季，换个关键词试试。',
    'No TV shows found matching \\"': '没有找到匹配 \\"',
    'Create - Yamtrack': '新增 - Yamtrack',
    "Create Entry": "创建记录",
    "Home - Yamtrack": "首页 - Yamtrack",
    "Journal - Yamtrack": "日志 - Yamtrack",
    "Apply Range": "应用范围",
    "%% of activity": "%% 的活动",
    "SCORE": "评分",
    "Last watched:": "最近观看：",
    "• Watched": "• 已看",
    "No": "没有",
    "Tracked Yet": "在追踪",
    "Start tracking your": "开始记录你的",
    "to keep a record of what you've": "，记录你已经",
    "Browse": "浏览",
    "'s Media:": "的媒体：",
    "Search - Yamtrack": "搜索 - Yamtrack",
    'class=\\"grid grid-cols-1 gap-4\\">': 'class="grid grid-cols-1 gap-4">',
    "Page": "第",
    "of": "页，共",
    "results)": "条结果）",
    '\\". Try checking your spelling or using different keywords.': '\\"，请检查拼写或换个关键词。',
    "Statistics - Yamtrack": "统计 - Yamtrack",
    "%% Rated": "%% 已评分",
    "Calendar - YamTrack": "日历 - Yamtrack",
    "at": "在",
    "Create New List": "新建列表",
    "Lists - Yamtrack": "列表 - Yamtrack",
    "New List": "新列表",
    "Achievements": "成就",
    "Main Story": "主线",
    "Main + Extra": "主线 + 支线",
    "Completionist": "全成就",
    "Time to Beat": "通关时长",
    "Score": "评分",
    "Overview": "概览",
    "Cast": "演员表",
    "Episodes": "剧集",
    "Details": "详情",
    "Links": "链接",
    "Streaming": "流媒体",
    "DLCs": "DLC",
    "Repeats": "重看记录",
    "Watch Date": "观看日期",
    "Media Type": "媒体类型",
    "Notes": "备注",
    "Status": "状态",
    "Title": "标题",
    "Progress": "进度",
    "collaborator": "协作者",
    "Account Connections": "账户绑定",
    "You can sign in to your account using any of the following third-party accounts:": "你可以用以下任一第三方账户登录：",
    "Remove Selected": "移除所选",
    "You currently have no third-party accounts connected to this account.": "当前没有绑定任何第三方账户。",
    "About - Yamtrack": "关于 - Yamtrack",
    "JustWatch": "JustWatch",
    "Account - Yamtrack": "账户 - Yamtrack",
    "Save Changes": "保存修改",
    "Update Password": "更新密码",
    "Manage Account Connections": "管理账户绑定",
    "Advanced - Yamtrack": "高级 - Yamtrack",
    "Export - Yamtrack": "导出 - Yamtrack",
    "Import - Yamtrack": "导入 - Yamtrack",
    "Username:": "用户名：",
    "Mode:": "模式：",
    "Last Import:": "上次导入：",
    "Next Import:": "下次导入：",
    "Regenerate": "重新生成",
    "Go to Settings > Server > Network and enable Webhooks": "进入 设置 > 服务器 > 网络，启用 Webhook",
    "In Emby, go to Settings > Notifications": "在 Emby 中进入 设置 > 通知",
    "Enable Daily Digest": "启用每日摘要",
    "Receive notifications for recently released media": "最近有媒体发行时通知我",
    "Test Notifications": "测试通知",
    "Save Settings": "保存设置",
    "Preferences - Yamtrack": "偏好设置 - Yamtrack",
    "Save Preferences": "保存偏好设置",
    "Notifications - Yamtrack": "通知 - Yamtrack",
    "Integrations - Yamtrack": "集成 - Yamtrack",
    "Search Results": "搜索结果",
    "Export Data": "导出数据",
    "id_account_": "id_account_",
    "socialaccount,": "socialaccount,",
    "Predefined Ranges": "预设范围",
    "Load all (": "加载全部（",
    'No seasons found matching "': '没有找到匹配 "',
    '". Please try a different search term.': '" 的季，换个关键词试试。',
    'No TV shows found matching "': '没有找到匹配 "',
    '". Try checking your spelling or using different keywords.': '"，请检查拼写或换个关键词。',
    "item": "条",
    "day": "天",
    "Longest:": "最长：",
    "vote": "票",
    "cast members": "位演员",
    "Repeats (": "重看记录（",
    "We're sorry, but there was an error processing your request.": "抱歉，处理你的请求时出错了。",
    "Normally, these cached results expire automatically after 24 hours. If your search results appear outdated, you can clear the cache manually.": "通常缓存结果会在 24 小时后自动过期。如果搜索结果看起来过时了，可以手动清空缓存。",
    "Hide the media card hover overlay with action buttons on touch devices for a cleaner mobile experience. On desktop, the overlay will still appear when you hover your mouse over media cards.": "在触屏设备上隐藏媒体卡片上带操作按钮的悬浮层，手机端更清爽。桌面端鼠标悬停时仍会显示。",
    "Search in collection...": "在收藏中搜索……",
    "Steam 64-bit ID": "Steam 64 位 ID",
    "Kitsu user ID": "Kitsu 用户 ID",
    "MAL username": "MAL 用户名",
    "Public AniList username": "公开的 AniList 用户名",
    "Public Trakt username": "公开的 Trakt 用户名",
    "Plex Usernames": "Plex 用户名",
    "Add to custom lists": "加入自定义列表",
    "Delete activity": "删除活动",
}

OTHER = {
    # 模板属性
    "Add any notes or comments...": "写点备注或评论……",
}

ALL = {}
ALL.update(ZH)
ALL.update(ZH_ATTRS)
ALL.update(EXTRA)
ALL.update(OTHER)

SKIP = {
    "Yamtrack", "Apprise", "Discord", "Emby", "Jellyfin", "Plex", "Slack",
    "Telegram", "Pushover", "ntfy", "JustWatch", "kitsu.app/users/",
    "discord://webhook_id/webhook_token", "https://yamtrack.mydomain.com",
    "username1, username2, username3", "top-10 lg:top-64", "top-10 lg:top-55",
}


def main():
    entries = po.parse(open(PO, encoding="utf-8").read())
    translated = 0
    missing = []
    for e in entries:
        if e["msgid"] == "":
            continue
        mid = e["msgid"]
        if mid in SKIP:
            e["msgstr"] = mid
            translated += 1
        elif mid in ALL:
            e["msgstr"] = ALL[mid]
            translated += 1
        else:
            missing.append(mid)
    po.write(entries, PO, HEADER)
    print("translated:", translated, "missing:", len(missing), "total:", len(entries) - 1)
    for m in missing:
        print("MISS:", repr(m))


if __name__ == "__main__":
    main()
