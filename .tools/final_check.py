import sqlite3
from pathlib import Path

# 数据库路径按「脚本所在位置」自动推算，换台机器 clone 下来也能直接跑。
db = str(Path(__file__).resolve().parent.parent / "src" / "db" / "db.sqlite3")
c = sqlite3.connect(db)

print("=== [2] Steam 导入 ===")
n = c.execute("SELECT COUNT(*) FROM app_item WHERE source='steam'").fetchone()[0]
print("   steam 条目:", n)
img = c.execute(
    "SELECT COUNT(*) FROM app_item WHERE source='steam' AND image IS NOT NULL AND image <> ''"
).fetchone()[0]
print("   有封面URL:", img)
print("   丢游戏数:", "0（2664 全部导入）" if n == 2664 else f"异常，只有 {n}")

print("\n=== [3] 成就 ===")
tot = c.execute("SELECT COUNT(*) FROM app_game WHERE achievements_total > 0").fetchone()[0]
full = c.execute(
    "SELECT COUNT(*) FROM app_game "
    "WHERE achievements_total > 0 AND achievements_unlocked >= achievements_total"
).fetchone()[0]
print("   有成就数据:", tot)
print("   全成就:", full)

print("\n=== [4] 家庭共享 ===")
fam = c.execute(
    "SELECT COUNT(*) FROM app_game WHERE notes LIKE '%家庭共享%'"
).fetchone()[0]
print("   家庭共享条目:", fam, "(未配置 token，正常为 0)")

print("\n=== [5] 书籍/电影能力 ===")
for t in ("book", "movie"):
    k = c.execute(
        "SELECT COUNT(*) FROM app_item WHERE media_type=?", (t,)
    ).fetchone()[0]
    print(f"   {t} 类型条目: {k}（表存在即可用）")

print("\n=== 状态分布 ===")
for row in c.execute("SELECT status, COUNT(*) FROM app_game GROUP BY status ORDER BY 2 DESC"):
    print("  ", row[0], row[1])
