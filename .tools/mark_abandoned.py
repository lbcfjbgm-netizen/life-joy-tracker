#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把「买了没玩、没成就、2020 年前的老游戏」标记成 Dropped（已放弃）。

两步式：
  第1步（默认，只读）：出名单
      python mark_abandoned.py

  第2步（写库，必须人等确认后才跑）：
      python mark_abandoned.py --execute
      python mark_abandoned.py --execute --exclude-529

只改 app_game.status 一个字段；写前自动备份库 + 写回滚 JSON。
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import unicodedata

ROOT = os.environ.get("YAMTRACK_ROOT") or str(Path(__file__).resolve().parent.parent)
PROJECT = os.path.dirname(ROOT)  # 上一级：life joy
DB = os.path.join(ROOT, "src", "db", "db.sqlite3")
OUT_DIR = os.path.join(PROJECT, "ui-redesign", "shots", "impl")
OUT_TXT = os.path.join(OUT_DIR, "Q-list.txt")
BAK_DIR = os.path.join(ROOT, ".tools", "backups")
BAK_DB = os.path.join(BAK_DIR, "db_before_abandon.sqlite3")
ROLLBACK = os.path.join(BAK_DIR, "abandon_rollback.json")

DIRTY_PROGRESS = 529  # 13 条 Valve 老游戏的错误游玩时长

SQL = """
SELECT
    i.media_id              AS appid,
    i.title                 AS title,
    i.title_zh              AS title_zh,
    i.release_date          AS release_date,
    g.progress              AS progress,
    g.status                AS status,
    g.id                    AS game_id,
    g.score                 AS score,
    g.end_date              AS end_date,
    g.achievements_total    AS ach_total,
    g.achievements_unlocked AS ach_unlocked
FROM app_game g
JOIN app_item i ON i.id = g.item_id
WHERE i.release_date IS NOT NULL
  AND i.release_date < '2020-01-01'
  AND (g.progress IS NULL OR g.progress < 900)
  AND (g.achievements_total IS NULL OR g.achievements_total = 0)
  AND g.status <> 'Dropped'
  AND g.score IS NULL
  AND g.end_date IS NULL
ORDER BY i.release_date, i.title
"""

BASE_SQL = """
SELECT
    (SELECT COUNT(*) FROM app_game)                                   AS n_game,
    (SELECT COUNT(*) FROM app_item)                                   AS n_item,
    (SELECT COUNT(*) FROM app_game WHERE score IS NOT NULL)           AS n_score,
    (SELECT COUNT(*) FROM app_game WHERE status = 'Dropped')          AS n_dropped,
    (SELECT COUNT(*) FROM app_game WHERE status = 'Completed')        AS n_completed,
    (SELECT COUNT(*) FROM app_game WHERE status = 'Paused')           AS n_paused,
    (SELECT COUNT(*) FROM app_game WHERE status = 'Planning')         AS n_planning,
    (SELECT COUNT(*) FROM app_game WHERE status = 'In progress')      AS n_progress
"""


# ---------- 小工具 ----------

def dwidth(s):
    """终端显示宽度：中文算 2 格。"""
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def fit(s, width):
    """按显示宽度截断/补空格。"""
    s = "" if s is None else str(s)
    if dwidth(s) > width:
        out = ""
        for ch in s:
            if dwidth(out + ch) > width - 1:
                break
            out += ch
        s = out + "…"
    return s + " " * max(0, width - dwidth(s))


def connect_ro():
    return sqlite3.connect("file:" + DB + "?mode=ro", uri=True)


def connect_rw():
    return sqlite3.connect(DB, timeout=30)


def baselines(con):
    cur = con.cursor()
    col = list(cur.execute(BASE_SQL).fetchone())
    return dict(zip(
        ["n_game", "n_item", "n_score", "n_dropped", "n_completed",
         "n_paused", "n_planning", "n_progress"],
        col,
    ))


