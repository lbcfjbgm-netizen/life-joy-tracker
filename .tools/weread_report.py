"""Report the state of the WeRead import straight from the project database."""
import sqlite3
import sys

c = sqlite3.connect(r"src\db\db.sqlite3")
c.row_factory = sqlite3.Row

n = c.execute("select count(*) from app_item where source='weread'").fetchone()[0]
print("items with source=weread:", n)
print("highlights:", c.execute("select count(*) from app_highlight").fetchone()[0])
print("notes:", c.execute("select count(*) from app_note").fetchone()[0])
print("book rows:", c.execute("select count(*) from app_book").fetchone()[0])

print("\n--- reading_seconds > 0 ---")
row = c.execute(
    "select count(*), min(reading_seconds), max(reading_seconds), sum(reading_seconds) "
    "from app_book where reading_seconds > 0",
).fetchone()
print("count/min/max/sum:", tuple(row))

print("\n--- total_pages non empty ---")
row = c.execute(
    "select count(*) from app_item where source='weread' and total_pages is not null",
).fetchone()
print(tuple(row))

print("\n--- score non empty ---")
row = c.execute("select count(*) from app_book where score is not null").fetchone()
print(tuple(row))

print("\n--- status mix ---")
for r in c.execute("select status, count(*) from app_book group by status"):
    print(tuple(r))

print("\n--- top 5 by reading_seconds ---")
for r in c.execute(
    "select i.title, b.reading_seconds, b.score, b.status, b.progress "
    "from app_book b join app_item i on i.id=b.item_id "
    "order by b.reading_seconds desc limit 5",
):
    print(dict(r))

print("\n--- sample score rows ---")
for r in c.execute(
    "select i.media_id, i.title, b.score from app_book b "
    "join app_item i on i.id=b.item_id where b.score is not null limit 6",
):
    print(dict(r))

print("\n--- manual item 2669 untouched? ---")
for r in c.execute("select id, source, title, total_pages from app_item where id=2669"):
    print(dict(r))

print("\n--- source mix ---")
for r in c.execute("select source, count(*) from app_item group by source"):
    print(tuple(r))
