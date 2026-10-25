"""Apply the Steam store metadata gathered by fetch_steam_store.py.

Rules (hard, from the task brief):
  * only fill EMPTY fields, never overwrite an existing value
  * title  -> only when the stored title has no Chinese char AND Steam's
              schinese name does
  * synopsis -> only when it is blank
  * image  -> only when it is blank (a dead-cover replace is opt-in via
              --replace-dead with an explicit appid list file)
  * genres / release_date: the Item model has no such column, so nothing is
    written; the values stay in .tools/raw/steam_store/<day>/*.json

Usage (from yamtrack/src, with the venv python):
    ..\\.venv\\Scripts\\python.exe ..\\.tools\\apply_store_meta.py --dry-run
    ..\\.venv\\Scripts\\python.exe ..\\.tools\\apply_store_meta.py --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
RAW_ROOT = ROOT / ".tools" / "raw" / "steam_store"
CJK = __import__("re").compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def has_cjk(text: str | None) -> bool:
    return bool(text) and CJK.search(text) is not None


NEW_HOST = "shared.fastly.steamstatic.com"


def _head_ok(url: str) -> bool:
    """True when the CDN answers 200 with a real image (proxy from env)."""
    import requests  # noqa: PLC0415

    proxy = os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:7897"
    try:
        resp = requests.head(
            url,
            timeout=15,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; YamtrackCoverRepair/1.0)"},
            proxies={"http": proxy, "https": proxy},
        )
    except Exception:  # noqa: BLE001
        return False
    return resp.status_code == 200 and "image" in resp.headers.get("Content-Type", "")


def pick_cover(appid: str, store_header: str | None) -> str | None:
    """Return a reachable cover URL for a game whose stored cover is dead.

    Steam never mirrored appids >= 3,000,000 to the old ``media.steampowered``
    path, so the store's own (hashed) asset path is the only real source. Its
    host is rewritten to ``shared.fastly`` because the ``shared.akamai`` host
    times out from this machine.
    """
    cands: list[str] = []
    header = (store_header or "").strip()
    if header:
        cands.append(header)
        # portrait art from the same hashed folder is nicer and usually present
        cands.append(
            __import__("re").sub(r"[^/]+$", "library_600x900.jpg", header)
        )
    base = f"https://{NEW_HOST}/store_item_assets/steam/apps/{appid}"
    cands += [f"{base}/library_600x900.jpg", f"{base}/header.jpg"]
    seen: set[str] = set()
    for url in cands:
        url = url.replace("shared.akamai.steamstatic.com", NEW_HOST)
        if url in seen:
            continue
        seen.add(url)
        if _head_ok(url):
            return url
    return None


def load_day(day: str) -> tuple[dict[str, dict], dict]:
    """Read the day's raw JSON files (resume friendly: _results.json optional)."""
    folder = RAW_ROOT / day
    sys.path.insert(0, str(ROOT / "scripts"))
    from fetch_steam_store import parse_data  # noqa: PLC0415

    rows: dict[str, dict] = {}
    files = sorted(p for p in folder.glob("[0-9]*.json") if not p.name.endswith(".english.json"))
    for path in files:
        appid = path.stem
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entry = (raw or {}).get(appid) or {}
        parsed = parse_data(appid, entry.get("data") if entry.get("success") else None)
        parsed["lang"] = "schinese"
        parsed["file"] = str(path)
        rows[appid] = parsed
    return rows, {"files": len(files), "folder": str(folder)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=dt.date.today().isoformat())
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--replace-dead", type=Path, default=None,
                    help="JSON file: {\"dead\": [appid, ...]} -> swap the cover")
    ap.add_argument("--replace-dead-url", action="store_true",
                    help="with --replace-dead, write the store header_image")
    ap.add_argument("--skip-title", action="store_true",
                    help="never touch Item.title (use when a title_zh column holds the name)")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    rows, meta = load_day(args.day)
    sys.path.insert(0, str(SRC))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from app.models import Item  # noqa: PLC0415

    dead: set[str] = set()
    if args.replace_dead and args.replace_dead.is_file():
        payload = json.loads(args.replace_dead.read_text(encoding="utf-8"))
        dead = {str(x) for x in (payload.get("dead") or payload.get("appids") or [])}

    stat = {
        "appids": len(rows),
        "store_ok": sum(1 for r in rows.values() if r.get("ok")),
        "store_failed": sum(1 for r in rows.values() if not r.get("ok")),
        "title": 0, "synopsis": 0, "image": 0, "image_dead": 0,
        "genres_available": sum(1 for r in rows.values() if r.get("genres")),
        "release_available": sum(1 for r in rows.values() if r.get("release_date")),
        "cn_name_available": sum(1 for r in rows.values() if r.get("name_has_cjk")),
        "skipped_title_already_cjk": 0,
        "skipped_title_no_store_cn": 0,
        "skipped_synopsis_filled": 0,
    }
    changes: list[dict] = []

    for appid, row in sorted(rows.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
        if not row.get("ok"):
            continue
        item = Item.objects.filter(source="steam", media_id=appid).first()
        if item is None:
            continue
        fields: list[str] = []
        old = {"title": item.title, "synopsis": item.synopsis, "image": item.image}

        name = (row.get("name") or "").strip()
        if args.skip_title:
            stat["skipped_title_by_flag"] = stat.get("skipped_title_by_flag", 0) + 1
        elif has_cjk(item.title or ""):
            stat["skipped_title_already_cjk"] += 1
        elif name and has_cjk(name):
            item.title = name
            fields.append("title")
            stat["title"] += 1
        else:
            stat["skipped_title_no_store_cn"] += 1

        desc = (row.get("short_description") or row.get("detailed_description") or "").strip()
        if desc and not (item.synopsis or "").strip():
            item.synopsis = desc
            fields.append("synopsis")
            stat["synopsis"] += 1
        elif desc:
            stat["skipped_synopsis_filled"] += 1

        if appid in dead:
            url = pick_cover(appid, row.get("header_image"))
            if url:
                item.image = url
                fields.append("image")
                stat["image_dead"] += 1
                stat["image"] += 1
            else:
                stat["image_dead_unresolved"] = stat.get("image_dead_unresolved", 0) + 1
        elif not (item.image or "").strip():
            img = (row.get("header_image") or "").strip()
            if img:
                item.image = img
                fields.append("image")
                stat["image"] += 1

        if fields:
            changes.append({"appid": appid, "fields": fields, "before": old})
            if apply:
                item.save(update_fields=fields)

    out = RAW_ROOT / args.day / "_apply_log.json"
    out.write_text(json.dumps({"applied": apply, "stats": stat, "changes": changes},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(stat, ensure_ascii=False, indent=2), flush=True)
    print(f"[APPLY] 模式={'写库' if apply else 'dry-run'} 变更 {len(changes)} 条 → {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
