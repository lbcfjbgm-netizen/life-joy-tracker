# -*- coding: utf-8 -*-
"""Download the Notion screenshots of the imported games into .tools/notion_shots/.

The site looks for a hand added game's pictures at
``.tools/notion_shots/<media_id>/<file>`` and needs the media_id in the folder
name, so this reads the media_ids straight out of the dry-run plan file.

Usage (run from the repo root):
    .venv\\Scripts\\python.exe .tools\\download_notion_shots.py --dry-run
    .venv\\Scripts\\python.exe .tools\\download_notion_shots.py --limit-games 3 --limit-shots 10
    .venv\\Scripts\\python.exe .tools\\download_notion_shots.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
PLAN_JSON = ROOT / ".tools" / "_notion_import_plan.json"
SHOT_ROOT = ROOT / ".tools" / "notion_shots"
REPORT_JSON = ROOT / ".tools" / "_notion_shot_download.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"

ALLOWED = {".jpg", ".jpeg", ".png", ".webp"}
SIGNATURE = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG\r\n\x1a\n": ".png",
    b"RIFF": ".webp",
}


def sniff(data: bytes, declared: str) -> str:
    """Return the real suffix of the payload, falling back to the URL's own."""
    for magic, suffix in SIGNATURE.items():
        if data.startswith(magic):
            if suffix == ".webp" and data[8:12] != b"WEBP":
                continue
            return suffix
    return declared if declared in ALLOWED else ".jpg"


def suffix_of(url: str, name: str = "") -> str:
    """Best guess of the file type from the URL path or the Notion file name."""
    path = unquote(urlparse(url).path)
    for candidate in (Path(path).suffix, Path(name).suffix):
        if candidate.lower() in ALLOWED:
            return candidate.lower()
    return ".jpg"


def plan_entries(args) -> list[dict]:
    if not PLAN_JSON.exists():
        raise SystemExit(f"先跑 .tools/import_notion_games.py --dry-run 生成 {PLAN_JSON}")
    plan = json.loads(PLAN_JSON.read_text(encoding="utf-8"))
    rows = [row for row in plan.get("create", []) if row.get("screenshot_urls")]
    if args.limit_games:
        rows = rows[: args.limit_games]
    for row in rows:
        if args.limit_shots:
            row["_shots"] = row["screenshot_urls"][: args.limit_shots]
        else:
            row["_shots"] = row["screenshot_urls"]
    return rows


def download(sess: requests.Session, url: str, out_dir: Path, no: int | None, idx: int) -> dict:
    """Fetch one picture and write it under ``out_dir``; return a result record."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{(no if isinstance(no, int) else 999):02d}_{idx:02d}"
    try:
        resp = sess.get(url, timeout=60, stream=True)
    except requests.RequestException as exc:
        return {"ok": False, "reason": f"网络异常 {type(exc).__name__}", "url": url}
    with resp:
        if resp.status_code != 200:
            reason = "签名过期/无权限" if resp.status_code in (403, 400) else f"HTTP {resp.status_code}"
            if resp.status_code == 404:
                reason = "文件不存在"
            return {"ok": False, "reason": reason, "status": resp.status_code, "url": url}
        data = resp.content
    suffix = sniff(data, suffix_of(url))
    path = out_dir / f"{stem}{suffix}"
    path.write_bytes(data)
    return {
        "ok": True,
        "path": str(path),
        "bytes": len(data),
        "format": suffix,
        "url": url,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="really download")
    parser.add_argument("--dry-run", action="store_true", help="only list what would download")
    parser.add_argument("--limit-games", type=int, default=0, help="only the first N rows with shots")
    parser.add_argument("--limit-shots", type=int, default=0, help="only the first N shots per row")
    parser.add_argument("--limit-total", type=int, default=0, help="stop after N shots in total")
    parser.add_argument("--pause", type=float, default=0.2, help="seconds between requests")
    args = parser.parse_args()

    rows = plan_entries(args)
    total = sum(len(r["_shots"]) for r in rows)
    print(f"有截图的新条目 {len(rows)} 个，截图 {total} 张")

    if args.dry_run or not args.apply:
        for row in rows:
            print(f"  {row['no']} {row['title']} -> .tools/notion_shots/{row['media_id']}/"
                  f"({len(row['_shots'])} 张)")
        print("dry-run：未下载")
        return

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    results = []
    stop = False
    for row in rows:
        if stop:
            break
        out_dir = SHOT_ROOT / row["media_id"]
        for idx, url in enumerate(row["_shots"], start=1):
            if args.limit_total and len(results) >= args.limit_total:
                stop = True
                break
            record = download(sess, url, out_dir, row.get("no"), idx)
            record.update({"no": row["no"], "title": row["title"], "media_id": row["media_id"]})
            results.append(record)
            flag = "OK " if record["ok"] else "ERR"
            detail = (f"{record['bytes']}B {record['format']} -> {record['path']}"
                      if record["ok"] else record["reason"])
            print(f"  {flag} {row['no']} #{idx} {detail}", flush=True)
            time.sleep(args.pause)

    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    print(f"成功 {len(ok)} / 失败 {len(bad)}")
    reasons: dict[str, int] = {}
    for record in bad:
        reasons[record["reason"]] = reasons.get(record["reason"], 0) + 1
    for reason, count in reasons.items():
        print(f"  失败原因 {reason}: {count}")
    REPORT_JSON.write_text(
        json.dumps({"total": len(results), "ok": len(ok), "failed": len(bad),
                    "reasons": reasons, "results": results},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"明细：{REPORT_JSON}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
