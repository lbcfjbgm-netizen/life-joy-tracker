import sqlite3
from pathlib import Path

# 数据库路径按「脚本所在位置」自动推算，换台机器 clone 下来也能直接跑。
db = str(Path(__file__).resolve().parent.parent / "src" / "db" / "db.sqlite3")
c = sqlite3.connect(db)

print("status counts:")
for row in c.execute("SELECT status, COUNT(*) FROM app_game GROUP BY status ORDER BY 2 DESC"):
    print(" ", row[0], row[1])

print("\nsample completed:")
q = (
    "SELECT i.title, g.achievements_unlocked, g.achievements_total "
    "FROM app_game g JOIN app_item i ON g.item_id = i.id "
    "WHERE g.status = 'completed' LIMIT 8"
)
for row in c.execute(q):
    print(" ", row[0][:32], "|", row[1], "/", row[2])

print("\nfull achievement games:")
q2 = (
    "SELECT COUNT(*) FROM app_game "
    "WHERE achievements_total > 0 AND achievements_unlocked >= achievements_total"
)
print(" ", c.execute(q2).fetchone()[0])
