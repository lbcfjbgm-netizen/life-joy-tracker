# -*- coding: utf-8 -*-
"""阶段1探针：只读抽样，测 Steam GetPlayerAchievements 能补回多少成就数据。

绝对只读：
  - SQLite 用 URI mode=ro 打开
  - 额外 PRAGMA query_only=1
  - 启动时做一次「写操作必须失败」的自检（失败即证明只读；若意外成功立刻 rollback）

不打印、不落盘任何 API key / STEAM_ID 明文，报告里只出现长度和掩码。
"""

import hashlib
import io
import json
import os
import random
import sqlite3
import sys
import time
import unicodedata

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = r"C:\Users\USERNAME\Downloads\life joy\yamtrack"
DB = os.path.join(ROOT, "src", "db", "db.sqlite3")
ENV = os.path.join(ROOT, ".env")
REPORT = r"C:\Users\USERNAME\Downloads\life joy\ui-redesign\shots\impl\M-report.txt"
RAW_OUT = os.path.join(ROOT, ".tools", "probe_results.json")

STEAM_URL = "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/"
SCHEMA_URL = "https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
PROXY = "http://127.0.0.1:7897"
INTERVAL = 0.5
FORCE_APPID = "2592160"   # Dispatch 超英派遣中心
SAMPLE_N = 25
SEED = 20261024


# --------------------------------------------------------------------------- #
# 配置读取（不打印值）
# --------------------------------------------------------------------------- #
def read_env(path):
    d = {}
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            d[k] = v
    return d


def mask(v):
    """只给出长度和首尾各 2 个字符，绝不泄漏全文。"""
    if not v:
        return "<空>"
    if len(v) <= 6:
        return "*" * len(v) + f"(len={len(v)})"
    return f"{v[:2]}***{v[-2:]}(len={len(v)})"


# --------------------------------------------------------------------------- #
# 只读连库
# --------------------------------------------------------------------------- #
def open_ro(db_path):
    uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.execute("PRAGMA query_only = 1")
    return con


def readonly_selfcheck(con):
    """尝试 UPDATE 一条，期望抛 readonly 错误；无论结果都 rollback。"""
    ok = False
    msg = ""
    try:
        con.execute("BEGIN")
        con.execute("UPDATE app_game SET achievements_total = achievements_total WHERE id = -1")
        msg = "!! 写入竟然成功了（已立即 rollback，数据未变）"
    except sqlite3.Error as e:
        ok = True
        msg = f"UPDATE 被拒绝：{e}"
    finally:
        try:
            con.rollback()
        except sqlite3.Error:
            pass
    return ok, msg


# --------------------------------------------------------------------------- #
# 取样本
# --------------------------------------------------------------------------- #
def load_pool(con):
    sql = """
        SELECT g.id, g.item_id, i.media_id, i.title, i.title_zh,
               g.achievements_total, g.achievements_unlocked,
               g.achievements_synced_at, g.notes
        FROM app_game g JOIN app_item i ON i.id = g.item_id
        WHERE i.source = 'steam' AND i.media_id GLOB '[0-9]*'
    """
    rows = []
    for r in con.execute(sql):
        (gid, item_id, media_id, title, title_zh,
         tot, unl, synced, notes) = r
        rows.append({
            "game_id": gid,
            "item_id": item_id,
            "appid": str(media_id).strip(),
            "title": (title_zh or title or "").strip() or f"<{media_id}>",
            "title_en": (title or "").strip(),
            "db_total": tot or 0,
            "db_unlocked": unl or 0,
            "synced_at": synced,
            "notes": (notes or "").strip(),
            "family": "家庭共享" in (notes or ""),
        })
    return rows