def fingerprint(cur, ids):
    """对「除 status 外的全部列」算 SHA256，证明没动别的字段。"""
    cols = [c[1] for c in cur.execute("PRAGMA table_info(app_game)")]
    keep = [c for c in cols if c != "status"]
    sel = ", ".join('"%s"' % c for c in keep)
    h = hashlib.sha256()
    for gid in sorted(ids):
        row = cur.execute("SELECT %s FROM app_game WHERE id = ?" % sel, (gid,)).fetchone()
        h.update(repr((gid,) + tuple(row)).encode("utf-8"))
    return h.hexdigest(), len(keep)


def year_of(d):
    try:
        return int(str(d)[:4])
    except Exception:
        return 0


def minutes_of(p):
    return 0 if p is None else int(p)


# ---------- 第 1 步：出名单 ----------

def build_report(rows, base):
    dirty = [r for r in rows if r[4] == DIRTY_PROGRESS]
    normal = [r for r in rows if r[4] != DIRTY_PROGRESS]

    L = []
    a = L.append
    a("=== 条件 ===")
    a("2020年前 + 游玩<15h + 零成就 + 未打分 + 无完成日期 + 当前非Dropped")
    a("")
    a("=== 命中总数: %d ===" % len(rows))
    a("")

    head = (fit("appid", 9) + " " + fit("标题", 40) + " " +
            fit("发布", 11) + fit("分钟", 6) + "状态")

    def table(title, items):
        a("=== %s（%d 条）===" % (title, len(items)))
        a(head)
        for r in items:
            appid, title_, zh, rel, prog, status = r[0], r[1], r[2], r[3], r[4], r[5]
            name = title_ or ""
            if zh and zh != title_:
                name = "%s (%s)" % (name, zh)
            a(fit(appid or "", 9) + " " + fit(name, 40) + " " +
              fit(rel or "", 11) + fit(minutes_of(prog), 6) + (status or ""))
        a("")

    table("A 组：正常数据", normal)
    table("B 组：疑似脏数据 progress=529", dirty)

    # 年份分布
    y0 = sum(1 for r in rows if year_of(r[3]) < 2010)
    y1 = sum(1 for r in rows if 2010 <= year_of(r[3]) <= 2014)
    y2 = sum(1 for r in rows if 2015 <= year_of(r[3]) <= 2019)
    a("=== 按年份分布 ===")
    a("2010前: %d 条" % y0)
    a("2010-2014: %d 条" % y1)
    a("2015-2019: %d 条" % y2)
    a("")

    # 时长分布
    b0 = sum(1 for r in rows if minutes_of(r[4]) < 60)
    b1 = sum(1 for r in rows if 60 <= minutes_of(r[4]) < 300)
    b2 = sum(1 for r in rows if 300 <= minutes_of(r[4]) < 600)
    b3 = sum(1 for r in rows if 600 <= minutes_of(r[4]) < 900)
    null_prog = sum(1 for r in rows if r[4] is None)
    a("=== 按游玩时长分布 ===")
    a("0-59分钟: %d 条" % b0)
    a("60-299分钟(1-5h): %d 条" % b1)
    a("300-599分钟(5-10h): %d 条" % b2)
    a("600-899分钟(10-15h): %d 条" % b3)
    a("（其中 progress 为空的 %d 条，按 0 分钟计入第一档）" % null_prog)
    a("")

    # 状态分布
    st = {}
    for r in rows:
        st[r[5]] = st.get(r[5], 0) + 1
    a("=== 按当前状态分布 ===")
    for k, v in sorted(st.items(), key=lambda x: -x[1]):
        a("%s: %d 条" % (k, v))
    a("")

    # 抽查 15 条（均匀取样，覆盖不同年份）
    a("=== 抽查 15 条（随便挑，让用户看看都是些什么游戏）===")
    if rows:
        n = min(15, len(rows))
        step = len(rows) / float(n)
        picks = [rows[min(len(rows) - 1, int(i * step))] for i in range(n)]
        for idx, r in enumerate(picks, 1):
            name = r[1] or ""
            if r[2] and r[2] != r[1]:
                name = "%s (%s)" % (name, r[2])
            a("%2d. %-8s %s | %s | %s分钟 | %s" % (
                idx, r[0] or "", name, r[3] or "", minutes_of(r[4]), r[5]))
    a("")

    a("=== 写库前的基线（这一份是只读查出来的）===")
    a("Dropped 当前: %d 条" % base["n_dropped"])
    a("app_game 总行数: %d" % base["n_game"])
    a("app_item 总行数: %d" % base["n_item"])
    a("score 非空: %d" % base["n_score"])
    a("状态分布: Completed=%d / Paused=%d / Planning=%d / In progress=%d" % (
        base["n_completed"], base["n_paused"], base["n_planning"], base["n_progress"]))
    a("")
    a("=== 执行后会变成 ===")
    a("Dropped: %d → %d（%d 条全部参与）" % (base["n_dropped"], base["n_dropped"] + len(rows), len(rows)))
    a("排除 529 那批的话: Dropped 变 %d（只动 %d 条）" % (
        base["n_dropped"] + len(normal), len(normal)))
    return L, dirty, normal


