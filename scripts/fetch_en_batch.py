"""Batch-fetch Steam **english store** names for many appids at once.

Why this script exists
----------------------
``store.steampowered.com/api/appdetails`` only answers one appid per request
(a comma separated list is rejected with HTTP 400 / body ``null``), so the
single-appid script needs ~1.5 s per game.  The store front-end itself uses
``IStoreBrowseService/GetItems``, which accepts a whole list of appids in one
POST-ish GET and answers in well under a second for 50 ids.

This script therefore:

* only asks for appids that have **no** ``<appid>.english.json`` yet
  (natural resume -- an existing file is never refetched)
* sends ``--batch-size`` appids per request (default 30, hard cap 50)
* sleeps ``--sleep-min``..``--sleep-max`` seconds between batches
* HTTP 429/403 -> pause 5 minutes, three such pauses in a row -> give up
* saves the untouched per-batch response as
  ``.tools/raw/steam_store/<day>/_batch_en_0001.json`` (iron rule: raw first)
* then rewrites every item into the per-appid file the single-appid script
  already uses, ``<appid>.english.json``, in the same
  ``{"<appid>": {"success": true, "data": {...}}}`` shape, so downstream
  tooling does not care which endpoint produced it

Usage (from ``yamtrack/``)::

    .venv\\Scripts\\python.exe scripts\\fetch_en_batch.py --dry-run --limit 5
    .venv\\Scripts\\python.exe scripts\\fetch_en_batch.py --limit 300
    .venv\\Scripts\\python.exe scripts\\fetch_en_batch.py          # everything
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

GETITEMS_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
RAW_ROOT = ROOT / ".tools" / "raw" / "steam_store"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DEFAULT_PROXY = "http://127.0.0.1:7897"
COOLDOWN = 300
MAX_COOLDOWNS = 3
REQUEST_TIMEOUT = 60
SOFT_RETRIES = 3
MAX_BATCH = 50
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def steam_appids() -> list[str]:
    import django

    django.setup()
    from app.models import Item  # noqa: PLC0415

    qs = Item.objects.filter(source="steam").only("media_id")
    ids = [str(i.media_id) for i in qs if str(i.media_id).isdigit()]
    ids.sort(key=int)
    return ids


def already_done(folder: Path, appid: str) -> bool:
    return (folder / f"{appid}.english.json").exists()


def call(ids: list[str], proxy: str | None, lang: str, cc: str):
    """Return ``(status, body_bytes)``; raises nothing."""
    import requests  # noqa: PLC0415

    payload = {
        "ids": [{"appid": int(a)} for a in ids],
        "context": {"language": lang, "country_code": cc, "steam_realm": 1},
        "data_request": {"include_basic_info": True},
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    resp = requests.get(
        GETITEMS_URL,
        params={"input_json": json.dumps(payload)},
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        timeout=REQUEST_TIMEOUT,
        proxies=proxies,
    )
    return resp.status_code, resp.content


def to_appdetails_shape(appid: str, item: dict) -> dict:
    """Wrap one GetItems entry in the ``appdetails`` envelope."""
    name = (item.get("name") or "").strip()
    data = {
        "name": name,
        "type": {0: "game", 1: "dlc", 2: "hardware", 3: "demo"}.get(item.get("type"), "game"),
        "steam_appid": int(appid),
        "store_url_path": item.get("store_url_path"),
        "visible": item.get("visible"),
        "source_endpoint": "IStoreBrowseService/GetItems",
    }
    basic = item.get("basic_info") or {}
    if basic:
        data["short_description"] = basic.get("short_description")
        data["developers"] = basic.get("developers") or []
        data["publishers"] = basic.get("publishers") or []
        data["header_image"] = basic.get("header_image")
        data["capsule_image"] = basic.get("capsule_image")
    return {appid: {"success": True, "data": data}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Steam 商店英文名批量抓取（只落 JSON，不写库）")
    ap.add_argument("--limit", type=int, default=0, help="本次最多查多少个 appid")
    ap.add_argument("--batch-size", type=int, default=30, help=f"每批 appid 数（上限 {MAX_BATCH}）")
    ap.add_argument("--sleep-min", type=float, default=3.0)
    ap.add_argument("--sleep-max", type=float, default=5.0)
    ap.add_argument("--lang", default="english")
    ap.add_argument("--cc", default="US")
    ap.add_argument("--day", default="")
    ap.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or DEFAULT_PROXY)
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只查、只落 JSON")
    ap.add_argument("--refetch-failed", action="store_true",
                    help="把已有但 success=false 的单个文件也重查")
    args = ap.parse_args(argv)

    batch_size = max(1, min(args.batch_size, MAX_BATCH))
    day = args.day or dt.date.today().isoformat()
    folder = RAW_ROOT / day
    folder.mkdir(parents=True, exist_ok=True)

    todo = steam_appids()
    total = len(todo)
    if args.refetch_failed:
        pending = [a for a in todo if not already_done(folder, a) or _is_false(folder, a)]
    else:
        pending = [a for a in todo if not already_done(folder, a)]
    skipped = total - len(pending)
    if args.limit:
        pending = pending[: args.limit]
    if not pending:
        print(f"[DONE] 库里 {total} 个 appid 全都已有英文 JSON，没有要抓的", flush=True)
        return 0

    proxy = None if args.no_proxy else args.proxy
    print(
        f"[STAGE] 批量抓取 — 库 {total} 个 / 已有跳过 {skipped} 个 / 本次要抓 {len(pending)} 个 / "
        f"批大小 {batch_size} / 间隔 {args.sleep_min}-{args.sleep_max}s / "
        f"语言 {args.lang} / 地区 {args.cc} / 代理 {proxy or '直连'}",
        flush=True,
    )

    stats = {"batches": 0, "requests": 0, "429": 0, "403": 0, "ok": 0, "false": 0,
             "missing": 0, "net_fail": 0, "soft": 0, "cooldowns": 0}
    cooldowns = 0
    given_up = ""
    started = time.monotonic()
    batch_no = 0

    for start in range(0, len(pending), batch_size):
        chunk = pending[start: start + batch_size]
        batch_no += 1
        raw_file = folder / f"_batch_en_{batch_no:04d}.json"

        body = None
        for attempt in range(SOFT_RETRIES):
            wait = random.uniform(args.sleep_min, args.sleep_max) if stats["batches"] else 0
            if wait:
                time.sleep(wait)
            stats["requests"] += 1
            try:
                code, body_bytes = call(chunk, proxy, args.lang, args.cc)
            except Exception as exc:  # noqa: BLE001 - transport level
                stats["soft"] += 1
                stats["net_fail"] += len(chunk)
                print(f"  ! 批次 {batch_no} 网络错误：{exc!r}（第 {attempt + 1} 次）", flush=True)
                time.sleep(10 * (attempt + 1))
                continue
            if code in (429, 403):
                stats[str(code)] += 1
                cooldowns += 1
                stats["cooldowns"] += 1
                if cooldowns >= MAX_COOLDOWNS:
                    given_up = f"连续 {cooldowns} 次 429/403，停手"
                    print(f"  !! {code} 连续 {cooldowns} 次，停手不查了", flush=True)
                    break
                print(f"  ! {code} 被限速，暂停 {COOLDOWN // 60} 分钟再继续", flush=True)
                time.sleep(COOLDOWN)
                continue
            if code >= 500:
                stats["soft"] += 1
                print(f"  ! 批次 {batch_no} HTTP {code}，{10 * (attempt + 1)}s 后重试", flush=True)
                time.sleep(10 * (attempt + 1))
                continue
            if code != 200:
                stats["soft"] += 1
                print(f"  ! 批次 {batch_no} HTTP {code}，记失败", flush=True)
                time.sleep(5)
                continue
            cooldowns = 0
            body = body_bytes
            break

        if given_up:
            break

        stats["batches"] += 1
        if body is None:
            stats["net_fail"] += len(chunk)
            print(f"[{batch_no}] {len(chunk)} 个：整批网络失败", flush=True)
            continue

        raw_file.write_bytes(body)  # raw bytes on disk before parsing
        try:
            payload = json.loads(raw_file.read_text(encoding="utf-8"))
            items = payload["response"]["store_items"]
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 批次 {batch_no} JSON 解析失败：{exc!r}（原始文件已保留）", flush=True)
            stats["net_fail"] += len(chunk)
            continue

        by_id = {}
        for it in items:
            a = str(it.get("appid") or it.get("id") or "")
            if a.isdigit():
                by_id[a] = it

        got = 0
        for appid in chunk:
            it = by_id.get(appid)
            if it is None:
                stats["missing"] += 1
                continue
            if it.get("success") != 1 or not (it.get("name") or "").strip():
                stats["false"] += 1
                # remember the miss so a later run can tell it apart from "never asked"
                miss = {appid: {"success": False, "data": None,
                                "source_endpoint": "IStoreBrowseService/GetItems"}}
                (folder / f"{appid}.english.json").write_text(
                    json.dumps(miss, ensure_ascii=False), encoding="utf-8"
                )
                continue
            payload_one = to_appdetails_shape(appid, it)
            (folder / f"{appid}.english.json").write_text(
                json.dumps(payload_one, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            stats["ok"] += 1
            got += 1

        done = min(start + len(chunk), len(pending))
        elapsed = time.monotonic() - started
        rate = done / elapsed if elapsed > 0 else 0
        eta = (len(pending) - done) / rate / 60 if rate > 0 else 0
        print(
            f"[{batch_no}] {len(chunk)} 个 → 本批成功 {got} | 进度 {done}/{len(pending)} | "
            f"累计成功 {stats['ok']} / success=false {stats['false']} / 缺 {stats['missing']} | "
            f"已用 {elapsed / 60:.1f} 分钟 / 预计还剩 {eta:.0f} 分钟",
            flush=True,
        )

    elapsed = time.monotonic() - started
    summary = {
        "day": day,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "batch_size": batch_size,
        "sleep_range_s": [args.sleep_min, args.sleep_max],
        "lang": args.lang,
        "cc": args.cc,
        "pending": len(pending),
        "batches": stats["batches"],
        "requests": stats["requests"],
        "ok": stats["ok"],
        "success_false": stats["false"],
        "missing_from_response": stats["missing"],
        "network_fail": stats["net_fail"],
        "http_429": stats["429"],
        "http_403": stats["403"],
        "cooldowns": stats["cooldowns"],
        "elapsed_seconds": round(elapsed, 1),
        "stopped": given_up,
    }
    (folder / "_batch_en_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n[SUMMARY] " + json.dumps(summary, ensure_ascii=False), flush=True)
    if given_up:
        print(f"[STOPPED] {given_up}", flush=True)
    print(f"原始批次 JSON → {folder}", flush=True)
    return 0


def _is_false(folder: Path, appid: str) -> bool:
    f = folder / f"{appid}.english.json"
    try:
        payload = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return True
    entry = (payload or {}).get(appid) or {}
    return not entry.get("success")


if __name__ == "__main__":
    sys.exit(main())
