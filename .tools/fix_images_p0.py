# -*- coding: utf-8 -*-
"""封面图修复 P0：只改 app_item.image 一列。
stage=fix    : 按 AC-completed-games.csv 的 41 条「可修→换竖版」改库（HEAD 200 才写）
stage=test   : 只做 HEAD 探测，不写库
"""
import argparse, csv, hashlib, json, sqlite3, sys, time, urllib.request, urllib.error, os, socket
from concurrent.futures import ThreadPoolExecutor

DB = r"C:\Users\USERNAME\Downloads\life joy\yamtrack\src\db\db.sqlite3"
CSV = r"C:\Users\USERNAME\Downloads\life joy\ui-redesign\shots\impl\AC-completed-games.csv"
OUT = r"C:\Users\USERNAME\Downloads\life joy\ui-redesign\shots\impl"
UA = {"User-Agent": "Mozilla/5.0"}
PROXY = "http://127.0.0.1:7897"


def head(url, timeout=20):
    """返回 (status, ctype)。先直连，网络层失败再走本机代理重试一次。"""
    for proxy in (None, PROXY):
        try:
            if proxy:
                op = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            else:
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            req = urllib.request.Request(url, method="HEAD", headers=UA)
            with op.open(req, timeout=timeout) as r:
                return r.status, r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except Exception as e:
            last = e
    return 0, str(last)


def fp_all_cols_except_image(c):
    cols = [r[1] for r in c.execute("PRAGMA table_info(app_item)") if r[1] != "image"]
    # 列名必须加双引号：其中有和 SQL 关键字同名的列，裸拼会语法报错
    sql = "SELECT %s FROM app_item WHERE media_type='game' ORDER BY id" % ",".join('"%s"' % x for x in cols)
    h = hashlib.sha256()
    n = 0
    for row in c.execute(sql):
        h.update(repr(row).encode("utf-8"))
        n += 1
    return h.hexdigest(), n, cols


def load_fix_rows():
    rows = list(csv.DictReader(open(CSV, encoding="utf-8-sig")))
    # 注意「不可修→只有横版」也含"可修"二字，必须排除
    fix = [r for r in rows if r["verdict"].startswith("可修") and r["best_vertical_url"].strip()]
    return fix


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="test")
    a = ap.parse_args()

    fix = load_fix_rows()
    print("候选条数:", len(fix))

    with ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(lambda r: head(r["best_vertical_url"]), fix))

    ok, bad = [], []
    for r, (st, ct) in zip(fix, codes):
        rec = {"appid": r["appid"], "title": r["title"], "url": r["best_vertical_url"],
               "status": st, "ctype": ct}
        (ok if st == 200 else bad).append(rec)
        print("HEAD %s %s %s %s" % (st, r["appid"], ct.split(";")[0], r["title"][:34]))

    print("HEAD通过:", len(ok), " 未通过:", len(bad))
    if a.stage != "fix":
        json.dump({"ok": ok, "bad": bad}, open(os.path.join(OUT, "_head_probe.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        return

    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    before_fp, nrows, cols = fp_all_cols_except_image(c)
    print("指纹(改前):", before_fp, "行数:", nrows)

    changed, failed_upd = [], []
    for rec in ok:
        row = c.execute("SELECT id,image FROM app_item WHERE media_id=? AND media_type='game'",
                        (rec["appid"],)).fetchone()
        if not row:
            failed_upd.append(dict(rec, reason="库中无此 appid"))
            print("!! 库中无此 appid", rec["appid"])
            continue
        iid, old = row
        if old == rec["url"]:
            print("= 已是目标值", rec["appid"])
            continue
        cur = c.execute("UPDATE app_item SET image=? WHERE media_id=? AND media_type='game'",
                        (rec["url"], rec["appid"]))
        if cur.rowcount != 1:
            failed_upd.append(dict(rec, reason="rowcount=%d" % cur.rowcount))
            print("!! rowcount", cur.rowcount, rec["appid"])
            c.rollback()
            continue
        c.commit()
        changed.append(rec)
        print("改 %s\n   旧 %s\n   新 %s" % (rec["appid"], old, rec["url"]))

    after_fp, nrows2, _ = fp_all_cols_except_image(c)
    print("指纹(改后):", after_fp, "行数:", nrows2)
    print("指纹一致:", before_fp == after_fp)

    stat = {
        "before_fp": before_fp, "after_fp": after_fp, "fp_same": before_fp == after_fp,
        "rows": nrows, "changed": len(changed), "head_bad": bad, "upd_fail": failed_upd,
        "changed_list": changed,
    }
    json.dump(stat, open(os.path.join(OUT, "_fix_stats.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("成功改:", len(changed), " 失败:", len(bad) + len(failed_upd))


if __name__ == "__main__":
    main()