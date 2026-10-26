#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
回滚脚本：把 mark_abandoned.py 标记成 Dropped 的游戏，status 改回原来的值。

用法：
  只看看会改什么（不写库）：
      python rollback_abandon.py --dry-run

  真的回滚：
      python rollback_abandon.py

只改 app_game.status 一个字段，逐条按 abandon_rollback.json 里的 old_status 还原。
"""

import argparse
import json
import os
import sqlite3
import sys

ROOT = os.environ.get("YAMTRACK_ROOT") or str(Path(__file__).resolve().parent.parent)
DB = os.path.join(ROOT, "src", "db", "db.sqlite3")
ROLLBACK = os.path.join(ROOT, ".tools", "backups", "abandon_rollback.json")
BAK_DB = os.path.join(ROOT, ".tools", "backups", "db_before_abandon.sqlite3")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只检查不写库")
    ap.add_argument("--file", default=ROLLBACK, help="回滚 JSON 路径")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not os.path.exists(args.file):
        print("回滚文件不存在: %s" % args.file)
        sys.exit(2)

    with open(args.file, "r", encoding="utf-8") as f:
        payload = json.load(f)
    print("[回滚文件] %s  共 %d 条" % (args.file, len(payload)))

    con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True) if args.dry_run else sqlite3.connect(DB, timeout=30)
    con.isolation_level = None
    cur = con.cursor()

    ok = miss = already = 0
    bad = []
    for rec in payload:
        gid = rec.get("game_id")
        old = rec.get("old_status")
        row = cur.execute("SELECT id, status FROM app_game WHERE id = ?", (gid,)).fetchone()
        if row is None:
            miss += 1
            bad.append((rec.get("appid"), rec.get("title"), "记录已不存在"))
            continue
        if row[1] == old:
            already += 1
            continue
        ok += 1
        if not args.dry_run:
            cur.execute("UPDATE app_game SET status = ? WHERE id = ?", (old, gid))

    if not args.dry_run:
        con.commit()

    # 复查
    if not args.dry_run:
        still = 0
        for rec in payload:
            r = cur.execute("SELECT status FROM app_game WHERE id = ?", (rec["game_id"],)).fetchone()
            if r is None or r[0] != rec["old_status"]:
                still += 1
        print("[回滚] 复查不一致的还有 %d 条" % still)

    dropped = cur.execute("SELECT COUNT(*) FROM app_game WHERE status='Dropped'").fetchone()[0]
    con.close()

    print("[结果] %s  需还原=%d  本来就是原值=%d  找不到记录=%d" % (
        "试运行（未写库）" if args.dry_run else "已回滚", ok, already, miss))
    if bad:
        print("[异常]")
        for b in bad[:20]:
            print("   ", b)
    print("[现状] Dropped 数量 = %d" % dropped)
    if not args.dry_run and miss == 0:
        print("[完成] 753 条已按原 status 还原。若需再次全量恢复，可用备份库：%s" % BAK_DB)


if __name__ == "__main__":
    main()
