"""Apply the rebuilt blocklist to the local library.

Blocked games become Dropped. Games that the *new* rule no longer blocks, but
that were already switched to Dropped by the earlier stricter pass, are restored
to their original status read from the pre-change backup.
"""

import json
import os
import sqlite3
from pathlib import Path

# 仓库根：可用 YAMTRACK_ROOT 环境变量覆盖，否则按脚本位置往上推两级。
ROOT = os.environ.get("YAMTRACK_ROOT") or str(Path(__file__).resolve().parent.parent)
DB = ROOT + r"\src\db\db.sqlite3"
BACKUP = ROOT + r"\.tools\backups\db_before_blocklist.sqlite3"
BLOCK = ROOT + r"\.tools\raw\steam_blocklist.json"
RECENT = ROOT + r"\.tools\raw\recently_played_2weeks.json"

blocked = sorted({int(e["appid"]) for e in json.load(open(BLOCK, encoding="utf-8"))["appids"]})
recent = {int(a) for a in json.load(open(RECENT, encoding="utf-8"))["appids"]}
blocked = [a for a in blocked if a not in recent]

con = sqlite3.connect(DB)
cur = con.cursor()

before = dict(cur.execute("select status, count(*) from app_game group by status").fetchall())
print("before:", before)

ph = ",".join("?" for _ in blocked)
cur.execute(
    f"""
    UPDATE app_game SET status = 'Dropped'
    WHERE status != 'Dropped'
      AND item_id IN (
        SELECT id FROM app_item
        WHERE media_type = 'game' AND CAST(media_id AS INTEGER) IN ({ph})
      )
    """,
    blocked,
)
changed = cur.rowcount

# --- restore games the new rule exempts (they were dropped by the earlier pass)
restored = 0
try:
    bcon = sqlite3.connect(f"file:{BACKUP}?mode=ro", uri=True)
    bstatus = {
        int(media_id): status
        for media_id, status in bcon.execute(
            "select i.media_id, g.status from app_item i join app_game g on g.item_id=i.id "
            "where i.media_type='game'",
        )
    }
    bcon.close()
    blocked_set = set(blocked)
    dropped_now = cur.execute(
        "select g.id, CAST(i.media_id AS INTEGER), g.status from app_game g "
        "join app_item i on i.id=g.item_id where g.status='Dropped'",
    ).fetchall()
    to_restore = []
    for game_id, appid, status in dropped_now:
        if appid in blocked_set:
            continue
        original = bstatus.get(appid)
        if original and original != "Dropped":
            to_restore.append((original, game_id))
    if to_restore:
        cur.executemany("UPDATE app_game SET status=? WHERE id=?", to_restore)
        restored = len(to_restore)
except Exception as exc:  # noqa: BLE001
    print("restore step skipped:", exc)

con.commit()

after = dict(cur.execute("select status, count(*) from app_game group by status").fetchall())
total = cur.execute("select count(*) from app_game").fetchone()[0]
print("changed to Dropped:", changed)
print("restored (10h / recently played exemption):", restored)
print("after:", after)
print("total game rows:", total)
print(
    "dropped among blocked appids:",
    cur.execute(
        f"select count(*) from app_game g join app_item i on i.id=g.item_id "
        f"where i.media_type='game' and CAST(i.media_id AS INTEGER) in ({ph}) "
        f"and g.status='Dropped'",
        blocked,
    ).fetchone()[0],
    "of", len(blocked),
)
con.close()
