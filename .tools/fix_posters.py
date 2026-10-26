"""Dry-run (read-only) checker for Steam portrait posters.

Reads app_item where media_type='game' and image contains 'header',
derives https://media.steampowered.com/steam/apps/<media_id>/library_600x900.jpg (1x)
and validates each URL with a HEAD request.

Usage:
    python fix_posters.py            # dry-run: report only, NEVER writes the DB
    python fix_posters.py --write    # step 2: backup + UPDATE image only (gated)
"""

import argparse
import concurrent.futures as cf
import os
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# 仓库根：可用 YAMTRACK_ROOT 环境变量覆盖，否则按脚本位置往上推两级。
ROOT = os.environ.get("YAMTRACK_ROOT") or str(Path(__file__).resolve().parent.parent)
PROJECT = os.path.dirname(ROOT)  # 上一级：life joy
OUT_DIR = os.path.join(PROJECT, "ui-redesign", "shots", "impl")

DB = os.path.join(ROOT, "src", "db", "db.sqlite3")
BACKUP_DIR = os.path.join(ROOT, ".tools", "backups")
BACKUP_PATH = os.path.join(BACKUP_DIR, "db_before_poster_fix.sqlite3")
REPORT = os.path.join(OUT_DIR, "L-report.txt")
MANIFEST = os.path.join(OUT_DIR, "L-manifest.json")

NEW_URL = "https://media.steampowered.com/steam/apps/{appid}/library_600x900.jpg"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
TIMEOUT = 10
RETRIES = 2
RETRY_SLEEP = 0.3
WORKERS = 8


def fetch_rows():
    con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT id, media_id, title, image FROM app_item "
            "WHERE media_type='game' AND image LIKE '%header%' ORDER BY id"
        )
        return [tuple(r) for r in cur.fetchall()]
    finally:
        con.close()


def head_status(url):
    """Return (status_code_or_None, note). Retries on transient failure."""
    last = None
    for attempt in range(RETRIES + 1):
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.status, ""
        except urllib.error.HTTPError as e:
            last = ("http", e.code)
            if e.code in (403, 404, 410):
                # definitive answer for this URL; no point retrying
                return e.code, ""
        except Exception as e:  # timeout / conn reset / dns ...
            last = ("exc", type(e).__name__ + ": " + str(e)[:120])
        if attempt < RETRIES:
            time.sleep(RETRY_SLEEP)
    if last and last[0] == "http":
        return last[1], ""
    return None, last[1] if last else "unknown"


def check_one(row):
    rid, media_id, title, old = row
    appid = (media_id or "").strip()
    if not appid.isdigit():
        return {
            "id": rid, "appid": appid, "title": title, "old": old,
            "new": "", "status": None, "note": "media_id not numeric",
        }
    url = NEW_URL.format(appid=appid)
    status, note = head_status(url)
    return {
        "id": rid, "appid": appid, "title": title, "old": old,
        "new": url, "status": status, "note": note,
    }


def classify(results):
    ok, notfound, other = [], [], []
    for r in results:
        if r["status"] == 200:
            ok.append(r)
        elif r["status"] in (404, 410):
            notfound.append(r)
        else:
            other.append(r)
    return ok, notfound, other


def jpeg_size(data):
    """解析 JPEG 的 SOF 段，返回 (宽, 高)；失败返回 None。"""
    i = 2
    n = len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h
        i += 2 + seg_len
    return None


def probe_dims(rows, limit=8):
    """抽样下载，确认 1x 图确实是 300x450 竖版。"""
    out = []
    for r in rows[:limit]:
        url = NEW_URL.format(appid=r["appid"])
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = resp.read()
            wh = jpeg_size(data)
            out.append((r["appid"], r["title"], len(data), wh))
        except Exception as e:
            out.append((r["appid"], r["title"], 0, None))
    return out


def shape_of(image):
    low = (image or "").lower()
    if "header_alt_assets" in low and "schinese" in low:
        return "header_alt_assets_N_schinese.jpg"
    if "header_alt_assets" in low:
        return "header_alt_assets_N.jpg"
    if "header_schinese" in low:
        return "header_schinese.jpg"
    if "header" in low:
        return "header.jpg"
    return "other"


