r"""验收用：一行查完 4 个基线数字。只读，绝不改库。

用法（在 yamtrack 目录下）：
    .venv\Scripts\python.exe .tools\check_baseline.py
"""

import sqlite3
from pathlib import Path

# 数据库路径按「脚本所在位置」自动推算（.tools/ 的上一级就是项目根），
# 这样别人 clone 到任何目录都能直接跑，不用改代码。
DB_FILE = Path(__file__).resolve().parent.parent / "src" / "db" / "db.sqlite3"
DB = DB_FILE.as_uri() + "?mode=ro"

EXPECTED = {
    "app_game": 4935,
    "app_item": 10171,
    "score 非空": 284,
    "status='Dropped'": 1603,
}


def main():
    conn = sqlite3.connect(DB, uri=True)
    queries = {
        "app_game": "select count(*) from app_game",
        "app_item": "select count(*) from app_item",
        "score 非空": "select count(*) from app_game where score is not null",
        "status='Dropped'": "select count(*) from app_game where status='Dropped'",
    }

    ok = True
    for label, sql in queries.items():
        actual = conn.execute(sql).fetchone()[0]
        expected = EXPECTED[label]
        flag = "OK" if actual == expected else "MISMATCH"
        if actual != expected:
            ok = False
        print(f"{label:20s} 实测 {actual:<8} 期望 {expected:<8} {flag}")

    integrity = conn.execute("pragma integrity_check").fetchone()[0]
    print(f"{'integrity_check':20s} {integrity}")

    snapshots = conn.execute("select count(*) from app_playtimesnapshot").fetchone()[0]
    print(f"{'月度快照条数':20s} {snapshots}")

    conn.close()
    print()
    print("结论：" + ("4 个基线全部保住" if ok else "⚠️ 有基线被改动，查上面 MISMATCH"))


if __name__ == "__main__":
    main()
