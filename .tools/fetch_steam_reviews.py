# -*- coding: utf-8 -*-
"""回填 app_game.steam_review_pct —— Steam 游戏好评率（并发版）。

数据源：Steam 官方 appreviewhistogram（匿名 JSON，无需 token）
    https://store.steampowered.com/appreviewhistogram/<appid>?l=english&version=3

    ── 为什么不用别的两个方案（都实测过，都有坑）──
    A. `/appreviews/<appid>?json=1`：对无 cookie 请求一律 302 踢回首页。
       带 UA/Referer/cookie/cc/各种参数、换 curl / PowerShell / requests 全试过，
       全部 302。带 token / key 也一样 302。此路不通。
    B. 商店页 HTML 的 "N% of the N user reviews" 正则：
       ① 这个正则命中的是「**最近 30 天**」那一行（`in the last 30 days`），
          不是总评 —— 巫师3 命中 81%，真实总评 95%，差 14 个点。
       ② 就算取到 "All Reviews" 那一行，Steam 也会按**页面语言**过滤：
          中文页显示 192,944 条，英文页显示 248,293 条，都不是全语言总数。
       → 用它会把整库好评率算偏，所以弃用。
    C. appreviewhistogram（本脚本采用）：返回**全语言**按周/按月汇总的
       好评数(up)与差评数(down)，巫师3 汇总 92 万条，明显是全语言口径。

好评率算法：
    pct = round(total_positive / (total_positive + total_negative) * 100)
    分母只用 up+down，**不用 total_reviews**（口径不同会偏）。

数据结构坑（已实测）：
    results.rollups  按月，覆盖 [上线, 当月]
    results.recent   按日，最近 30 天，**与最后 rollup 那个月完全重复**
    → 直接用 rollups 全量；recent 只补「晚于 rollups 最后月份」的日数据，避免重复计数。

评论数门槛（用户 2026-10-24 拍板）：
    **总数 < 100 的一律不写**，steam_review_pct 留 NULL。
    理由：1 个好评 = 100%，样本太小不可信。后续按
    「pct IS NULL + 没玩过 → 隐藏」落地，无需另存评论数字段。

安全约束（硬性）：
    * 只 UPDATE app_game.steam_review_pct / status，WHERE 条件限定「原来为 NULL」。
    * 绝不碰 score（用户个人评分）、不碰 app_item、不碰 users_user、不碰其他表。
    * 不清空、不删除任何数据。

用法：
    ..\\.venv\\Scripts\\python.exe .tools\\fetch_steam_reviews.py --limit 20
    ..\\.venv\\Scripts\\python.exe .tools\\fetch_steam_reviews.py            # 剩余全部
    ..\\.venv\\Scripts\\python.exe .tools\\fetch_steam_reviews.py --workers 16 --batch 250
    ..\\.venv\\Scripts\\python.exe .tools\\fetch_steam_reviews.py --recompute-nulls
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "src" / "db" / "db.sqlite3"
LOG = ROOT / ".tools" / "logs" / "steam_reviews.log"
FAILED = ROOT / ".tools" / "raw" / "steam_reviews_failed.json"
CACHE = ROOT / ".tools" / "raw" / "steam_reviews_count.json"

HIST = "https://store.steampowered.com/appreviewhistogram/{appid}?l=english&version=3"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
PROXY = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}

MIN_REVIEWS = 100         # 评论数门槛（用户拍板，2026-10-24）
BACKOFF = (3, 10, 30)     # 单条重试退避
COMMIT_EVERY = 50         # 主线程每收集 N 条提交一次

_log_lock = threading.Lock()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str, echo: bool = True) -> None:
    line = f"[{now()}] {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with _log_lock:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if echo:
            print(msg, flush=True)


def month_key(ts: int) -> tuple[int, int]:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return (d.year, d.month)


def compute(payload: dict) -> tuple[int | None, int, int, str]:
    """返回 (好评率或None, 好评数, 差评数, 状态说明)。"""
    if payload.get("success") != 1:
        return None, 0, 0, "success!=1"
    res = payload.get("results") or {}
    rollups = res.get("rollups") or []
    recent = res.get("recent") or []

    up = dn = 0
    if rollups:
        for b in rollups:
            up += int(b.get("recommendations_up") or 0)
            dn += int(b.get("recommendations_down") or 0)
        last = month_key(int(rollups[-1]["date"]))
        for b in recent:
            if month_key(int(b["date"])) > last:
                up += int(b.get("recommendations_up") or 0)
                dn += int(b.get("recommendations_down") or 0)
    elif recent:
        for b in recent:
            up += int(b.get("recommendations_up") or 0)
            dn += int(b.get("recommendations_down") or 0)

    total = up + dn
    if total == 0:
        return None, 0, 0, "无评论数据"
    if total < MIN_REVIEWS:
        return None, up, dn, f"评论数{total}<{MIN_REVIEWS}"
    return round(up / total * 100), up, dn, "ok"


_thread_local = threading.local()


def session() -> requests.Session:
    """每个线程独立 Session —— 共用会串 cookie，实测会开始返回 302。"""
    s = getattr(_thread_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(
            {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
        )
        _thread_local.s = s
    return s


def fetch_one(appid: str, proxies) -> tuple[str, dict | None, str]:
    url = HIST.format(appid=appid)
    last = ""
    for attempt in range(len(BACKOFF) + 1):
        try:
            r = session().get(
                url, timeout=25, proxies=proxies, allow_redirects=False
            )
            if r.status_code == 200:
                ctype = (r.headers.get("content-type") or "").lower()
                if "json" not in ctype:
                    last = f"非JSON({ctype[:24]})"
                else:
                    return appid, r.json(), ""
            else:
                last = f"HTTP{r.status_code}"
        except requests.RequestException as exc:
            last = type(exc).__name__
        if attempt < len(BACKOFF):
            time.sleep(BACKOFF[attempt])
    return appid, None, last


def proxy_alive() -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 7897), timeout=2):
            return True
    except OSError:
        return False


def load_json(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
    return {}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def recompute_nulls(counts: dict) -> tuple[int, list]:
    """把评论数 < MIN_REVIEWS 的已写值置回 NULL（门槛从 50 提到 100 后需要）。"""
    rw = sqlite3.connect(DB, timeout=60)
    rows = rw.execute(
        "SELECT g.id, i.media_id FROM app_game g JOIN app_item i ON i.id=g.item_id "
        "WHERE g.steam_review_pct IS NOT NULL"
    ).fetchall()
    demote = []
    for gid, appid in rows:
        rec = counts.get(str(appid)) or {}
        total = int(rec.get("total") or 0)
        if total and total < MIN_REVIEWS:
            demote.append(gid)
    if demote:
        rw.executemany(
            "UPDATE app_game SET steam_review_pct=NULL WHERE id=? AND steam_review_pct IS NOT NULL",
            [(g,) for g in demote],
        )
        rw.commit()
    rw.close()
    return len(demote), []


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--batch", type=int, default=250)
    ap.add_argument("--pause", type=float, default=5.0, help="批间停顿秒")
    ap.add_argument("--proxy", choices=["auto", "on", "off"], default="auto")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--recompute-nulls", action="store_true",
                    help="按评论数门槛把不合格的已写值置回 NULL")
    args = ap.parse_args()

    counts = load_json(CACHE)

    if args.recompute_nulls:
        log("=" * 70)
        n, _ = recompute_nulls(counts)
        log(f"recompute-nulls：{n} 条因评论数<{MIN_REVIEWS} 被置回 NULL")
        print(f"置回 NULL: {n} 条")
        return 0

    proxies = None
    if args.proxy == "on":
        proxies = PROXY
    elif args.proxy == "auto":
        proxies = PROXY if proxy_alive() else None

    log("=" * 70)
    log(
        f"启动(并发) limit={args.limit or '全量'} workers={args.workers} "
        f"batch={args.batch} proxy={'on' if proxies else 'off'} "
        f"门槛={MIN_REVIEWS} dry_run={args.dry_run}"
    )

    ro = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    rows = ro.execute(
        """
        SELECT g.id, i.media_id, i.title
        FROM app_game g JOIN app_item i ON i.id = g.item_id
        WHERE g.steam_review_pct IS NULL
        ORDER BY g.id
        """
    ).fetchall()
    total_rows, already = ro.execute(
        "SELECT COUNT(*), SUM(steam_review_pct IS NOT NULL) FROM app_game"
    ).fetchone()
    ro.close()

    log(f"app_game 共 {total_rows} 条，已有 {already or 0} 条，待处理 {len(rows)} 条")
    if args.limit:
        rows = rows[: args.limit]
        log(f"--limit 生效，本次只跑 {len(rows)} 条")
    if not rows:
        log("没有待处理条目。")
        return 0

    rw = sqlite3.connect(DB, timeout=120)
    failed = load_json(FAILED)

    ok = bad = skipped = done = 0
    pending: list[tuple[int, int]] = []
    started = time.time()

    for start in range(0, len(rows), args.batch):
        chunk = rows[start:start + args.batch]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(fetch_one, str(a), proxies): (g, a, t)
                       for g, a, t in chunk}
            for fut in as_completed(futures):
                gid, appid, title = futures[fut]
                _aid, payload, err = fut.result()
                done += 1

                if payload is None:
                    bad += 1
                    failed[str(appid)] = {"title": title, "reason": err or "unknown",
                                          "id": gid}
                    log(f"  {appid} {title[:36]} 失败: {err}", echo=False)
                else:
                    pct, up, dn, status = compute(payload)
                    counts[str(appid)] = {"title": title, "pct": pct,
                                          "up": up, "down": dn, "total": up + dn,
                                          "status": status}
                    if pct is None:
                        skipped += 1
                        log(f"  {appid} {title[:36]} 跳过: {status}", echo=False)
                    else:
                        pending.append((pct, gid))
                        ok += 1
                        if args.limit and args.limit <= 30:
                            log(f"  {appid} {title[:34]} {pct}% ({up:,}/{up+dn:,})")

                if len(pending) >= COMMIT_EVERY and not args.dry_run:
                    rw.executemany(
                        "UPDATE app_game SET steam_review_pct=? "
                        "WHERE id=? AND steam_review_pct IS NULL",
                        pending,
                    )
                    rw.commit()
                    pending.clear()

        elapsed = time.time() - started
        per = elapsed / done if done else 0
        left = len(rows) - done
        log(
            f"已完成 {done}/{len(rows)}，成功 {ok}，失败 {bad}，跳过 {skipped}，"
            f"{per:.2f}s/条，剩余约 {per * left / 60:.1f} 分钟"
        )
        if not args.dry_run:
            save_json(CACHE, counts)
            save_json(FAILED, failed)
        time.sleep(args.pause)

    if pending and not args.dry_run:
        rw.executemany(
            "UPDATE app_game SET steam_review_pct=? "
            "WHERE id=? AND steam_review_pct IS NULL",
            pending,
        )
        rw.commit()
    rw.close()
    save_json(CACHE, counts)
    save_json(FAILED, failed)

    elapsed = time.time() - started
    log(
        f"结束：处理 {done}，写入 {ok}，失败 {bad}，跳过 {skipped}，"
        f"耗时 {elapsed / 60:.1f} 分钟"
    )
    print(
        f"\n本轮：处理 {done}，写入 {ok}，失败 {bad}，跳过 {skipped}，"
        f"耗时 {elapsed / 60:.1f} 分钟"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
