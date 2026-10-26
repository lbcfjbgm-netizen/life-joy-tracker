# -*- coding: utf-8 -*-
"""只读核查：已通关(Completed)游戏 的 竖版 / 横版 图片可用性。

- 数据库以 mode=ro 打开，绝不写入
- 只发 HEAD 请求，不下载图片本体
- 并发 8、超时 10s、失败重试 2 次
- 输出 AC-report.txt（人读） + AC-completed-games.csv（UTF-8 with BOM）
"""
import csv
import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("YAMTRACK_ROOT") or str(Path(__file__).resolve().parent.parent)
PROJECT = os.path.dirname(ROOT)  # 上一级：life joy
DB = os.path.join(ROOT, "src", "db", "db.sqlite3")
OUT_DIR = os.path.join(PROJECT, "ui-redesign", "shots", "impl")
REPORT = os.path.join(OUT_DIR, "AC-report.txt")
CSVPATH = os.path.join(OUT_DIR, "AC-completed-games.csv")

WORKERS = 8
TIMEOUT = 10
RETRIES = 2
HEADERS = {"User-Agent": "Mozilla/5.0"}

RETRY_STATUS = {408, 429, 500, 502, 503, 504}

MEDIA = "https://media.steampowered.com/steam/apps/{id}/{name}"
FASTLY = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{id}/{name}"
FASTLY_ROOT = "https://shared.fastly.steamstatic.com/store_item_assets/"
STORE_API = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
API_BATCH = 50
API_TRIES = 5


