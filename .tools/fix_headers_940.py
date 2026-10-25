# -*- coding: utf-8 -*-
"""P1 竖版封面写库（940 条）。只改 app_item.image 一列。
复用 .tools/fix_images_p0.py 的 HEAD / 全列指纹逻辑，按本次 CSV 结构（new_vertical_url / verdict）改写。
"""
import argparse, csv, hashlib, json, os, sqlite3, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

DB = r"C:\Users\USERNAME\Downloads\life joy\yamtrack\src\db\db.sqlite3"
CSV = r"C:\Users\USERNAME\Downloads\life joy\ui-redesign\shots\impl\P1-header-scan.csv"
OUT = r"C:\Users\USERNAME\Downloads\life joy\ui-redesign\shots\impl"
BACKUPS = r"C:\Users\USERNAME\Downloads\life joy\yamtrack\.tools\backups"
UA = {"User-Agent": "Mozilla/5.0"}
PROXY = "http://127.0.0.1:7897"
BASE = "http://127.0.0.1:8001"


def head(url, timeout=20):
    """直连优先，失败再走本机代理重试一次。返回 (status, ctype|err)。"""
    last = None
    for proxy in (None, PROXY):
        try:
            op = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
            req = urllib.request.Request(url, method="HEAD", headers=UA)
            with op.open(req, timeout=timeout) as r:
                return r.status, r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            return e.code, "HTTPError"
        except Exception as e:
            last = e
    return 0, "%s: %s" % (type(last).__name__, last)


def fp_all_cols_except_image(c):
    cols = [r[1] for r in c.execute("PRAGMA table_info(app_item)") if r[1] != "image"]
    sql = "SELECT %s FROM app_item WHERE media_type='game' ORDER BY id" % ",".join('"%s"' % x for x in cols)
    h = hashlib.sha256()
    n = 0
    for row in c.execute(sql):
        h.update(repr(row).encode("utf-8"))
        n += 1
    return h.hexdigest(), n


def baselines(c):
    q = lambda s: c.execute(s).fetchone()[0]
    return {
        "app_game": q("select count(*) from app_game"),
        "app_item": q("select count(*) from app_item"),
        "score_notnull": q("select count(*) from app_game where score is not null"),
        "dropped": q("select count(*) from app_game where status='Dropped'"),
        "image_header": q("select count(*) from app_item where image like '%header%'"),
        "image_vertical": q("select count(*) from app_item where image like '%600x900%' or image like '%capsule%'"),
    }


