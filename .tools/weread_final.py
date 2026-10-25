"""Final verification: sample rows, score cross-check, idempotency proof."""
import json
import os
import re
import sqlite3
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

c = sqlite3.connect(r"src\db\db.sqlite3")
c.row_factory = sqlite3.Row

print("=" * 78)
print("1) source=weread book count")
print("   ", c.execute("select count(*) from app_item where source='weread'").fetchone()[0])

print("\n2) reading_seconds > 0")
row = c.execute(
    "select count(*), min(reading_seconds), max(reading_seconds) from app_book "
    "where reading_seconds > 0",
).fetchone()
print(f"    count={row[0]}  min={row[1]}s  max={row[2]}s")
print("    zero-reading books:", c.execute(
    "select count(*) from app_book where reading_seconds=0").fetchone()[0])
for r in c.execute(
    "select i.title, b.reading_seconds from app_book b join app_item i on i.id=b.item_id "
    "where b.reading_seconds>0 order by b.reading_seconds asc limit 3",
):
    print(f"    smallest: {r['title'][:24]} = {r['reading_seconds']}s")

print("\n3) total_pages")
print("    non-empty:", c.execute(
    "select count(*) from app_item where source='weread' and total_pages is not null"
).fetchone()[0])

print("\n4) score cross-check against newRating")
env = open(".env", encoding="utf-8").read()
key = re.search(r"WEREAD_API_KEY\s*=\s*(\S+)", env).group(1)
S = requests.Session()
S.headers.update({"Authorization": f"Bearer {key}", "Content-Type": "application/json"})


def call(api_name, **body):
    payload = {"api_name": api_name, "skill_version": "1.0.5"}
    payload.update(body)
    time.sleep(1.6)
    return S.post("https://i.weread.qq.com/api/agent/gateway",
                  data=json.dumps(payload), timeout=30).json()


rows = c.execute(
    "select i.media_id, i.title, b.score from app_book b join app_item i on i.id=b.item_id "
    "where b.score is not null limit 4",
).fetchall()
for r in rows:
    info = call("/book/info", bookId=r["media_id"])
    raw = info.get("newRating")
    expect = round(raw / 100, 2) if raw else None
    print(f"    newRating={raw!s:<6} /1000*10={expect!s:<6} -> stored={r['score']}  {r['title'][:22]}")

print("\n    score null but book exists:")
for r in c.execute(
    "select i.title, b.score from app_book b join app_item i on i.id=b.item_id "
    "where b.score is null limit 3",
):
    print(f"      {r['title'][:40]} score={r['score']!r}")

print("\n5) highlights / notes")
print("    highlights:", c.execute("select count(*) from app_highlight").fetchone()[0])
print("    notes:", c.execute("select count(*) from app_note").fetchone()[0])
for r in c.execute(
    "select i.title, h.chapter, h.range, h.text from app_highlight h "
    "join app_item i on i.id=h.item_id where length(h.text)>20 limit 2",
):
    print(f"    HL [{r['title'][:16]}] ch={r['chapter'][:18]!r} range={r['range']}")
    print(f"       text={r['text'][:70]!r}")
for r in c.execute(
    "select i.title, n.chapter, n.content, n.abstract from app_note n "
    "join app_item i on i.id=n.item_id where length(n.content)>5 limit 2",
):
    print(f"    NT [{r['title'][:16]}] ch={r['chapter'][:18]!r}")
    print(f"       content={r['content'][:50]!r}")
    print(f"       abstract={r['abstract'][:50]!r}")

print("\n6) idempotency: no duplicate (source,media_id)")
dups = c.execute(
    "select media_id, source, count(*) n from app_item "
    "where source='weread' group by media_id, source having n>1",
).fetchall()
print("    duplicate items:", len(dups))
print("    book rows:", c.execute("select count(*) from app_book").fetchone()[0])
print("    items weread:", c.execute(
    "select count(*) from app_item where source='weread'").fetchone()[0])

print("\n7) manual item 2669 untouched")
for r in c.execute("select id, source, title, total_pages from app_item where id=2669"):
    print("   ", dict(r))
print("    book rows for item 2669:",
      c.execute("select count(*) from app_book where item_id=2669").fetchone()[0])

print("\n8) states")
for r in c.execute("select status, count(*) from app_book group by status"):
    print("   ", tuple(r))
print("\n9) sample titles")
for r in c.execute("select media_id, title from app_item where source='weread' limit 5"):
    print(f"    {r['media_id']:<28} {r['title'][:40]}")
