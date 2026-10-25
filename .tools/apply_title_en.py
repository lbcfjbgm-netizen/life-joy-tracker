"""Fill Item.title_en from the raw Steam **english** store JSON.

Rules
-----
* only touches ``Item.title_en``; every other field is left alone
* a row that already has a non-empty ``title_en`` is never overwritten
* source of truth is ``<appid>.english.json`` on disk (raw before parsing)
* a name that still contains CJK characters is not an English name -> skip

Usage (from ``yamtrack/``)::

    .venv\\Scripts\\python.exe .tools\\apply_title_en.py --dry-run
    .venv\\Scripts\\python.exe .tools\\apply_title_en.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
RAW_ROOT = ROOT / ".tools" / "raw" / "steam_store"


def load_english_names(day: str) -> dict[str, str]:
    folder = RAW_ROOT / day
    if not folder.is_dir():
        raise SystemExit(f"没有这个目录：{folder}")
    names: dict[str, str] = {}
    for f in folder.glob("*.english.json"):
        appid = f.name[: -len(".english.json")]
        if not appid.isdigit():
            continue
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        entry = (payload or {}).get(appid) or {}
        if not entry.get("success"):
            continue
        name = ((entry.get("data") or {}).get("name") or "").strip()
        if name:
            names[appid] = name
    return names


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-10-22")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    apply = args.apply and not args.dry_run

    names = load_english_names(args.day)
    print(f"[STAGE] 落库 — 英文名 {len(names)} 个（来自 {args.day} 的原始 JSON）/ "
          f"模式 {'APPLY(只写 title_en)' if apply else 'DRY-RUN(不写库)'}", flush=True)

    import django

    django.setup()
    from app.models import Item  # noqa: PLC0415
    from django.db import connection

    c = connection.cursor()
    c.execute(
        "select id, media_id, title, title_zh, title_en from app_item where source='steam'"
    )
    rows = c.fetchall()

    filled = skipped_have = skipped_cjk = no_json = same_as_title = 0
    updates = []
    for item_id, media_id, title, title_zh, title_en in rows:
        appid = str(media_id)
        existing = (title_en or "").strip()
        if existing:
            skipped_have += 1
            continue
        name = names.get(appid)
        if not name:
            no_json += 1
            continue
        if CJK.search(name):
            skipped_cjk += 1
            continue
        updates.append((item_id, name))
        if (title or "") == name:
            same_as_title += 1

    print(
        f"[统计] 库里 steam 条目 {len(rows)} / 已有英文名 {skipped_have} / "
        f"待填 {len(updates)}（其中与 title 相同 {same_as_title}）/ "
        f"英文名仍含中文跳过 {skipped_cjk} / 没抓到 JSON {no_json}",
        flush=True,
    )

    if apply and updates:
        with connection.cursor() as w:
            w.executemany(
                "update app_item set title_en=? where id=? and (title_en is null or title_en='')",
                [(name, item_id) for item_id, name in updates],
            )
        print(f"[APPLY] 写入 {len(updates)} 条 title_en", flush=True)
    elif updates:
        for item_id, name in updates[:10]:
            print(f"  样例 id={item_id} -> {name}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
