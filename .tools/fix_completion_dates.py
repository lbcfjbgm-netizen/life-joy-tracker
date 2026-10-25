# -*- coding: utf-8 -*-
"""用 Steam 成就的最后解锁时间，修正游戏的完成日期（end_date）。

规则（用户 2026-10-24 拍板）：
    「全成就时间和完成最后一个成就的时间一样，也就是通关时间」
    → 某游戏的全部成就都解锁时，end_date = 最后一个成就的解锁时间戳。
    → 同时 completion_estimated = 0（这是真实时间，不再是推算的）
    → status = 'Completed'（本来就是就不动）

安全边界（硬性）：
    * 只写 app_game 的 end_date / completion_estimated / status 三个字段。
    * 绝不碰 score / progress / notes / achievements_* / is_all_achievements
      / steam_review_pct / app_item / users_user / app_aicopy / app_highlight / app_note。
    * 不删任何数据。

数据源：ISteamUserStats/GetPlayerAchievements/v1（走代理 http://127.0.0.1:7897）
    * 没全解锁 → 不动
    * 全解锁但所有 unlocktime 都是 0（统计数据型成就）→ 跳过并记入失败清单
    * 一个成就都没解锁 → 跳过

数据库里的 end_date 是 UTC 朴素时间（Django USE_TZ=True 的存法），
所以时间戳要按 UTC 写入；页面上会按 Asia/Shanghai 显示。

用法：
    ..\\.venv\\Scripts\\python.exe .tools\\fix_completion_dates.py --limit 20 --dry-run
    ..\\.venv\\Scripts\\python.exe .tools\\fix_completion_dates.py --limit 20
    ..\\.venv\\Scripts\\python.exe .tools\\fix_completion_dates.py
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "src" / "db" / "db.sqlite3"
ENV = ROOT / ".env"
OUT_RAW = ROOT / ".tools" / "raw"
FAILED_JSON = OUT_RAW / "achievement_dates_failed.json"
CHANGES_JSON = OUT_RAW / "achievement_dates_changes.json"

API_URL = "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/"
PROXY = "http://127.0.0.1:7897"
PROXIES = {"http": PROXY, "https": PROXY}
DELAY = 0.5           # 每次请求之间的间隔（秒）
MAX_RETRIES = 3
RETRY_BASE = 5        # 429 退避：5 / 10 / 20 秒
COMMIT_EVERY = 100
LOCAL_TZ = timezone(timedelta(hours=8))  # Asia/Shanghai，无夏令时

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def read_env() -> tuple[str, str]:
    text = ENV.read_text(encoding="utf-8-sig")
    key = re.search(r"^STEAM_API_KEY=(\S+)", text, re.MULTILINE)
    sid = re.search(r"^STEAM_ID=(\d+)", text, re.MULTILINE)
    if not key or not sid:
        raise SystemExit("找不到 STEAM_API_KEY / STEAM_ID")
    return key.group(1).strip(), sid.group(1).strip()


def fetch_achievements(api_key: str, steam_id: str, appid: str):
    """返回 (achievements 列表 | None, 错误说明)。

    429 会退避重试；403/400 视为该游戏不开放成就，直接记为跳过。
    """
    params = {"key": api_key, "steamid": steam_id, "appid": appid, "l": "schinese"}
    last_err = ""
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(API_URL, params=params, proxies=PROXIES, timeout=30)
        except requests.RequestException as exc:
            last_err = f"network: {type(exc).__name__}"
            time.sleep(DELAY)
            continue

        if r.status_code == 429:
            wait = RETRY_BASE * (2 ** attempt)
            print(f"    429 限速 appid {appid}，{wait}s 后重试", flush=True)
            time.sleep(wait)
            last_err = "http 429"
            continue

        if r.status_code != 200:
            return None, f"http {r.status_code}"

        try:
            stats = r.json().get("playerstats") or {}
        except ValueError:
            return None, "bad json"

        if not stats.get("success") or "achievements" not in stats:
            return None, "no achievements (success=false or private)"

        return stats["achievements"], ""

    return None, last_err or "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个（按候选优先排序）")
    ap.add_argument("--only", default="", help="额外把指定 appid（逗号分隔）排到最前面")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    ap.add_argument("--resume", action="store_true",
                    help="断点续跑：跳过今天已同步过 achievements 且 completion_estimated=0 的游戏")
    args = ap.parse_args()

    api_key, steam_id = read_env()
    OUT_RAW.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # 全成就的排前面，这样 --limit 20 就能覆盖到真正会改的那批
    rows = cur.execute(
        """
        SELECT g.id, i.media_id AS appid, i.title, g.end_date,
               g.completion_estimated, g.status,
               g.achievements_total, g.achievements_unlocked,
               g.achievements_synced_at
        FROM app_game g JOIN app_item i ON i.id = g.item_id
        WHERE g.achievements_total > 0
        ORDER BY CASE WHEN g.achievements_unlocked >= g.achievements_total
                      THEN 0 ELSE 1 END, g.id
        """
    ).fetchall()
    if args.resume:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        before = len(rows)
        rows = [
            r for r in rows
            if not (r["completion_estimated"] == 0
                    and r["achievements_synced_at"]
                    and str(r["achievements_synced_at"])[:10] == today)
        ]
        print(f"--resume：跳过今天已处理过的 {before - len(rows)} 个", flush=True)
    if args.only:
        wanted = [x.strip() for x in args.only.split(",") if x.strip()]
        head = [r for w in wanted for r in rows if str(r["appid"]) == w]
        head_ids = {r["id"] for r in head}
        rows = head + [r for r in rows if r["id"] not in head_ids]
    if args.limit:
        rows = rows[: args.limit]

    print(f"待检查 {len(rows)} 个游戏（共 achievements_total>0 的全部）", flush=True)

    changed: list[dict] = []
    failed: list[dict] = []
    skipped_not_full = 0
    skipped_no_unlock = 0
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    pending_updates: list[tuple] = []

    def flush():
        if not pending_updates or args.dry_run:
            return
        cur.executemany(
            "UPDATE app_game SET end_date = ?, completion_estimated = 0, "
            "status = 'Completed' WHERE id = ?",
            pending_updates,
        )
        con.commit()

    for idx, row in enumerate(rows, 1):
        appid = str(row["appid"])
        title = row["title"] or ""

        achievements, err = fetch_achievements(api_key, steam_id, appid)
        time.sleep(DELAY)

        if achievements is None:
            failed.append(
                {"appid": appid, "title": title, "reason": err, "db_full": bool(
                    (row["achievements_unlocked"] or 0) >= (row["achievements_total"] or 1) > 0)}
            )
        else:
            total = len(achievements)
            unlocked = [a for a in achievements if a.get("achieved") == 1]
            stamps = [a.get("unlocktime", 0) for a in unlocked if a.get("unlocktime", 0) > 0]

            if not unlocked or total == 0:
                skipped_no_unlock += 1
            elif len(unlocked) < total:
                skipped_not_full += 1
            elif not stamps:
                failed.append({"appid": appid, "title": title,
                               "reason": "full but every unlocktime is 0", "db_full": True})
            else:
                last_ts = max(stamps)
                new_dt = datetime.fromtimestamp(last_ts, timezone.utc)
                new_str = new_dt.strftime("%Y-%m-%d %H:%M:%S")
                old_str = row["end_date"]
                changed.append({
                    "id": row["id"], "appid": appid, "title": title,
                    "old_end_date": old_str,
                    "new_end_date": new_str,
                    "old_estimated": row["completion_estimated"],
                    "old_status": row["status"],
                    "achievements": f"{len(unlocked)}/{total}",
                    "local": (new_dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"),
                })
                pending_updates.append((new_str, row["id"]))
                if len(pending_updates) >= COMMIT_EVERY:
                    flush()
                    pending_updates.clear()

        if idx % 50 == 0:
            print(f"  ... {idx}/{len(rows)} 已查，命中 {len(changed)}，失败 {len(failed)}",
                  flush=True)

    flush()
    con.close()

    payload = {
        "checked_at": now_ts,
        "checked": len(rows),
        "changed_count": len(changed),
        "skipped_not_full": skipped_not_full,
        "skipped_no_unlock": skipped_no_unlock,
        "failed_count": len(failed),
        "dry_run": args.dry_run,
        "changes": changed,
    }
    if not args.dry_run:
        CHANGES_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        if failed:
            FAILED_JSON.write_text(json.dumps(failed, ensure_ascii=False, indent=1), encoding="utf-8")

    from_none = [c for c in changed if not c["old_end_date"]]
    from_old = [c for c in changed if c["old_end_date"] and
                str(c["old_end_date"])[:19] != c["new_end_date"][:19]]
    same = [c for c in changed if c["old_end_date"] and
            str(c["old_end_date"])[:19] == c["new_end_date"][:19]]
    estimated_fixed = [c for c in changed if c["old_estimated"]]
    status_fixed = [c for c in changed if c["old_status"] != "Completed"]

    print("\n=== 汇总 ===", flush=True)
    print(f"检查 {len(rows)} / 修正 {len(changed)} / 没全成就跳过 {skipped_not_full} / "
          f"零解锁跳过 {skipped_no_unlock} / 失败 {len(failed)}"
          f"{'（dry-run 未写库）' if args.dry_run else ''}", flush=True)
    print(f"  · 原本没有日期、补出真实日期：{len(from_none)}", flush=True)
    print(f"  · 原本是推算/错误日期、改真实日期：{len(from_old)}", flush=True)
    print(f"  · 原本已正确、值不变（只清推算标记）：{len(same)}", flush=True)
    print(f"  · 其中 completion_estimated 由 1 变 0：{len(estimated_fixed)}", flush=True)
    print(f"  · 其中 status 由非 Completed 变 Completed：{len(status_fixed)}", flush=True)
    for c in status_fixed:
        print(f"      {c['appid']:>8}  {c['title'][:30]:<30} {c['old_status']} -> Completed",
              flush=True)

    print("\n=== 日期变化 ===", flush=True)
    for c in changed:
        print(f"{c['appid']:>8}  {c['title'][:34]:<34}  {str(c['old_end_date'])[:19]:<19} -> "
              f"{c['new_end_date'][:19]}  ({c['achievements']}, 本地 {c['local'][:16]})",
              flush=True)

    if failed:
        print("\n=== 失败 ===", flush=True)
        for f in failed[:40]:
            print(f"{f['appid']:>8}  {(f['title'] or '')[:30]:<30}  {f['reason']}", flush=True)
        if len(failed) > 40:
            print(f"... 共 {len(failed)} 条，见 {FAILED_JSON}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
