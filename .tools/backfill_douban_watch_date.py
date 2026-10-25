"""Backfill end_date on films already imported, using the local collection scan.

The collection (all 646 interests, with their ``create_time``) was saved to
``.tools/_douban_all.json`` while the paging was being checked, so the watch
date of every film is already on disk. That means the films imported before the
date was written can be fixed with **no request to Douban at all**, which
matters because the site is currently answering its IP rate limit.

It uses the same functions the importer uses, so the stored value is exactly
what a re-run would have written.
"""

import json
import os
import sys

import django

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.utils import timezone  # noqa: E402

from app.models import Movie  # noqa: E402
from integrations.imports import douban_movie  # noqa: E402

COLLECTION = os.path.join(ROOT, ".tools", "_douban_all.json")
DRY_RUN = "--apply" not in sys.argv

with open(COLLECTION, encoding="utf-8") as handle:
    interests = json.load(handle)

# film id -> the interest entry, which holds create_time
by_id = {}
for entry in interests:
    subject = entry.get("subject") or {}
    if subject.get("subtype") == "movie" and subject.get("id"):
        by_id[str(subject["id"])] = entry

print(f"collection: {len(by_id)} films with a create_time on disk")
print("mode:", "DRY RUN (pass --apply to write)" if DRY_RUN else "APPLY")
print()

fixed = 0
skipped_has_date = 0
no_date = 0
wrong = []

for movie in Movie.objects.filter(item__source="douban").select_related("item"):
    media_id = movie.item.media_id
    entry = by_id.get(media_id)
    if entry is None:
        print(f"  ? {media_id} not in the local scan, left alone")
        continue

    watched = douban_movie.watched_at(entry)
    if watched is None:
        no_date += 1
        continue

    if movie.end_date is not None:
        if movie.end_date != watched:
            # A date that does not match the collection: it was typed by hand or
            # written by an older run, and the user's own value wins
            wrong.append((media_id, movie.item.title, movie.end_date, watched))
        skipped_has_date += 1
        continue

    print(
        f"  + {media_id} {movie.item.title[:20]:22s} "
        f"-> {timezone.localtime(watched):%Y-%m-%d %H:%M:%S}",
    )
    if not DRY_RUN:
        movie.end_date = watched
        movie.save(update_fields=["end_date"])
    fixed += 1

print()
print(f"filled: {fixed} | already had a date: {skipped_has_date} | no date sent: {no_date}")
if wrong:
    print()
    print("dates that differ from the collection (kept as they are):")
    for media_id, title, current, expected in wrong[:5]:
        print(
            f"  {media_id} {title[:20]:22s} now "
            f"{timezone.localtime(current):%Y-%m-%d} vs collection "
            f"{timezone.localtime(expected):%Y-%m-%d}",
        )
