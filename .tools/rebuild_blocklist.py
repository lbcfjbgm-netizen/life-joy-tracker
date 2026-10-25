"""Rebuild the Steam blocklist from the local library and the 2-week play log.

Rule (decided by the operator):
    junk = (steam_review_pct IS NULL OR steam_review_pct < 60)   # bad reviews
           AND progress < 600                                    # under 10 hours
           AND appid NOT IN recently played (last two weeks)     # not played now
All three must hold. Any one failing keeps the game visible.

The result is a superset of the previous blocklist: entries that were already
blocked stay blocked even when the current data would not select them again.

Run with the project venv, no Django needed:
    .venv\\Scripts\\python.exe .tools\\rebuild_blocklist.py
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # ...\yamtrack
DB_PATH = ROOT / "src" / "db" / "db.sqlite3"
BLOCKLIST_PATH = ROOT / ".tools" / "raw" / "steam_blocklist.json"
RECENT_PATH = ROOT / ".tools" / "raw" / "recently_played_2weeks.json"
# The original 317 games, frozen so every rebuild keeps blocking them
BASELINE_PATH = ROOT / ".tools" / "raw" / "steam_blocklist_v1_317.json"

THRESHOLD = 60
MIN_PLAY_MINUTES = 600          # 10 hours played in total buys an exemption
RULE_TEXT = (
    "好评率<60 或 无好评率(评论<100)，且累计游玩<600分钟(10小时)，"
    "且最近两周未游玩"
)


def load_recent():
    if not RECENT_PATH.exists():
        raise SystemExit(f"missing {RECENT_PATH}; run the recently-played fetch first")
    data = json.loads(RECENT_PATH.read_text(encoding="utf-8"))
    appids = {int(a) for a in data.get("appids") or []}
    if not appids:
        raise SystemExit("recently played list is empty; refusing to guess")
    return appids, data.get("fetched_at")


def load_old():
    """Return the baseline entries that must stay blocked forever."""
    if not BASELINE_PATH.exists():
        return {}
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    out = {}
    for entry in data.get("appids") or []:
        try:
            out[int(entry["appid"])] = entry
        except (KeyError, TypeError, ValueError):
            continue
    return out


def load_library():
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute(
            """
            SELECT CAST(i.media_id AS INTEGER) AS appid,
                   i.title,
                   g.steam_review_pct,
                   g.status,
                   g.progress
            FROM app_item i
            JOIN app_game g ON g.item_id = i.id
            WHERE i.media_type = 'game'
            """,
        ).fetchall()
    finally:
        con.close()
    lib = {}
    for appid, title, pct, status, progress in rows:
        lib[appid] = {
            "name": title,
            "review_pct": pct,
            "status": status,
            "progress": int(progress or 0),
        }
    return lib


def reason_for(pct):
    if pct is None:
        return "评论数不足100，无好评率"
    return f"好评率{pct}<{THRESHOLD}"


def main():
    recent, fetched_at = load_recent()
    old = load_old()
    lib = load_library()

    entries = {}
    exempt_played = []          # bad reviews, but 10h+ played
    exempt_played_recent = 0    # ... and also played in the last two weeks

    # 1. fresh evaluation of every game in the local library
    for appid, info in lib.items():
        pct = info["review_pct"]
        if pct is not None and pct >= THRESHOLD:
            continue
        if appid in recent:
            continue
        if info["progress"] >= MIN_PLAY_MINUTES:
            if appid in recent:
                exempt_played_recent += 1
            else:
                exempt_played.append(
                    {
                        "appid": appid,
                        "name": info["name"],
                        "review_pct": pct,
                        "progress": info["progress"],
                    },
                )
            continue
        entries[appid] = {
            "appid": appid,
            "name": info["name"],
            "review_pct": pct,
            "played_minutes": info["progress"],
            "reason": reason_for(pct),
            "source": "rule",
        }

    # 2. keep everything the old list already blocked (superset guarantee)
    kept_old = 0
    dropped_old_recent = []
    for appid, entry in old.items():
        if appid in recent:
            dropped_old_recent.append(appid)
            continue
        if appid in entries:
            entries[appid]["source"] = "rule+old"
            continue
        if appid in lib and lib[appid]["progress"] >= MIN_PLAY_MINUTES:
            # played long enough, the new rule lets it back in
            continue
        pct = entry.get("review_pct")
        if pct is None and appid in lib:
            pct = lib[appid]["review_pct"]
        entries[appid] = {
            "appid": appid,
            "name": entry.get("name")
            or (lib[appid]["name"] if appid in lib else f"App {appid}"),
            "review_pct": pct,
            "played_minutes": lib[appid]["progress"] if appid in lib else None,
            "reason": entry.get("reason") or reason_for(pct),
            "source": "old",
        }
        kept_old += 1

    appids = [entries[a] for a in sorted(entries)]
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rule": RULE_TEXT,
        "min_played_minutes": MIN_PLAY_MINUTES,
        "recent_played_fetched_at": fetched_at,
        "recent_played_count": len(recent),
        "count": len(appids),
        "appids": appids,
    }
    BLOCKLIST_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    new_only = sum(1 for e in appids if e["source"] == "rule")
    old_only = sum(1 for e in appids if e["source"] == "old")
    both = sum(1 for e in appids if e["source"] == "rule+old")
    null_reason = sum(1 for e in appids if e["review_pct"] is None)

    print(f"old blocklist entries      : {len(old)}")
    print(f"old kept only              : {old_only}")
    print(f"old also matched new rule  : {both}")
    print(f"new from rule only         : {new_only}")
    print(f"total blocklist            : {len(appids)}")
    print(f"  of which review_pct NULL : {null_reason}")
    print(f"recently played (2 weeks)  : {len(recent)} -> {sorted(recent)}")
    print(f"exempt by 10h played       : {len(exempt_played)}")
    print(f"  (also recently played)   : {exempt_played_recent}")
    for item in exempt_played[:10]:
        print(
            f"    {item['appid']} {item['name'][:40]} "
            f"pct={item['review_pct']} {item['progress']}min",
        )
    if dropped_old_recent:
        print(f"old entries unblocked because recently played: {dropped_old_recent}")
    print(f"written to {BLOCKLIST_PATH}")


if __name__ == "__main__":
    main()