def probe_manifest(rows):
    """查 Steam 官方 StoreBrowse 接口拿每个游戏的权威资产清单。

    新版游戏库里没有 library_600x900.jpg，Steam 把竖版放在
    store_item_assets/steam/apps/<id>/<hash>/library_capsule.jpg
    （文件名不一样，但实测像素就是 300x450，比例 0.667，正好是竖版）。
    只走这一步才能判断「到底有没有竖版」，直接拼 library_600x900.jpg 会误判。
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Accept"] = "*/*"
    out = {}
    ids = [{"appid": int(r[0])} for r in rows]
    for i in range(0, len(ids), API_BATCH):
        chunk = ids[i:i + API_BATCH]
        payload = {
            "ids": chunk,
            "context": {"language": "schinese", "country_code": "CN", "steam_realm": 1},
            "data_request": {"include_assets": True},
        }
        for attempt in range(API_TRIES):
            try:
                r = session.get(STORE_API, params={"input_json": json.dumps(payload)}, timeout=40)
                if r.status_code == 200:
                    for it in r.json().get("response", {}).get("store_items", []):
                        out[str(it.get("appid"))] = it.get("assets", {}) or {}
                    break
            except Exception:  # noqa: BLE001
                time.sleep(1.5 * (attempt + 1))
        print("  manifest batch %d/%d  got=%d" % (min(i + API_BATCH, len(ids)), len(ids), len(out)))
    session.close()
    return out



def head(url, session):
    """返回 (status, content_length, content_type)。异常返回 ('ERR', 0, 异常名)。"""
    last = ("ERR", 0, "unknown")
    for attempt in range(RETRIES + 1):
        try:
            r = session.head(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
            cl_raw = r.headers.get("Content-Length") or "0"
            try:
                cl = int(cl_raw)
            except ValueError:
                cl = 0
            if r.status_code in RETRY_STATUS and attempt < RETRIES:
                last = (r.status_code, cl, r.headers.get("Content-Type", ""))
                time.sleep(0.4 * (attempt + 1))
                continue
            return (r.status_code, cl, r.headers.get("Content-Type", ""))
        except Exception as e:  # noqa: BLE001
            last = ("ERR", 0, type(e).__name__)
            if attempt < RETRIES:
                time.sleep(0.4 * (attempt + 1))
                continue
    return last


def db_image_type(image, appid):
    """归类库里 image 的形态。"""
    s = (image or "").strip()
    if not s:
        return "empty"
    if s.startswith("data:"):
        return "empty"
    # 只认本 appid 的资产路径
    if "library_600x900" in s:
        return "vertical"
    if "library_hero" in s:
        return "hero"
    if "header" in s:
        return "header"
    if "capsule" in s:
        return "capsule"
    return "other"


def fastly_hashed_base(image, appid):
    """库里若是 fastly 带 hash 的路径，返回同一 hash 目录（备用）。"""
    m = re.search(r"store_item_assets/steam/apps/%s/([0-9a-f]{20,})/" % re.escape(appid), image or "")
    if m:
        return "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/%s/%s/" % (appid, m.group(1))
    return None


def build_asset_url(assets, key):
    """assets[key] -> 可访问 URL（新版是 steam/apps/<id>/<hash>/<file>，老版是相对文件名）。"""
    if not assets:
        return None
    fn = assets.get(key)
    if not fn:
        return None
    fmt = assets.get("asset_url_format") or ""
    if not fmt:
        return None
    path = fmt.replace("${FILENAME}", fn)
    if not path.startswith("steam/"):
        path = "steam/apps/" + path.lstrip("/")
    return FASTLY_ROOT + path


def db_path_probe(image, session):
    """直接用库里存的那条 URL 发 HEAD。库里存的是 header_schinese / 带 hash 的老路径时，
    标准路径探测会误判成「无图」，这里以库里真实 URL 为准。"""
    u = (image or "").strip()
    if not u.startswith("http"):
        return ("SKIP", 0, "")
    return head(u, session)


def probe_one(row, assets):
    appid, title, image = row
    session = requests.Session()
    # ① 标准老路径
    v = head(MEDIA.format(id=appid, name="library_600x900.jpg"), session)
    hv = head(MEDIA.format(id=appid, name="library_hero.jpg"), session)
    hd = head(MEDIA.format(id=appid, name="header.jpg"), session)
    # ④⑤ fastly 标准路径
    vf = head(FASTLY.format(id=appid, name="library_600x900.jpg"), session)
    hf = head(FASTLY.format(id=appid, name="library_hero.jpg"), session)
    hd_fastly = head(FASTLY.format(id=appid, name="header.jpg"), session)
    # ⑥⑦ 官方资产清单里的竖版和 header（新游戏文件名带 hash，直接拼标准名会 404）
    mc = mc2 = hd_manifest = ("SKIP", 0, "")
    if assets:
        u1 = build_asset_url(assets, "library_capsule")
        u2 = build_asset_url(assets, "library_capsule_2x")
        u3 = build_asset_url(assets, "header")
        if u1:
            mc = head(u1, session)
        if u2:
            mc2 = head(u2, session)
        if u3:
            hd_manifest = head(u3, session)
    # 库里实际存的那条 URL
    dbp = db_path_probe(image, session)
    session.close()

    has_v_std = v[0] == 200 or vf[0] == 200
    has_v_manifest = mc[0] == 200
    has_v = has_v_std or has_v_manifest
    has_hv_manifest = False
    if assets and not (hv[0] == 200 or hf[0] == 200):
        # 官方清单里的横版
        s2 = requests.Session()
        s2.headers.update(HEADERS)
        u = build_asset_url(assets, "library_hero")
        if u:
            has_hv_manifest = head(u, s2)[0] == 200
        s2.close()
    has_hv = hv[0] == 200 or hf[0] == 200 or has_hv_manifest
    has_hd = hd[0] == 200 or hd_fastly[0] == 200 or hd_manifest[0] == 200
    db_ok = dbp[0] == 200
    if not has_hd and db_ok and db_image_type(image, appid) in ("header", "capsule"):
        has_hd = True

    if v[0] == 200:
        v_bytes = v[1]
    elif vf[0] == 200:
        v_bytes = vf[1]
    elif mc[0] == 200:
        v_bytes = mc[1]
    else:
        v_bytes = 0
    if hv[0] == 200:
        hv_bytes = hv[1]
    elif hf[0] == 200:
        hv_bytes = hf[1]
    elif has_hv_manifest:
        sep = requests.Session()
        sep.headers.update(HEADERS)
        u = build_asset_url(assets, "library_hero")
        hv_bytes = head(u, sep)[1] if u else 0
        sep.close()
    else:
        hv_bytes = 0
    if hd[0] == 200:
        hd_bytes = hd[1]
    elif hd_fastly[0] == 200:
        hd_bytes = hd_fastly[1]
    elif hd_manifest[0] == 200:
        hd_bytes = hd_manifest[1]
    else:
        hd_bytes = 0

    return {
        "appid": appid,
        "title": title,
        "image": image or "",
        "db_type": db_image_type(image, appid),
        "has_v": has_v,
        "has_hv": has_hv,
        "has_hd": has_hd,
        "has_vf": vf[0] == 200,
        "has_hf": hf[0] == 200,
        "v_status": v[0],
        "hv_status": hv[0],
        "hd_status": hd[0],
        "vf_status": vf[0],
        "hf_status": hf[0],
        "hd_fastly_status": hd_fastly[0],
        "manifest_vertical_url": build_asset_url(assets, "library_capsule") if assets else None,
        "manifest_vertical_status": mc[0],
        "manifest_hero_status": "MANIFEST-OK" if has_hv_manifest else "MANIFEST-NO",
        "has_v_std": has_v_std,
        "best_vertical_url": (
            MEDIA.format(id=appid, name="library_600x900.jpg") if v[0] == 200
            else (FASTLY.format(id=appid, name="library_600x900.jpg") if vf[0] == 200
                  else (build_asset_url(assets, "library_capsule") if mc[0] == 200 else None))
        ),
        "db_url_status": dbp[0],
        "db_url_bytes": dbp[1] if db_ok else 0,
        "db_ok": db_ok,
        "v_bytes": v_bytes,
        "hv_bytes": hv_bytes,
        "hd_bytes": hd_bytes,
        "all_err": all(x[0] == "ERR" for x in (v, hv, hd, vf, hf)),
    }


def verdict(rec):
    """A/B/C/D/E 分类。

    判定一律以「实际能拿到图没有」为准：库里的 URL 本身能 200 就算有图，
    避免把 header_schinese.jpg / library_capsule.jpg 这类特殊路径误判成「无图」。
    """
    if rec["db_type"] == "vertical":
        return ("E", "正常") if rec["has_v"] else ("C", "图挂了→竖版404")
    # 非竖版（header / header_schinese / capsule / hero / 空）
    if rec["has_v"]:
        return "A", "可修→换竖版"
    if rec["has_hv"] or rec["has_hd"] or rec["db_ok"]:
        return "B", "不可修→只有横版"
    return "D", "无图"


def mark(b):
    return "✅" if b else "❌"


def pad(s, width):
    """按显示宽度粗略补位（CJK 记 2 宽）。"""
    s = str(s)
    w = sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)
    return s + " " * max(0, width - w)


def main():
    t0 = time.time()
    if not os.path.exists(DB):
        print("DB not found:", DB)
        return 1
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    cur = con.cursor()
    rows = cur.execute(
        """
        SELECT i.media_id, i.title, i.image
        FROM app_game g JOIN app_item i ON i.id = g.item_id
        WHERE g.status = 'Completed'
        ORDER BY i.title
        """
    ).fetchall()
    con.close()

    total = len(rows)
    print("[LOAD] %d rows" % total)

    bad = [r for r in rows if not re.fullmatch(r"\d+", str(r[0] or ""))]
    good = [r for r in rows if re.fullmatch(r"\d+", str(r[0] or ""))]

    print("[MANIFEST] 查 Steam 官方资产清单 ...")
    manifests = probe_manifest(good)

    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(probe_one, r, manifests.get(str(r[0]))) for r in good]
        for i, fut in enumerate(futs, 1):
            results.append(fut.result())
            if i % 25 == 0:
                print("  probed %d/%d  (%.0fs)" % (i, len(good), time.time() - t0))

    # 网络健康检查
    err_cnt = sum(1 for r in results if r["all_err"])
    got_status = len(results) - err_cnt
    rate = got_status / len(results) if results else 0
    print("[NET] responded %d/%d = %.1f%%" % (got_status, len(results), rate * 100))
    if rate < 0.80:
        print("!! HEAD 请求大面积失败（成功率 %.1f%% < 80%%），停止，请检查网络。" % (rate * 100))
        return 2

    # 分类
    A, B, C, D, E = [], [], [], [], []
    for r in results:
        cls, note = verdict(r)
        r["cls"] = cls
        r["note"] = note
        {"A": A, "B": B, "C": C, "D": D, "E": E}[cls].append(r)

    n_v = sum(1 for r in results if r["has_v"])
    n_v_std = sum(1 for r in results if r["has_v_std"])
    n_hv = sum(1 for r in results if r["has_hv"])
    n_hd = sum(1 for r in results if r["has_hd"])
    n_none = sum(
        1 for r in results
        if not r["has_v"] and not r["has_hv"] and not r["has_hd"] and not r["db_ok"]
    )
    no_hero = [r for r in results if not r["has_hv"]]

    for lst in (A, B, C, D, E):
        lst.sort(key=lambda x: x["title"].lower())

    now = time.strftime("%Y-%m-%d %H:%M:%S")

    # ---------- CSV ----------
    with open(CSVPATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "appid", "title", "db_image_type", "has_vertical", "has_hero", "has_header",
                "has_vertical_fastly", "has_hero_fastly", "vertical_bytes", "hero_bytes",
                "header_bytes", "verdict", "best_vertical_url",
            ]
        )
        for r in results:
            w.writerow(
                [
                    r["appid"], r["title"], r["db_type"],
                    "TRUE" if r["has_v"] else "FALSE",
                    "TRUE" if r["has_hv"] else "FALSE",
                    "TRUE" if r["has_hd"] else "FALSE",
                    "TRUE" if r["has_vf"] else "FALSE",
                    "TRUE" if r["has_hf"] else "FALSE",
                    r["v_bytes"], r["hv_bytes"], r["hd_bytes"], r["note"],
                    r["best_vertical_url"] or "",
                ]
            )

    # ---------- 报告 ----------
    L = []
    a = L.append
    a("=== 已通关游戏图片核查报告 ===")
    a("核查时间: %s" % now)
    a("总数: %d" % total)
    if bad:
        a("跳过（media_id 非纯数字）: %d  ->  %s" % (len(bad), ", ".join(str(b[0]) for b in bad)))
    a("")
    a("=== 汇总 ===")
    a("竖版存在（含官方资产清单里的 library_capsule）: %d 个" % n_v)
    a("  - 其中走标准老路径 library_600x900.jpg 就能拿到: %d 个" % n_v_std)
    a("  - 只能走官方清单带 hash 的竖版(library_capsule 等): %d 个" % (n_v - n_v_std))
    a("横版 library_hero 存在: %d 个" % n_hv)
    a("header 存在: %d 个" % n_hd)
    a("三类全没有: %d 个" % n_none)
    a("")
    a("=== 分类统计 ===")
    a("A. 库里存 header，但 Steam 有竖版 → 可修（%d 个）" % len(A))
    a("B. 库里存 header，Steam 也无竖版（只有 header）→ 不可修（%d 个）" % len(B))
    a("C. 库里存竖版，但竖版 URL 已 404 → 需要修（%d 个）" % len(C))
    a("D. 三类全没有 → 无图（%d 个）" % len(D))
    a("E. 库里存竖版且正常 → 无需处理（%d 个）" % len(E))
    a("")
    a("★ 重要：新版游戏的竖版不叫 library_600x900.jpg，Steam 改成了")
    a("  store_item_assets/steam/apps/<id>/<hash>/library_capsule.jpg")
    a("  实测像素 300x450（比例 0.667），和竖版封面完全等价，只是文件名变了。")
    a("  只拼 library_600x900.jpg 会把这批游戏误判成「没有竖版」。")
    a("")

    def section(title, lst, cols):
        a(title)
        if not lst:
            a("（无）")
            a("")
            return
        a(cols[0])
        for r in lst:
            a(cols[1](r))
        a("")
    _ = section
    # A 类：分两段输出，先列能用标准路径的，再列需要 hash 路径的
    a("=== A 类明细（可修，优先看）===")
    if not A:
        a("（无）")
        a("")
    else:
        a_std = [r for r in A if r["has_v_std"]]
        a_hash = [r for r in A if not r["has_v_std"]]
        a("-- A-1 标准路径就能拿到竖版（%d 个）:" % len(a_std))
        a("appid    游戏名                          库里存的   竖版  横版  建议")
        for r in a_std:
            a("%s %s %s %s  %s   换竖版" % (
                pad(r["appid"], 9), pad(r["title"][:26], 22), pad(r["db_type"], 10),
                mark(r["has_v"]), mark(r["has_hv"])))
        a("")
        a("-- A-2 只能用官方清单里的 hash 竖版（%d 个）:" % len(a_hash))
        a("appid    游戏名                          库里存的   竖版  横版  竖版URL（直接可用）")
        for r in a_hash:
            a("%s %s %s %s  %s   %s" % (
                pad(r["appid"], 9), pad(r["title"][:26], 22), pad(r["db_type"], 10),
                mark(r["has_v"]), mark(r["has_hv"]), r["best_vertical_url"] or ""))
        a("")

    a("=== B 类明细（不可修）===")
    if not B:
        a("（无）")
        a("")
    else:
        a("appid    游戏名                          库里存的   竖版  横版  header  库里URL可用")
        for r in B:
            a("%s %s %s %s  %s  %s    %s" % (
                pad(r["appid"], 9), pad(r["title"][:26], 22), pad(r["db_type"], 10),
                mark(r["has_v"]), mark(r["has_hv"]), mark(r["has_hd"]),
                "是" if r["db_ok"] else "否"))
        a("")

    a("=== C 类明细（图挂了）===")
    if not C:
        a("（无）")
        a("")
    else:
        a("appid    游戏名                          竖版HTTP  库里URL")
        for r in C:
            a("%s %s %s   %s" % (
                pad(r["appid"], 9), pad(r["title"][:26], 22), r["v_status"],
                (r["image"][:70] if r["image"] else "(空)")))
        a("")

    a("=== D 类明细（完全无图）===")
    if not D:
        a("（无）")
        a("")
    else:
        a("appid    游戏名                          库里存的   库里URL")
        for r in D:
            a("%s %s %s %s" % (
                pad(r["appid"], 9), pad(r["title"][:26], 22), pad(r["db_type"], 10),
                (r["image"][:70] if r["image"] else "(空)")))
        a("")

    a("=== 横版缺失（巨屏背景会 404）===")
    a("这些游戏没有 library_hero.jpg：%d 个" % len(no_hero))
    a("appid    游戏名                          库里有竖版")
    for r in no_hero:
        a("%s %s %s" % (pad(r["appid"], 9), pad(r["title"][:26], 22), mark(r["has_v"])))
    a("")

    a("=== 补充说明 ===")
    a("1) 探测过的 URL：")
    a("   media.steampowered.com/steam/apps/<id>/library_600x900.jpg")
    a("   media.steampowered.com/steam/apps/<id>/library_hero.jpg")
    a("   media.steampowered.com/steam/apps/<id>/header.jpg")
    a("   shared.fastly.steamstatic.com/store_item_assets/steam/apps/<id>/library_600x900.jpg")
    a("   shared.fastly.steamstatic.com/store_item_assets/steam/apps/<id>/library_hero.jpg")
    a("   + Steam 官方 IStoreBrowseService 资产清单里的 library_capsule / library_capsule_2x")
    a("2) 库里 image 存 http 的 %d 行，全部用 HEAD 直接验过（是否可用见 B/C/D 表的「库里URL」列）。"
      % sum(1 for r in results if r["image"].startswith("http")))
    a("3) 检查方式: HEAD 请求，并发 8，超时 10s，失败重试 2 次，UA=Mozilla/5.0。")
    a("4) 数据库以 mode=ro（只读）打开，本次核查未写入任何数据。")
    a("")
    a("耗时: %.1f 秒" % (time.time() - t0))

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")

    # ---------- stdout 摘要 ----------
    print("[DONE] %.1fs" % (time.time() - t0))
    print("A=%d B=%d C=%d D=%d E=%d  no_hero=%d" % (len(A), len(B), len(C), len(D), len(E), len(no_hero)))
    print("REPORT:", REPORT)
    print("CSV   :", CSVPATH, "rows=", len(results) + 1)
    print("---A---")
    for r in A[:20]:
        print(r["appid"], r["title"], "| v", r["v_bytes"], "hv", r["hv_bytes"], "hd", r["hd_bytes"])
    print("---B---")
    for r in B:
        print(r["appid"], r["title"], "| hv", r["hv_bytes"], "hd", r["hd_bytes"])
    print("---C---")
    for r in C:
        print(r["appid"], r["title"], r["v_status"], r["vf_status"])
    print("---D---")
    for r in D:
        print(r["appid"], r["title"], r["image"][:70])
    return 0


if __name__ == "__main__":
    sys.exit(main())