def step1():
    con = connect_ro()
    cur = con.cursor()
    rows = cur.execute(SQL).fetchall()
    base = baselines(con)
    L, dirty, normal = build_report(rows, base)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_TXT, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(L) + "\n")

    # 终端只看摘要（完整 753 行在文件里）
    head_lines = L[:6]
    print("\n".join(head_lines))
    print("")
    print("B 组（疑似脏数据 progress=529）:")
    for r in dirty:
        print("  %-8s %-40s %s  %s分钟  %s" % (
            r[0] or "", (r[1] or "")[:40], r[3] or "", minutes_of(r[4]), r[5]))
    print("")
    # 分布段
    iy = L.index("=== 按年份分布 ===")
    iend = L.index("=== 抽查 15 条（随便挑，让用户看看都是些什么游戏）===")
    print("\n".join(L[iy:iend]))
    print("\n".join(L[iend:iend + 17]))
    print("")
    print("名单文件: %s" % OUT_TXT)
    print("A=%d  B=%d  总=%d" % (len(normal), len(dirty), len(rows)))
    con.close()
    return rows, dirty, normal, base


# ---------- 第 2 步：写库 ----------

def whole_db_fingerprint(cur, tables):
    """对整库所有表的全部行算一个 SHA256，用来证明备份和源库内容一致。"""
    h = hashlib.sha256()
    for t in sorted(tables):
        h.update(("T:" + t).encode("utf-8"))
        cols = [c[1] for c in cur.execute('PRAGMA table_info("%s")' % t)]
        sel = ", ".join('"%s"' % c for c in cols)
        n = 0
        for row in cur.execute('SELECT %s FROM "%s"' % (sel, t)):
            h.update(repr(row).encode("utf-8"))
            n += 1
        h.update(("n=%d" % n).encode("utf-8"))
    return h.hexdigest()


