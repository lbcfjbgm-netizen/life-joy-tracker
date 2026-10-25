"""Aggregate the games that are one or two steps away from full achievements.

「临门一脚」的聚合逻辑：差几个成就就全成就、而且已经解锁了不少的游戏。

口径（跟验收方给的定义一致，改之前先想清楚）：

* ``achievements_total >= 5``：成就太少的游戏不算，2/2 这种没意义；
* ``achievements_total - achievements_unlocked <= 10``：差得不多；
* ``achievements_unlocked / achievements_total >= 0.3``：已经有点进度；
* ``achievements_total - achievements_unlocked >= 1``：**差 0 个的不算**，
  那些是「已经全成就通关」，不是「临门一脚」。

最后那条是必须的：不加的话库里 100 多个「已全成就」的会一起混进来，
它们差 0 个也满足「差得不多」，但那不叫临门，叫已完成。

🔴 性能约定跟 ``app.year_in_review`` 一样：只走 ``.values()`` + 聚合，
不实例化模型（每次实例化都会 deepcopy 字段并去重关联对象），也不在循环里
访问 ``game.item``。命中行数只有个位数到几十，全部读进来也没问题，
但代码本身不依赖这一点。

游玩时长取 ``Game.progress``（单位分钟，从入库到今天的累计分钟数），
不在 ``Item`` 上；排序用它只是为了「同一差数里先看清哪款更值得捡起来」。
"""

from __future__ import annotations

from django.core.cache import cache
from django.db.models import F, Sum

from app.models import Game

# 缓存 10 分钟：成就和时长一天才同步一两次，没必要每次请求都重算。
CACHE_TTL = 600

# 口径常量，改这里即可。
MIN_ACHIEVEMENTS = 5
MAX_REMAINING = 10
# 解锁率下限，写成 3/10 而不是 0.3 的浮点数：查询里要用整数比较
# ``unlocked * 10 >= total * 3``，SQLite 上整数除法会把 3/10 算成 0，
# 直接在 SQL 里比浮点数容易在边界上出岔子。
MIN_UNLOCKED_NUM = 3
MIN_UNLOCKED_DEN = 10
MIN_UNLOCKED_SHARE = MIN_UNLOCKED_NUM / MIN_UNLOCKED_DEN


def cache_key(user_id: int) -> str:
    """Return the cache key for one user's near-complete list."""
    return f"near_complete:{user_id}"


def get_near_complete(user_id: int | None = None) -> list[dict]:
    """Return the cached near-complete list for one user.

    Args:
        user_id: 用户主键；不传时用库里第一个用户（只给 shell 手动自验用）。

    Returns:
        已排好序的列表，每项是纯 dict，可直接进模板，也能直接进缓存。
        缓存里绝不放 QuerySet 或模型实例。
    """
    owner = user_id if user_id is not None else _default_user_id()
    if owner is None:
        return []

    key = cache_key(owner)
    payload = cache.get(key)
    if payload is not None:
        return payload

    payload = build_near_complete(owner)
    cache.set(key, payload, CACHE_TTL)
    return payload


def build_near_complete(user_id: int) -> list[dict]:
    """Run the aggregation for one user and return ready-to-render rows.

    条件全部交给数据库算：``remaining`` 用 ``F()`` 表达式相减，
    「解锁占比 >= 0.3」用 ``unlocked * 10 >= total * 3`` 的整数比较，
    避免在 SQL 里做浮点除法（SQLite 上除法是整数除法，会算错）。
    """
    rows = (
        Game.objects.filter(
            user_id=user_id,
            achievements_total__gte=MIN_ACHIEVEMENTS,
        )
        # Meta.ordering 里的字段不在 values() 里，留着会被并进 GROUP BY。
        .order_by()
        .annotate(
            remaining=F("achievements_total") - F("achievements_unlocked"),
            unlocked_scaled=F("achievements_unlocked") * MIN_UNLOCKED_DEN,
            total_scaled=F("achievements_total") * MIN_UNLOCKED_NUM,
        )
        .filter(
            remaining__gte=1,
            remaining__lte=MAX_REMAINING,
            unlocked_scaled__gte=F("total_scaled"),
        )
        .values(
            "item__title",
            "item__image",
            "item__media_id",
            "item__source",
            "remaining",
            "achievements_total",
            "achievements_unlocked",
            "status",
        )
        .annotate(minutes=Sum("progress"))
        # 先按「还差几个」升序，同差数按累计时长降序。
        .order_by("remaining", "-minutes")
    )

    out = []
    for row in rows:
        minutes = row["minutes"] or 0
        total = row["achievements_total"]
        unlocked = row["achievements_unlocked"]
        out.append(
            {
                "title": row["item__title"],
                "image": row["item__image"],
                "media_id": row["item__media_id"],
                "source": row["item__source"],
                "status": row["status"],
                "remaining": row["remaining"],
                "total": total,
                "unlocked": unlocked,
                "percent": round(unlocked / total * 100) if total else 0,
                "minutes": minutes,
                "hours": round(minutes / 60, 1),
            },
        )
    return out


def _default_user_id() -> int | None:
    """Return the first user id, for manual shell checks only."""
    from users.models import User  # noqa: PLC0415

    return User.objects.order_by("pk").values_list("pk", flat=True).first()


__all__ = [
    "CACHE_TTL",
    "MAX_REMAINING",
    "MIN_ACHIEVEMENTS",
    "MIN_UNLOCKED_DEN",
    "MIN_UNLOCKED_NUM",
    "MIN_UNLOCKED_SHARE",
    "build_near_complete",
    "cache_key",
    "get_near_complete",
]
