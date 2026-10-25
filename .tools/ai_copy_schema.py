"""打印 app_aicopy 的表结构和行数。"""

import sqlite3

con = sqlite3.connect("db/db.sqlite3")
row = con.execute(
    "select sql from sqlite_master where name='app_aicopy'",
).fetchone()
print(row[0] if row else "NOT FOUND")
print("rows", con.execute("select count(*) from app_aicopy").fetchone()[0])
print(
    "indexes",
    con.execute(
        "select name from sqlite_master where type='index' and tbl_name='app_aicopy'",
    ).fetchall(),
)
con.close()
