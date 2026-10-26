"""在线热备数据库（不影响站点运行），并校验备份可用。"""

import os
import sqlite3
from pathlib import Path

# 仓库根：可用 YAMTRACK_ROOT 环境变量覆盖，否则按脚本位置往上推两级。
ROOT = os.environ.get("YAMTRACK_ROOT") or str(Path(__file__).resolve().parent.parent)

SRC = os.path.join(ROOT, "src", "db", "db.sqlite3")
DST = os.path.join(ROOT, ".tools", "backups", "db-before-memory-20261025-0230.sqlite3")

os.makedirs(os.path.dirname(DST), exist_ok=True)

src = sqlite3.connect(SRC)
dst = sqlite3.connect(DST)
src.backup(dst)
dst.close()
src.close()

print("备份路径:", DST)
print("大小 MB:", round(os.path.getsize(DST) / 1024 / 1024, 1))

uri = "file:" + DST.replace("\\", "/") + "?mode=ro"
chk = sqlite3.connect(uri, uri=True)
print("integrity:", chk.execute("pragma integrity_check").fetchone()[0])
print("users_user 行数:", chk.execute("select count(*) from users_user").fetchone()[0])
print("app_game 行数:", chk.execute("select count(*) from app_game").fetchone()[0])
print("app_item 行数:", chk.execute("select count(*) from app_item").fetchone()[0])
cols = [r[1] for r in chk.execute("pragma table_info(users_user)")]
print("备份里是否已含新列:", [c for c in cols if "density" in c or "statistics" in c] or "没有（符合预期）")
chk.close()