def backup(src):
    os.makedirs(BACKUPS, exist_ok=True)
    dst = os.path.join(BACKUPS, "db-before-940-%s.sqlite3" % time.strftime("%Y%m%d-%H%M%S"))
    s = sqlite3.connect(src, timeout=60)
    s.execute("PRAGMA busy_timeout=60000")
    d = sqlite3.connect(dst)
    with d:
        s.backup(d)
    d.close()
    s.close()
    size = os.path.getsize(dst)
    chk = sqlite3.connect(dst)
    integrity = chk.execute("PRAGMA integrity_check").fetchone()[0]
    cnt = chk.execute("select count(*) from app_item").fetchone()[0]
    chk.close()
    return dst, size, integrity, cnt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all", choices=["all", "probe"])
    ap.add_argument("--skip-backup", action="store_true")
    a = ap.parse_args()

    report = {}
    # ---------- 1. 备份 ----------
    if not a.skip_backup:
        dst, size, integrity, cnt = backup(DB)
        print("[BACKUP]", dst)
        print("[BACKUP] size=%d bytes (%.2f MB) integrity=%s app_item=%d" % (size, size / 1048576, integrity, cnt))
        report["backup"] = {"path": dst, "size": size, "integrity": integrity, "app_item": cnt}

    # ---------- 2. 读 CSV ----------
    rows = list(csv.DictReader(open(CSV, encoding="utf-8-sig")))
    ok_rows = [r for r in rows if r["verdict"].strip() == "可用" and r["new_vertical_url"].strip()]
    print("[CSV] total=%d  verdict=可用=%d  (csv rows=%d)" % (len(ok_rows), len(ok_rows), len(rows)))
    report["csv_total"] = len(rows)
    report["csv_ok"] = len(ok_rows)

    # ---------- 3. HEAD 复验 ----------
    results = []
    def job(r):
        st, ct = head(r["new_vertical_url"].strip())
        time.sleep(0.1)
        return st, ct
    with ThreadPoolExecutor(max_workers=8) as ex:
        for i, (r, (st, ct)) in enumerate(zip(ok_rows, ex.map(job, ok_rows))):
            results.append({"appid": r["appid"], "title": r["title"],
                            "url": r["new_vertical_url"].strip(), "status": st, "ctype": ct})
    passed = [x for x in results if x["status"] == 200]
    failed = [x for x in results if x["status"] != 200]
    print("[HEAD] pass=%d fail=%d" % (len(passed), len(failed)))
    for x in failed:
        print("[HEAD-FAIL] appid=%s status=%s reason=%s" % (x["appid"], x["status"], str(x["ctype"])[:120]))
    report["head_pass"] = len(passed)
    report["head_fail"] = [{"appid": x["appid"], "title": x["title"], "status": x["status"],
                            "ctype": str(x["ctype"]), "url": x["url"]} for x in failed]
    if a.stage == "probe":
        json.dump(report, open(os.path.join(OUT, "_940_probe.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        return

    # ---------- 4. 写库 ----------
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA busy_timeout=60000")
    before_fp, nrows = fp_all_cols_except_image(c)
    b_before = baselines(c)
    print("[FP] before=%s rows=%d" % (before_fp, nrows))
    print("[BASE] before", b_before)
    report["fp_before"] = before_fp
    report["rows"] = nrows
    report["base_before"] = b_before

    written, skipped_same, upd_fail, missing = [], [], [], []
    batch = 0
    total = len(passed)
    for i, rec in enumerate(passed, 1):
        row = c.execute("SELECT id,image FROM app_item WHERE media_id=? AND media_type='game'",
                        (rec["appid"],)).fetchall()
        if not row:
            missing.append(rec)
            continue
        if len(row) > 1:
            upd_fail.append(dict(rec, reason="media_id 重复 %d 行" % len(row)))
            continue
        iid, old = row[0]
        if old == rec["url"]:
            skipped_same.append(rec)
            continue
        cur = c.execute(
            "UPDATE app_item SET image=? WHERE media_id=? AND media_type='game' AND image LIKE '%header%'",
            (rec["url"], rec["appid"]))
        if cur.rowcount != 1:
            upd_fail.append(dict(rec, reason="rowcount=%d (image 已不是 header?)" % cur.rowcount))
            continue
        written.append(rec)
        if len(written) % 100 == 0:
            c.commit()
            print("[PROG] %d/%d written=%d fail=%d" % (i, total, len(written), len(upd_fail) + len(missing)))
    c.commit()

    print("[WRITE] written=%d same=%d upd_fail=%d missing=%d" % (
        len(written), len(skipped_same), len(upd_fail), len(missing)))

    # ---------- 5. 验收 ----------
    after_fp, nrows2 = fp_all_cols_except_image(c)
    b_after = baselines(c)
    print("[FP] after=%s rows=%d same=%s" % (after_fp, nrows2, before_fp == after_fp))
    print("[BASE] after", b_after)
    report.update({"fp_after": after_fp, "fp_same": before_fp == after_fp,
                   "rows_after": nrows2, "base_after": b_after,
                   "written": len(written), "same": len(skipped_same),
                   "upd_fail": upd_fail, "missing": missing,
                   "written_list": [w["appid"] for w in written]})

    # 抽 15 个
    sample = []
    step = max(1, len(written) // 15)
    for i in range(0, len(written), step):
        if len(sample) >= 15:
            break
        sample.append(written[i])
    relay = []
    for rec in sample:
        u = BASE + "/image_relay?url=" + urllib.parse.quote(rec["url"], safe="")
        try:
            out = subprocess.run(["curl.exe", "-s", "-o", "NUL", "-w", "%{http_code} %{content_type}",
                                  "--max-time", "25", u],
                                 capture_output=True, text=True, encoding="utf-8", timeout=40)
            code = (out.stdout or "").strip()
        except Exception as e:
            code = "ERR %s" % e
        relay.append({"appid": rec["appid"], "code": code, "url": rec["url"]})
        print("[RELAY]", rec["appid"], code)
    report["relay"] = relay
    c.close()

    json.dump(report, open(os.path.join(OUT, "_940_stats.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("[DONE] stats ->", os.path.join(OUT, "_940_stats.json"))


if __name__ == "__main__":
    main()