def pick(pool, n, seed_off, forced=()):
    """确定性抽样，并保证 forced 里的 appid 一定入选（顶掉某个随机位）。"""
    pool = sorted(pool, key=lambda x: x["appid"])
    rnd = random.Random(SEED + seed_off)
    chosen = rnd.sample(pool, min(n, len(pool)))
    have = {c["appid"] for c in chosen}
    for ap in forced:
        if ap in have:
            continue
        hit = next((p for p in pool if p["appid"] == ap), None)
        if hit is None:
            continue
        chosen[-1] = hit          # 直接顶掉最后一个随机位，保证 n 不变
        have.add(ap)
    return chosen


def probe_schema(key, appid):
    """GetSchemaForGame：不依赖用户数据，返回该游戏「一共有几个成就」。
    用来判断「拿不到 0」到底是游戏没成就，还是 API 不肯给。"""
    for attempt in range(3):
        px = {"http": PROXY, "https": PROXY} if attempt else None
        try:
            r = requests.get(SCHEMA_URL,
                             params={"key": key, "appid": appid, "l": "schinese"},
                             headers={"User-Agent": UA}, timeout=(6, 20),
                             proxies=px)
            if r.status_code != 200:
                return None
            j = r.json()
            g = j.get("game") or {}
            ach = (g.get("availableGameStats") or {}).get("achievements") or []
            return len(ach)
        except Exception:
            time.sleep(1.0)
    return None


# --------------------------------------------------------------------------- #
# Steam 请求
# --------------------------------------------------------------------------- #
class Client:
    def __init__(self, key, steamid):
        self.key = key
        self.steamid = steamid
        self.sess = requests.Session()
        self.sess.headers.update({"User-Agent": UA})
        self.proxies = None
        self.used_proxy = False
        self.net_note = ""

    def _get(self, appid):
        params = {
            "key": self.key,
            "steamid": self.steamid,
            "appid": appid,
            "l": "schinese",
        }
        return self.sess.get(STEAM_URL, params=params,
                             timeout=(6, 25), proxies=self.proxies)

    def fetch(self, appid):
        """返回 dict: http / ach_n / unlocked / error / raw_state
        前两次走直连，后两次走本机代理；429 单独退避。"""
        last_exc = None
        for attempt in range(4):
            use_proxy = attempt >= 2
            px = {"http": PROXY, "https": PROXY} if use_proxy else None
            try:
                params = {"key": self.key, "steamid": self.steamid,
                          "appid": appid, "l": "schinese"}
                r = self.sess.get(STEAM_URL, params=params,
                                  timeout=(6, 25), proxies=px)
            except requests.RequestException as e:
                last_exc = e
                if use_proxy:
                    self.used_proxy = True
                time.sleep(1.0 + attempt)
                continue

            code = r.status_code
            if code == 429:
                if attempt < 3:
                    time.sleep(5 * (2 ** attempt))
                    continue
                return {"http": 429, "ach_n": None, "unlocked": None,
                        "error": "429 限流，重试 3 次仍失败"}

            body = None
            try:
                body = r.json()
            except ValueError:
                body = None

            err = ""
            ach_n = unlocked = None
            if isinstance(body, dict):
                ps = body.get("playerstats") or {}
                if isinstance(ps, dict):
                    err = ps.get("error") or ""
                    ach = ps.get("achievements")
                    if isinstance(ach, list):
                        ach_n = len(ach)
                        unlocked = sum(1 for a in ach
                                       if isinstance(a, dict) and a.get("achieved") == 1)
            if not err and code != 200:
                err = (r.text or "")[:160].replace("\n", " ").strip()

            return {"http": code, "ach_n": ach_n, "unlocked": unlocked,
                    "error": err or ""}

        return {"http": None, "ach_n": None, "unlocked": None,
                "error": f"网络异常(可重试)：{type(last_exc).__name__ if last_exc else 'unknown'}"}


# --------------------------------------------------------------------------- #
# 报告排版
# --------------------------------------------------------------------------- #
def dwidth(s):
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def cut(s, width):
    out, w = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width:
            out += "…"
            break
        out += ch
        w += cw
    return out


