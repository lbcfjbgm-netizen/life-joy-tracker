"""Scratch helper: print row counts of the tables that must never change."""

import sqlite3
import sys

path = "db/db.sqlite3"
con = sqlite3.connect(path)
for table in ("app_game", "app_item", "app_highlight", "app_note"):
    print(table, con.execute(f"select count(*) from {table}").fetchone()[0])
print(
    "game_score_nonnull",
    con.execute(
        "select count(*) from app_game where score is not null and score != 0",
    ).fetchone()[0],
)
print(
    "aicopy_tables",
    con.execute(
        "select count(*) from sqlite_master where name like '%aicopy%'",
    ).fetchone()[0],
)
print("size", __import__("os").path.getsize(path))
con.close()
sys.exit(0)
