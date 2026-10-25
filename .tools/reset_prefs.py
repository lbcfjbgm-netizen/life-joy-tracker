r"""测试后复位用户偏好。只改这两列，绝不碰别的。

用法（在 yamtrack 目录下）：
    .venv\Scripts\python.exe .tools\reset_prefs.py
"""

import sqlite3

DB = r"src/db/db.sqlite3"


def main():
    conn = sqlite3.connect(DB)
    before = conn.execute(
        "select statistics_date_range, list_density from users_user where id=1",
    ).fetchone()
    print("复位前:", before)

    conn.execute(
        "update users_user set statistics_date_range='', list_density='' where id=1",
    )
    conn.commit()

    after = conn.execute(
        "select statistics_date_range, list_density from users_user where id=1",
    ).fetchone()
    print("复位后:", after)
    conn.close()

    if after == ("", ""):
        print("OK：偏好已复位成空串")
    else:
        print("⚠️ 没复位成功，检查 users_user 里是不是 id=1 不是你的用户")


if __name__ == "__main__":
    main()