def pad(s, width):
    return s + " " * max(0, width - dwidth(s))


# --------------------------------------------------------------------------- #
def main():
    t0 = time.time()
    lines = []

    def out(s=""):
        print(s, flush=True)
        lines.append(s)

    out("=== 成就补数探针（阶段1 · 只读）===")
    out(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # 配置
    if not os.path.exists(ENV):
        out(f"!! 找不到 .env: {ENV}")
        return 2
    env = read_env(ENV)
    key = env.get("STEAM_API_KEY", "").strip()
    steamid = env.get("STEAM_ID", "").strip()
    if not key or not steamid:
        out("!! .env 里缺 STEAM_API_KEY 或 STEAM_ID，终止")
        return 2
    out(f"凭据: STEAM_API_KEY={mask(key)}  STEAM_ID={mask(steamid)}")
    out(f"数据库: {DB}")
    out(f"库文件大小: {os.path.getsize(DB):,} 字节")

    # 连库
    con = open_ro(DB)
    ok, msg = readonly_selfcheck(con)
    out(f"只读自检: {'通过' if ok else '异常'} — {msg}")
    if not ok:
        con.close()
        out("!! 只读自检未通过，为保证不写库，终止")
        return 3

    pool = load_pool(con)
    a_pool = [p for p in pool if p["synced_at"] is not None and p["db_total"] == 0]
    b_pool = [p for p in pool if p["synced_at"] is None]

    out("")
    out("--- 全库分层（steam 条目）---")
    out(f"steam 游戏总数           : {len(pool)}")
    out(f"已同步且 total>0         : {len(pool) - len(a_pool) - len(b_pool)}")
    out(f"A 组 已同步但 total=0    : {len(a_pool)}")
    out(f"B 组 从未同步            : {len(b_pool)}")
    out(f"A 组里家庭共享           : {sum(1 for p in a_pool if p['family'])}")
    out(f"B 组里家庭共享           : {sum(1 for p in b_pool if p['family'])}")

    # Dispatch 在库里是「从未同步」，属于 B 组；按实际所在组做担保，
    # 避免「在 A 组找 B 组的东西」这种乌龙
    disp_in_a = any(p["appid"] == FORCE_APPID for p in a_pool)
    a = pick(a_pool, SAMPLE_N, 1, forced=[FORCE_APPID] if disp_in_a else [])
    b = pick(b_pool, SAMPLE_N, 2, forced=[] if disp_in_a else [FORCE_APPID])
    con.close()

    # 断点复用：上一次已经跑出结论的分组不重复请求，避免反复烧网络
    results = {"A": [], "B": []}
    reuse = set()
    if os.path.exists(RAW_OUT) and not os.environ.get("PROBE_FRESH"):
        try:
            with open(RAW_OUT, "r", encoding="utf-8") as f:
                prev = json.load(f)
            for tag, group in (("A", a), ("B", b)):
                want = {r["appid"] for r in group}
                got = {r["appid"]: r for r in (prev.get(tag) or [])}
                if want and want <= set(got):
                    results[tag] = [got[x] for x in want]
                    reuse.add(tag)
            if reuse:
                out("")
                out(f"复用上次结果: {sorted(reuse)}（想强制重跑就设环境变量 PROBE_FRESH=1）")
        except Exception:
            results = {"A": [], "B": []}

    client = Client(key, steamid)

    skip_note = []
    for gi, (tag, group) in enumerate((("A", a), ("B", b)), start=1):
        if tag in reuse:
            continue
        out("")
        out(f"--- {tag} 组请求中（{len(group)} 个，间隔 {INTERVAL}s 串行）---")
        for i, row in enumerate(group, start=1):
            if not row["appid"].isdigit():
                skip_note.append(f"{tag}: appid 非数字，跳过 {row['appid']!r}")
                continue
            res = client.fetch(row["appid"])
            rec = dict(row)
            rec.update(res)
            rec["group"] = tag
            # 交叉验证：官方说这个游戏共有几个成就（不依赖用户数据）
            # 复用间隔，不额外空转
            rec["schema_n"] = probe_schema(key, row["appid"])
            results[tag].append(rec)
            line = (f"[{tag} {i}/{len(group)}] {row['appid']:>9} "
                    f"{cut(row['title'], 22)} http={rec['http']} "
                    f"ach={rec['ach_n']} unl={rec['unlocked']} "
                    f"schema={rec['schema_n']} err={rec['error'][:50]}")
            print(line, flush=True)
            time.sleep(INTERVAL)

    # ------------------------------ 报告 ------------------------------ #
    out("")
    out("")
    out("################  M-report  ################")
    out(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    out(f"凭据掩码: STEAM_API_KEY={mask(key)}  STEAM_ID={mask(steamid)}")
    out(f"网络: {'走了本机代理(直连失败后切换)' if client.used_proxy else '直连'}  {client.net_note}")
    out("写库防护: sqlite3 URI mode=ro + PRAGMA query_only=1")
    out("")

    summary = {}
    for tag, label in (("A", "A 组：已同步但 total=0"),
                       ("B", "B 组：从未同步")):
        rows = results[tag]
        head = f"=== {label}（{len(rows)} 个）==="
        out(head)
        out(pad("appid", 9) + " " + pad("标题", 28) + " " +
            pad("HTTP", 6) + " " + pad("achieved", 9) + " " +
            pad("unlocked", 9) + " " + pad("官方成就数", 12) + " 错误信息")
        out("-" * 116)
        for r in rows:
            http = "-" if r["http"] is None else str(r["http"])
            ach = "-" if r["ach_n"] is None else str(r["ach_n"])
            unl = "-" if r["unlocked"] is None else str(r["unlocked"])
            sn = "-" if r.get("schema_n") is None else str(r["schema_n"])
            err = (r["error"] or "-").replace("\n", " ")
            out(pad(r["appid"], 9) + " " + pad(cut(r["title"], 28), 28) + " " +
                pad(http, 6) + " " + pad(ach, 9) + " " + pad(unl, 9) + " " +
                pad(sn, 12) + " " + cut(err, 36))
        out("")

        c200 = sum(1 for r in rows if r["http"] == 200)
        c403 = sum(1 for r in rows if r["http"] == 403)
        c400 = sum(1 for r in rows if r["http"] == 400)
        c404 = sum(1 for r in rows if r["http"] == 404)
        c429 = sum(1 for r in rows if r["http"] == 429)
        c5xx = sum(1 for r in rows if r["http"] and 500 <= r["http"] < 600)
        cnet = sum(1 for r in rows if r["http"] is None)
        cother = len(rows) - c200 - c403 - c400 - c404 - c429 - c5xx - cnet
        real = sum(1 for r in rows if r["http"] == 200 and (r["ach_n"] or 0) > 0)
        fam_real = sum(1 for r in rows
                       if r["http"] == 200 and (r["ach_n"] or 0) > 0 and r["family"])

        pct = (real / len(rows) * 100) if rows else 0
        has_ach = sum(1 for r in rows if (r.get("schema_n") or 0) > 0)
        blocked_with_ach = sum(1 for r in rows
                               if r["http"] != 200 and (r.get("schema_n") or 0) > 0)
        out(f"汇总 {tag}: 200={c200} 403={c403} 400={c400} 404={c404} "
            f"429={c429} 5xx={c5xx} 网络失败={cnet} 其他={cother}"
            f"  → 真有成就(200 且 achievements>0) = {real} 个（{pct:.0f}%）")
        out(f"      交叉验证: 官方 schema 显示「这游戏有成就」= {has_ach} 个，"
            f"其中被挡在 API 外拿不到的 = {blocked_with_ach} 个")
        if fam_real:
            out(f"      其中属于家庭共享条目: {fam_real} 个")
        errs = [r["error"] for r in rows if r["error"] and r["error"] != "-"]
        if errs:
            from collections import Counter
            out("      错误信息 Top:")
            for txt, n in Counter(errs).most_common(5):
                out(f"        {n:>2}× {cut(txt, 80)}")
        out("")
        summary[tag] = {"n": len(rows), "c200": c200, "c403": c403,
                        "c400": c400, "c404": c404, "c429": c429,
                        "c5xx": c5xx, "cnet": cnet, "cother": cother,
                        "real": real}

    # Dispatch 单列
    disp = next((r for r in results["A"] + results["B"]
                 if r["appid"] == FORCE_APPID), None)
    out(f"Dispatch({FORCE_APPID} 超英派遣中心) 单独结果:")
    if disp:
        out(f"  所在组={disp.get('group','A')}  HTTP={disp['http']}  "
            f"achievements={disp['ach_n']}  unlocked={disp['unlocked']}  "
            f"官方成就数(schema)={disp.get('schema_n')}  err={disp['error'] or '-'}")
        out(f"  归属: {disp.get('notes') or '<空>'}   "
            f"在本人 Steam 库中: {'是' if disp.get('owned') else '否/未知'}")
    else:
        out("  未包含在样本中（库里可能没这个 appid）")

    a_total, b_total = len(a_pool), len(b_pool)
    sa, sb = summary["A"], summary["B"]
    a_rate = sa["real"] / sa["n"] if sa["n"] else 0
    b_rate = sb["real"] / sb["n"] if sb["n"] else 0
    a_proj, b_proj = round(a_total * a_rate), round(b_total * b_rate)
    n_req = a_total + b_total
    # 每个游戏 = 1 次玩家成就查询 + 1 次 schema 交叉验证，各带 0.5s 间隔
    minutes = n_req * (INTERVAL * 2 + 0.7) / 60
    blocked_total = (round(a_total * (sa["c403"] + sa["c400"] + sa["c404"]) / sa["n"])
                     if sa["n"] else 0) + \
                    (round(b_total * (sb["c403"] + sb["c400"] + sb["c404"]) / sb["n"])
                     if sb["n"] else 0)

    out("")
    out("=== 结论 ===")
    if a_rate == 0:
        out(f"A 组成功率 0%（{sa['real']}/{sa['n']}）→ {a_total} 个里"
            f"一个都补不回来，纯跑一遍只会把 403 标记写进去")
        out(f"    重要：A 组样本里有 {sum(1 for r in results['A'] if (r.get('schema_n') or 0) > 0)} 个"
            f"「游戏本身有成就、但 Steam 对当前账号返回 403」。"
            f"这是账号/接口的权限墙，不是你漏做了什么，换脚本也拿不到。")
    else:
        out(f"A 组成功率 {a_rate*100:.0f}%（{sa['real']}/{sa['n']}）→ "
            f"{a_total} 个预计能补回约 {a_proj} 个")
    out(f"B 组成功率 {b_rate*100:.0f}%（{sb['real']}/{sb['n']}）→ "
        f"{b_total} 个预计能补回约 {b_proj} 个")
    out(f"合计预计能补回约 {a_proj + b_proj} 个（{a_total + b_total} 个待处理）")
    out(f"预计总耗时：约 {minutes:.0f} 分钟"
        f"（{n_req} 个游戏 × (玩家成就 + schema) 两次请求）")
    out(f"其中 403/400/404 这类「Steam 明确不给」的约 {blocked_total} 个，只能标记已尝试")
    if skip_note:
        out("跳过: " + "; ".join(skip_note))
    out(f"本次实际耗时: {time.time()-t0:.1f} 秒")

    # 落盘
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(RAW_OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1, default=str)

    print(f"\n报告已写入: {REPORT}")
    print(f"原始结果: {RAW_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