def step2(exclude_529):
    # 1) 备份
    os.makedirs(BAK_DIR, exist_ok=True)
    con_ro = connect_ro()
    if os.path.exists(BAK_DB):
        os.remove(BAK_DB)
    dst = sqlite3.connect(BAK_DB)
    con_ro.backup(dst)          # 在线热备，事务一致，不影响运行
    dst.close()
    src_size = os.path.getsize(DB)
    bak_size = os.path.getsize(BAK_DB)
    print("[备份] 源库 %d 字节 / 备份 %d 字节 / 文件大小一致=%s" % (
        src_size, bak_size, "是" if src_size == bak_size else "否"))

    # 内容级校验（WAL 模式下文件字节数可能差一页，必须比内容）
    cur_s = con_ro.cursor()
    tables = [r[0] for r in cur_s.execute(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
    fp_src = whole_db_fingerprint(cur_s, tables)
    con_ro.close()

    chk = sqlite3.connect("file:" + BAK_DB + "?mode=ro", uri=True)
    cur_b = chk.cursor()
    ic = cur_b.execute("PRAGMA integrity_check").fetchone()[0]
    fp_bak = whole_db_fingerprint(cur_b, tables)
    nb = cur_b.execute("select count(*) from app_game").fetchone()[0]
    chk.close()
    print("[备份] integrity_check=%s / app_game=%d" % (ic, nb))
    print("[备份] 全库内容指纹 源=%s..." % fp_src[:16])
    print("[备份] 全库内容指纹 备=%s..." % fp_bak[:16])
    print("[备份] 内容一致=%s（%d 张表）" % ("是" if fp_src == fp_bak else "否", len(tables)))

    # 2) 现查（避免名单过期）
    con = connect_rw()
    cur = con.cursor()
    rows = cur.execute(SQL).fetchall()
    base_before = baselines(con)
    targets = [r for r in rows if not (exclude_529 and r[4] == DIRTY_PROGRESS)]
    ids = [r[6] for r in targets]
    print("[名单] 现查命中 %d 条，本次要改 %d 条（排除529=%s）" % (
        len(rows), len(targets), "是" if exclude_529 else "否"))

    if len(ids) == 0:
        print("没有要改的记录，退出。")
        con.close()
        return

    # 3) 回滚 JSON（写库之前写）
    payload = [{
        "appid": str(r[0]),
        "title": r[1] or "",
        "title_zh": r[2] or "",
        "game_id": r[6],
        "old_status": r[5],
    } for r in targets]
    with open(ROLLBACK, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("[回滚] %s  %d 条" % (ROLLBACK, len(payload)))

    # 4) 写前指纹
    fp_before, ncols = fingerprint(cur, ids)
    print("[指纹] 写前=%s（覆盖 %d 列，不含 status）" % (fp_before, ncols))

    # 5) 只 UPDATE status
    t0 = time.time()
    done = 0
    cur.execute("BEGIN")
    for gid in ids:
        cur.execute("UPDATE app_game SET status = 'Dropped' WHERE id = ?", (gid,))
        done += 1
        if done % 100 == 0:
            print("  进度 %d/%d" % (done, len(ids)))
    con.commit()
    print("[写库] 完成 %d 条，用时 %.2fs" % (done, time.time() - t0))

    # 6) 写后指纹
    fp_after, _ = fingerprint(cur, ids)
    base_after = baselines(con)
    leftover = cur.execute(SQL).fetchone()
    con.close()

    print("")
    print("=== 第2步结果 ===")
    print("实际更新: %d 条（排除529=%s）" % (done, "是" if exclude_529 else "否"))
    print("Dropped: %d → %d" % (base_before["n_dropped"], base_after["n_dropped"]))
    print("app_game: %d → %d" % (base_before["n_game"], base_after["n_game"]))
    print("app_item: %d → %d" % (base_before["n_item"], base_after["n_item"]))
    print("score非空: %d → %d" % (base_before["n_score"], base_after["n_score"]))
    print("指纹 写前=%s 写后=%s 一致=%s" % (fp_before, fp_after,
                                        "是" if fp_before == fp_after else "否"))
    print("状态分布 after: Completed=%d Paused=%d Planning=%d Dropped=%d InProgress=%d" % (
        base_after["n_completed"], base_after["n_paused"], base_after["n_planning"],
        base_after["n_dropped"], base_after["n_progress"]))
    print("条件复查（应为空）: %s" % ("空" if leftover is None else str(leftover)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="真的写库（默认只读）")
    ap.add_argument("--exclude-529", action="store_true", help="排除 progress=529 的疑似脏数据")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not os.path.exists(DB):
        print("数据库不存在: %s" % DB)
        sys.exit(2)

    if args.execute:
        step2(args.exclude_529)
    else:
        step1()


if __name__ == "__main__":
    main()
