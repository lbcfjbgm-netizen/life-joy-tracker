"""Online hot backup of the live SQLite DB via Connection.backup().

Never copies the file cold (WAL + running web process would risk a torn read).

Usage (from ``yamtrack/``)::

    .venv\\Scripts\\python.exe scripts\\hot_backup.py <src_db> <dst_db>

Both inputs are explicit paths, so the working directory does not matter.
"""

import sqlite3
import sys
import time

src_path, dst_path = sys.argv[1], sys.argv[2]

src = sqlite3.connect(src_path)
dst = sqlite3.connect(dst_path)
t0 = time.time()
with dst:
    src.backup(dst, pages=256, sleep=0.05)
dst.close()
src.close()

chk = sqlite3.connect(f"file:{dst_path}?mode=ro", uri=True)
integrity = chk.execute("PRAGMA integrity_check").fetchone()[0]

# 表存在性检查：不写死行数，库随业务变化（删重复条目、批量导入）也能通过。
# 以前这里写死 item==2784 and game==2783，库一变就误报 BACKUP_FAILED。
required_tables = {"app_item", "app_game"}
present = {
    row[0]
    for row in chk.execute(
        "select name from sqlite_master where type='table' and name in ('app_item','app_game')",
    )
}
missing = sorted(required_tables - present)

if missing:
    item = game = syn = -1
    check_ok = False
    detail = f"missing tables: {','.join(missing)}"
else:
    item = chk.execute("select count(*) from app_item").fetchone()[0]
    game = chk.execute("select count(*) from app_game").fetchone()[0]
    syn = chk.execute("select count(*) from app_item where coalesce(synopsis,'')<>''").fetchone()[0]
    # 合理性：两张表都得有行，且 integrity_check 通过
    check_ok = integrity == "ok" and item > 0 and game > 0
    detail = "" if check_ok else f"integrity={integrity} item={item} game={game}"
chk.close()

print(f"backup took {time.time() - t0:.2f}s")
print("integrity:", integrity)
print("app_item:", item, "app_game:", game, "synopsis_nonempty:", syn)
print("tables_present:", sorted(present) if present else "none")
print("OK" if check_ok else f"BACKUP_FAILED {detail}".strip())