def write_report(results, ok, notfound, other, started, elapsed, dims=None):
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    total = len(results)
    rate = (len(ok) / total * 100) if total else 0.0
    shapes = {}
    hosts = {}
    for r in results:
        shapes[shape_of(r["old"])] = shapes.get(shape_of(r["old"]), 0) + 1
        hosts["shared.fastly" if "shared.fastly" in r["old"] else "media.steampowered"] = \
            hosts.get("shared.fastly" if "shared.fastly" in r["old"] else "media.steampowered", 0) + 1

    lines = []
    lines.append("Steam 竖版海报 dry-run 报告（只读，未写库）")
    lines.append("生成时间: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("数据库: " + DB + " (mode=ro)")
    lines.append("URL 模板: " + NEW_URL + "  (1x, 300x450)")
    lines.append("并发=%d 超时=%ds 重试=%d 间隔=%.1fs UA=Mozilla/5.0" % (WORKERS, TIMEOUT, RETRIES, RETRY_SLEEP))
    lines.append("耗时: %.1f 秒" % elapsed)
    lines.append("=" * 60)
    lines.append("待处理总数: %d（凡是 image 含 header 的 game 记录）" % total)
    lines.append("  按文件名形态: " + ", ".join("%s %d" % (k, v) for k, v in sorted(shapes.items(), key=lambda kv: -kv[1])))
    lines.append("  按原域名: " + ", ".join("%s %d" % (k, v) for k, v in sorted(hosts.items(), key=lambda kv: -kv[1])))
    lines.append("-" * 60)
    lines.append("总数: %d" % total)
    lines.append("可替换 (200): %d 条" % len(ok))
    lines.append("不可用 (404): %d 条" % len(notfound))
    lines.append("其它错误 (403/超时): %d 条" % len(other))
    lines.append("预计成功率: %.1f%%" % rate)
    lines.append("---")
    lines.append("样例：可替换 15 条（appid / 标题 / 旧URL / 新URL / HTTP状态）")
    for r in ok[:15]:
        lines.append("  %s | %s | %s | %s | %s" % (
            r["appid"], r["title"][:50], r["old"], r["new"], r["status"]))
    lines.append("---")
    lines.append("样例：不可用 10 条（这些保持原图不动）")
    for r in (notfound + other)[:10]:
        lines.append("  %s | %s | %s | %s | %s" % (
            r["appid"], r["title"][:50], r["old"], r["new"] or "-",
            r["status"] if r["status"] is not None else ("ERR " + r["note"])))
    lines.append("---")
    if dims:
        lines.append("尺寸抽检（下载真实图片解析 JPEG 头）:")
        for aid, title, nbytes, wh in dims:
            lines.append("  %-9s %-6s %7d bytes  %s" % (
                aid, ("%dx%d" % wh) if wh else "解析失败", nbytes, title[:40]))
    lines.append("---")
    # error breakdown
    if other or notfound:
        lines.append("失败原因分布:")
        buckets = {}
        for r in notfound:
            buckets["HTTP %s" % r["status"]] = buckets.get("HTTP %s" % r["status"], 0) + 1
        for r in other:
            key = ("HTTP %s" % r["status"]) if r["status"] else ("异常:" + (r["note"] or "unknown"))
            buckets[key] = buckets.get(key, 0) + 1
        for k, v in sorted(buckets.items(), key=lambda kv: -kv[1]):
            lines.append("  %-40s %d" % (k, v))
    lines.append("---")
    lines.append("可替换 appid 全量清单（供第 2 步使用）:")
    lines.append("  " + ",".join(r["appid"] for r in ok))
    lines.append("---")
    lines.append("不可替换 appid 全量清单:")
    lines.append("  " + ",".join(r["appid"] for r in notfound + other))
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return total, rate


def dry_run():
    print("[STAGE] dry-run — 只读扫描 + HEAD 验证")
    rows = fetch_rows()
    print("读到 %d 条 header 图记录，开始 HEAD 验证（并发 %d）..." % (len(rows), WORKERS))
    started = time.time()
    results = []
    lock = threading.Lock()
    done = 0
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for r in pool.map(check_one, rows):
            results.append(r)
            with lock:
                done += 1
                if done % 200 == 0:
                    print("  进度 %d/%d" % (done, len(rows)))
    elapsed = time.time() - started
    results.sort(key=lambda r: r["id"])
    ok, notfound, other = classify(results)
    print("尺寸抽检中（下载前 8 张确认是 300x450）...")
    dims = probe_dims(ok, 8)
    total, rate = write_report(results, ok, notfound, other, started, elapsed, dims)
    print("URL 模板: " + NEW_URL)
    print("总数: %d" % total)
    print("可替换 (200): %d" % len(ok))
    print("不可用 (404): %d" % len(notfound))
    print("其它错误: %d" % len(other))
    print("预计成功率: %.1f%%" % rate)
    print("报告: " + REPORT)
    return 0


def table_fingerprint(cur, exclude_image=True):
    """给 app_item 全表按「除 image 外所有列」算指纹，用来证明只改了 image。"""
    import hashlib
    cur.execute("PRAGMA table_info(app_item)")
    cols = [r[1] for r in cur.fetchall()]
    if exclude_image:
        cols = [c for c in cols if c != "image"]
    h = hashlib.sha256()
    sql = "SELECT %s FROM app_item ORDER BY id" % ",".join('"%s"' % c for c in cols)
    for row in cur.execute(sql):
        h.update(repr(row).encode("utf-8", "replace"))
    return h.hexdigest(), len(cols)


def do_write():
    """Step 2 — gated. Backup first, then UPDATE image only for 200-OK rows."""
    import json
    import random

    print("[STAGE] write — 备份 + 批量更新 image")
    rows = fetch_rows()
    print("读取 %d 条待查记录" % len(rows))
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(check_one, rows))
    ok, notfound, other = classify(results)
    print("验证通过 %d 条，将跳过 %d 条" % (len(ok), len(notfound) + len(other)))
    if not ok:
        print("没有可更新的记录，退出")
        return 1

    # ---- 备份（父代理指定路径，不另存）----
    os.makedirs(BACKUP_DIR, exist_ok=True)
    wal = DB + "-wal"
    wal_size = os.path.getsize(wal) if os.path.exists(wal) else 0
    print("WAL 文件大小: %d bytes（0 表示内容已全部落盘，直接复制文件是安全的）" % wal_size)
    if wal_size > 0:
        print("WAL 非空，先做 checkpoint 再备份")
        ck = sqlite3.connect(DB)
        ck.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        ck.commit()
        ck.close()
        wal_size = os.path.getsize(wal) if os.path.exists(wal) else 0
        print("checkpoint 后 WAL: %d bytes" % wal_size)

    shutil.copy2(DB, BACKUP_PATH)
    src_size = os.path.getsize(DB)
    bk_size = os.path.getsize(BACKUP_PATH)
    print("备份: %s (%d bytes)  源库: %d bytes  一致=%s" % (
        BACKUP_PATH, bk_size, src_size, src_size == bk_size))
    if src_size != bk_size:
        print("备份大小不一致，中止")
        return 1
    bcon = sqlite3.connect("file:" + BACKUP_PATH + "?mode=ro", uri=True)
    try:
        ic = bcon.execute("PRAGMA integrity_check").fetchone()[0]
        bcnt = bcon.execute("select count(*) from app_item").fetchone()[0]
        print("备份校验: integrity_check=%s  app_item=%d" % (ic, bcnt))
    finally:
        bcon.close()

    # ---- 写入 ----
    con = sqlite3.connect(DB, timeout=30)
    try:
        cur = con.cursor()
        before_fp, ncols = table_fingerprint(cur)
        before = {
            "app_game": cur.execute("select count(*) from app_game").fetchone()[0],
            "app_item": cur.execute("select count(*) from app_item").fetchone()[0],
            "score": cur.execute("select count(*) from app_game where score is not null").fetchone()[0],
            "dropped": cur.execute("select count(*) from app_game where status='Dropped'").fetchone()[0],
            "header": cur.execute("select count(*) from app_item where media_type='game' and image like '%header%'").fetchone()[0],
        }
        print("写前基线: %s" % before)
        print("除 image 外全部列 SHA256: %s  (%d 列)" % (before_fp, ncols))

        updated_items = []
        missed = []
        for r in ok:
            cur.execute(
                "UPDATE app_item SET image=? WHERE id=? AND media_type='game' AND image=?",
                (r["new"], r["id"], r["old"]),
            )
            if cur.rowcount == 1:
                updated_items.append(r)
                if len(updated_items) % 200 == 0:
                    print("  已更新 %d 条" % len(updated_items))
            else:
                missed.append(r["appid"])
        con.commit()
        print("  已更新 %d 条（提交完成）" % len(updated_items))

        after_fp, _ = table_fingerprint(cur)
        after = {
            "app_game": cur.execute("select count(*) from app_game").fetchone()[0],
            "app_item": cur.execute("select count(*) from app_item").fetchone()[0],
            "score": cur.execute("select count(*) from app_game where score is not null").fetchone()[0],
            "dropped": cur.execute("select count(*) from app_game where status='Dropped'").fetchone()[0],
            "header": cur.execute("select count(*) from app_item where media_type='game' and image like '%header%'").fetchone()[0],
        }
        portrait = cur.execute(
            "select count(*) from app_item where media_type='game' and image like '%library_600x900.jpg'"
        ).fetchone()[0]
        twox = cur.execute(
            "select count(*) from app_item where media_type='game' and image like '%library_600x900_2x.jpg'"
        ).fetchone()[0]

        print("-" * 60)
        print("实际更新: %d 条" % len(updated_items))
        print("验证通过但写时未命中: %d 条 %s" % (len(missed), missed[:20]))
        print("写后基线: %s" % after)
        print("除 image 外全部列 SHA256: %s" % after_fp)
        print("指纹一致: %s" % (after_fp == before_fp))
        print("竖版图总数(library_600x900): %d   2x 版: %d" % (portrait, twox))
        for k in ("app_game", "app_item", "score", "dropped"):
            flag = "OK" if before[k] == after[k] else "!!! 变了 !!!"
            print("  基线 %-9s %s -> %s  %s" % (k, before[k], after[k], flag))
        print("  header 图 %s -> %s" % (before["header"], after["header"]))

        # ---- 1003 条 404 记录是否原样 ----
        untouched = notfound + other
        same = 0
        changed_list = []
        for r in untouched:
            cur.execute("select image from app_item where id=?", (r["id"],))
            row = cur.fetchone()
            if row and row[0] == r["old"]:
                same += 1
            else:
                changed_list.append((r["appid"], r["old"], row[0] if row else None))
        print("-" * 60)
        print("未替换的 %d 条被误改数量: %d" % (len(untouched), len(changed_list)))
        if changed_list:
            print("  异常样例:", changed_list[:5])

        # ---- 抽查 15 条更新记录的新 URL ----
        random.seed(20261024)
        sample = random.sample(updated_items, min(15, len(updated_items)))
        ok15 = 0
        print("-" * 60)
        print("抽查 15 条更新后的新 URL:")
        for r in sample:
            st, note = head_status(r["new"])
            if st == 200:
                ok15 += 1
            print("  %-9s %-6s %s" % (r["appid"], "200" if st == 200 else st, r["title"][:36]))
        print("  全部 200: %s (%d/15)" % (ok15 == len(sample), ok15))

        # ---- 抽查 10 条 404 记录 ----
        sample10 = random.sample(untouched, min(10, len(untouched)))
        ok10 = 0
        print("抽查 10 条应保持原样的 404 记录:")
        for r in sample10:
            cur.execute("select image from app_item where id=?", (r["id"],))
            now = cur.fetchone()[0]
            same_one = (now == r["old"])
            if same_one:
                ok10 += 1
            print("  %-9s 未变=%-5s %s" % (r["appid"], same_one, now[:70]))
        print("  全部保持原样: %s (%d/10)" % (ok10 == len(sample10), ok10))

        # ---- 清单落盘，供后续验证 ----
        manifest = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "backup": BACKUP_PATH,
            "backup_bytes": bk_size,
            "url_template": NEW_URL,
            "updated": [{"id": r["id"], "appid": r["appid"], "title": r["title"],
                         "old": r["old"], "new": r["new"]} for r in updated_items],
            "skipped_404": [{"id": r["id"], "appid": r["appid"], "title": r["title"],
                             "image": r["old"]} for r in untouched],
            "before": before, "after": after,
            "fp_before": before_fp, "fp_after": after_fp,
            "missed": missed,
        }
        with open(MANIFEST, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
        print("-" * 60)
        print("清单已保存: " + MANIFEST)
    finally:
        con.close()
    return 0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="执行写入（需父代理确认后使用）")
    args = ap.parse_args()
    if args.write:
        return do_write()
    return dry_run()


if __name__ == "__main__":
    sys.exit(main())
