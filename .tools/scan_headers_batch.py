# -*- coding: utf-8 -*-
"""P1：全库 header 游戏扫描（只出报告，绝不写库）。
- 查 media_type='game' AND image like '%header%' 的全部 appid
- 42 个一批调 IStoreBrowseService/GetItems/v1
- 只用 assets.library_capsule / library_600x900 字段值自行拼 URL
- 支持中断续跑：进度落 scan_headers_progress.json
- 产出 P1-header-scan.csv
"""
import csv, json, os, sqlite3, sys, time, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor

DB = r"C:\Users\USERNAME\Downloads\life joy\yamtrack\src\db\db.sqlite3"
PROG = r"C:\Users\USERNAME\Downloads\life joy\yamtrack\.tools\scan_headers_progress.json"
OUTCSV = r"C:\Users\USERNAME\Downloads\life joy\ui-redesign\shots\impl\P1-header-scan.csv"
UA = {"User-Agent": "Mozilla/5.0"}
API = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?input_json="
BASE = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/%s/%s"
BATCH = 42


def load_db_rows():
    c = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    rows = c.execute("""SELECT DISTINCT media_id, title, image FROM app_item
                        WHERE media_type='game' AND image LIKE '%header%' ORDER BY CAST(media_id AS INTEGER)""").fetchall()
    c.close()
    return [{"appid": str(m), "title": t, "db_image": img} for m, t, img in rows]


def load_prog():
    if os.path.exists(PROG):
        try:
            return json.load(open(PROG, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_prog(p):
    tmp = PROG + ".tmp"
    json.dump(p, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, PROG)


def fetch_batch(appids):
    body = {"ids": [{"appid": int(a)} for a in appids],
            "context": {"language": "schinese", "country_code": "CN", "steam_realm": 1},
            "data_request": {"include_assets": True}}
    url = API + urllib.parse.quote(json.dumps(body))
    for proxy in (None, "http://127.0.0.1:7897"):
        try:
            op = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
            with op.open(urllib.request.Request(url, headers=UA), timeout=35) as r:
                return json.load(r)["response"]["store_items"], None
        except Exception as e:
            err = repr(e)[:160]
    return None, err


def head_one(u):
    for proxy in (None, "http://127.0.0.1:7897"):
        try:
            op = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
            with op.open(urllib.request.Request(u, method="HEAD", headers=UA), timeout=20) as r:
                return r.status, r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except Exception as e:
            e2 = e
    return 0, repr(e2)[:80]


def main():
    rows = load_db_rows()
    print("header 游戏条数:", len(rows))
    prog = load_prog()
    print("已有进度:", len(prog))

    todo = [r for r in rows if r["appid"] not in prog]
    print("待处理:", len(todo))

    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        ids = [r["appid"] for r in chunk]
        items, err = fetch_batch(ids)
        if items is None:
            # 请求失败：不写进度，下轮重试；记录失败原因
            for r in chunk:
                prog[r["appid"]] = dict(r, new_vertical_url="", verdict="请求失败", note=err)
            print("[batch %d] 失败 %s %s" % (i // BATCH + 1, err, ids[:3]))
        else:
            got = {}
            for x in items:
                a = x.get("assets") or {}
                fn = a.get("library_capsule") or a.get("library_600x900") or ""
                got[str(x.get("appid"))] = (BASE % (x.get("appid"), fn)) if fn else ""
            for r in chunk:
                nu = got.get(r["appid"], "")
                prog[r["appid"]] = dict(r, new_vertical_url=nu,
                                        verdict="可用" if nu else "Steam无图", head_status="")
        save_prog(prog)
        print("[batch %d/%d] done, 进度 %d" % (i // BATCH + 1, (len(todo) + BATCH - 1) // BATCH, len(prog)))
        time.sleep(0.6)

    # HEAD 全验（只验有 URL 的）
    withurl = [a for a, v in prog.items() if v.get("new_vertical_url")]
    need = [a for a in withurl if not prog[a].get("head_status")]
    print("有竖版URL:", len(withurl), " 待HEAD:", len(need))
    if need:
        urls = [prog[a]["new_vertical_url"] for a in need]
        with ThreadPoolExecutor(max_workers=16) as ex:
            res = list(ex.map(head_one, urls))
        for a, (st, ct) in zip(need, res):
            prog[a]["head_status"] = st
            prog[a]["ctype"] = ct
        save_prog(prog)
        print("HEAD 完成")

    # 出 CSV
    out = []
    for r in rows:
        v = prog.get(r["appid"], {})
        st = v.get("head_status", "")
        if v.get("verdict") == "请求失败":
            verdict = "请求失败"
        elif not v.get("new_vertical_url"):
            verdict = "Steam无图"
        elif st == 200:
            verdict = "可用"
        else:
            verdict = "Steam无图" if st in (403, 404) else ("请求失败" if st == 0 else "Steam无图")
        out.append([r["appid"], r["title"], r["db_image"], v.get("new_vertical_url", ""), st, verdict])
    with open(OUTCSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["appid", "title", "db_image", "new_vertical_url", "head_status", "verdict"])
        w.writerows(out)
    from collections import Counter
    print("verdict:", Counter(x[5] for x in out))
    print("head:", Counter(str(x[4]) for x in out))
    print("CSV:", OUTCSV, os.path.getsize(OUTCSV), "bytes")


if __name__ == "__main__":
    main()