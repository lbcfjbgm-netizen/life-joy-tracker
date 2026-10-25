"""Backfill the reading percent of the already imported WeRead books.

The first import stored 0 in ``progress`` for every book because WeRead sends no
page count. The importer now keeps the percent there, but those 278 rows were
already written, so this walks the shelf once more and only touches ``progress``
(and the reading time beside it). It does not create, delete or re-rate
anything.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402

from app.models import Book, Item, Sources  # noqa: E402
from integrations.imports import weread  # noqa: E402

user = get_user_model().objects.first()
imp = weread.WeReadImporter(weread.read_api_key(), user, "new")

books = imp._fetch_shelf()
print(f"shelf={len(books)}  db rows={Book.objects.count()}")

items = {
    item.media_id: item
    for item in Item.objects.filter(source=Sources.WEREAD.value)
}
print(f"weread items={len(items)}")

updated = unchanged = missing = failed = 0
touched = []

for i, shelf_book in enumerate(books, 1):
    media_id = str(shelf_book.get("bookId") or "")
    item = items.get(media_id)
    if item is None:
        missing += 1
        continue

    try:
        progress = imp._fetch_progress(media_id)
    except weread.WeReadError as error:
        print(f"  fail {media_id}: {error}")
        failed += 1
        continue

    value = imp._page_progress(item, progress)
    entry = Book.objects.filter(item=item, user=user).first()
    if entry is None:
        missing += 1
        continue

    changed = False
    if entry.progress != value:
        entry.progress = value
        changed = True
    if progress["reading_seconds"] and entry.reading_seconds != progress["reading_seconds"]:
        entry.reading_seconds = progress["reading_seconds"]
        changed = True

    if changed:
        # save() would call process_progress and hit the provider; a narrow
        # update writes the two columns and nothing else
        Book.objects.filter(pk=entry.pk).update(
            progress=entry.progress,
            reading_seconds=entry.reading_seconds,
        )
        updated += 1
        if len(touched) < 12:
            touched.append((media_id, item.title, value, progress["percent"]))
    else:
        unchanged += 1

    if i % 50 == 0:
        print(f"  {i}/{len(books)} updated={updated} unchanged={unchanged} failed={failed}")

print(f"\ndone: updated={updated} unchanged={unchanged} missing={missing} failed={failed}")
print("\nsample changed rows (media_id, title, stored_progress, raw_percent):")
for row in touched:
    print("   ", row)
