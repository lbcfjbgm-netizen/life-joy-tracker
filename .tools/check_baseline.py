r"""验收用：一行查完 4 个基线数字。只读，绝不改库。

用法（在 yamtrack 目录下）：
    .venv\Scripts\python.exe .tools\check_baseline.py
    .venv\Scripts\python.exe .tools\check_baseline.py --update   # 把当前值存成新基线

关于「期望值」为什么要能变：
    原来的期望值是写死的 4935 / 10171 / 284 / 1603，那是 2026-10-25 快照。
    但用户会持续录新游戏，录一个 app_game +1、app_item +1、有评分则 score +1。
    写死的话每录一款就报 MISMATCH，验收脚本自己先失灵了。

所以现在按「方向」判断：
    · 这三个计数**允许增长**（录新游戏），增长记为 INFO 不算失败
    · **减少一律算异常**（REDUCED）—— 删数据才会减少，那是真事故
    · status='Dropped' / integrity_check 仍然严格比对

基线来源优先级：
    1. --update 时把当前值写进 .tools/baseline.json
    2. 否则读 .tools/baseline.json（若存在）
    3. 否则用下面写死的出厂值
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# 数据库路径按「脚本所在位置」自动推算（.tools/ 的上一级就是项目根），
# 这样别人 clone 到任何目录都能直接跑，不用改代码。
TOOLS_DIR = Path(__file__).resolve().parent
DB_FILE = TOOLS_DIR.parent / "src" / "db" / "db.sqlite3"
DB = DB_FILE.as_uri() + "?mode=ro"
STATE_FILE = TOOLS_DIR / "baseline.json"

# 出厂值（2026-10-25 快照）。baseline.json 存在时以它为准。
FACTORY = {
    "app_game": 4935,
    "app_item": 10171,
    "score 非空": 284,
    "status='Dropped'": 1603,
}

# 这些计数会因为「正常录入新条目」而增长，所以只禁止减少。
GROWABLE = {"app_game", "app_item", "score 非空"}

QUERIES = {
    "app_game": "select count(*) from app_game",
    "app_item": "select count(*) from app_item",
    "score 非空": "select count(*) from app_game where score is not null",
    "status='Dropped'": "select count(*) from app_game where status='Dropped'",
}


def load_expected() -> dict[str, int]:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and all(k in data for k in FACTORY):
                return {k: int(v) for k, v in data.items() if k in FACTORY}
        except (OSError, ValueError, TypeError):
            pass
    return dict(FACTORY)


def main() -> int:
    update = "--update" in sys.argv

    want_write = update
    if want_write:
        # --update 要写文件，得用可写连接
        conn = sqlite3.connect(str(DB_FILE))
    else:
        conn = sqlite3.connect(DB, uri=True)

    actual = {label: conn.execute(sql).fetchone()[0] for label, sql in QUERIES.items()}
    integrity = conn.execute("pragma integrity_check").fetchone()[0]
    snapshots = conn.execute("select count(*) from app_playtimesnapshot").fetchone()[0]
    conn.close()

    if update:
        STATE_FILE.write_text(
            json.dumps({k: actual[k] for k in FACTORY}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        print(f"已把当前值写进基线：{STATE_FILE}")
        print()

    expected = load_expected()

    failed = False
    noted = False
    for label in QUERIES:
        got = actual[label]
        want = expected[label]
        if got == want:
            flag = "OK"
        elif label in GROWABLE and got > want:
            flag = "INFO(增长)"
            noted = True
        elif label in GROWABLE:
            flag = "REDUCED"
            failed = True
        else:
            flag = "MISMATCH"
            failed = True
        print(f"{label:20s} 实测 {got:<8} 基线 {want:<8} {flag}")

    print(f"{'integrity_check':20s} {integrity}")
    if integrity != "ok":
        failed = True
    print(f"{'月度快照条数':20s} {snapshots}")

    print()
    if failed:
        print("结论：❌ 有基线被改动（减了数据，或 Dropped 变了），查上面非 OK 的行")
        return 1
    if noted:
        print(
            "结论：✅ 基线完好；有计数比出厂值高，属于正常录入新条目。"
            "\n      确认没问题就加 --update 把它存成新基线。",
        )
    else:
        print("结论：✅ 4 个基线全部保住")
    return 0


if __name__ == "__main__":
    sys.exit(main())
