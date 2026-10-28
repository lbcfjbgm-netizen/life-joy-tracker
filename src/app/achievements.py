"""游戏成就的共用判断与取数。

详情页和「刷新成就」按钮必须用**同一套**规则：哪个条目算能同步、卡片上那
几个数字怎么从 Game 上读。两边各写一份迟早会走样（改了一处忘了另一处），
所以集中放在这里，视图和模板都只认这一份。
"""

from app import steam_screenshots
from app.models import MediaTypes


def steam_appid_of(item):
    """返回这个条目对应的 Steam App ID，没有就返回 None。

    判断只看 media_id：它是纯数字就当 appid 用。**不看 source** ——
    手写条目里也有把 Steam 编号填进 media_id 的（比如手动登记的某个 Steam
    游戏），这类照样能同步。真正的手写条目 media_id 是 uuid，不会是纯数字，
    所以自然被挡在外面。

    Args:
        item: 一个 Item，或者 None

    Returns:
        str | None: 纯数字的 appid 字符串；不是游戏 / 没 item / 不是纯数字
        时返回 None
    """
    if item is None or item.media_type != MediaTypes.GAME.value:
        return None

    media_id = str(item.media_id or "").strip()

    # 复用截图那边的 appid 判定（^\d{1,12}$），省得两处正则各写一遍
    if not steam_screenshots.valid_appid(media_id):
        return None

    return media_id


def achievement_summary(game):
    """把 Game 上的成就字段整理成模板直接用的一小袋数。

    ``total`` 为 0（还没同步过 / 这游戏没成就系统）时 ``percent`` 给 None，
    模板据此显示「无成就清单」的空状态，不会去算除零。

    Args:
        game: 一个 Game 实例，或者 None（没有追踪记录时）

    Returns:
        dict: total / unlocked / percent / synced_at / is_all
    """
    total = getattr(game, "achievements_total", 0) or 0
    unlocked = getattr(game, "achievements_unlocked", 0) or 0

    return {
        "total": total,
        "unlocked": unlocked,
        # 只有 Game 有 achievement_percent 属性，Book / Movie / TV 完全没有；
        # 详情页对所有媒体类型都会调这个函数，所以这里必须和上面几行一样用
        # getattr 兜住，否则一读属性就 AttributeError 把整页打成 500。
        # total 为 0（非游戏、或游戏还没同步过）时给 None 而不是 0，模板据此
        # 走「无成就清单」空状态，也不会出现 0% 这种假进度。
        "percent": getattr(game, "achievement_percent", None) if total else None,
        "synced_at": getattr(game, "achievements_synced_at", None),
        "is_all": bool(getattr(game, "is_all_achievements", False)),
    }
