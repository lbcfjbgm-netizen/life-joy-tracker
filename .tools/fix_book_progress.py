"""Fix the reading progress of books imported from WeRead.

A book whose page count is unknown keeps its reading position as a 0-100
percentage, so 100 is its ceiling. Two kinds of bad rows are repaired here:

1. ``progress > 100`` -- the number ran past the ceiling before the model
   learned to stop it (one book ended up at 101% after a "+1%" click);
2. ``status = Completed`` with ``progress != 100`` -- a finished book is read
   cover to cover, and the tracker now treats "finished" as the stronger
   statement, so an old row that never got the number raised is corrected.

Nothing else is touched: only the ``app_book`` table is read or written, so
games (minutes), movies (one show), TV (episodes) and page-counted books
(Douban / Hardcover / Open Library, which count in pages) keep their own
progress untouched. ``Item#2669`` (《我爱宝贝》, typed in by hand) is skipped by
primary key on top of that.

Usage, from the ``src`` directory::

    ..\\.venv\\Scripts\\python.exe ..\\.tools\\fix_book_progress.py            # dry run
    ..\\.venv\\Scripts\\python.exe ..\\.tools\\fix_book_progress.py --dry-run  # same
    ..\\.venv\\Scripts\\python.exe ..\\.tools\\fix_book_progress.py --apply    # write

The default is the dry run: nothing is written until ``--apply`` is passed.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

import django

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(os.path.dirname(_TOOLS_DIR), "src")
sys.path.insert(0, _SRC_DIR)
# The settings read relative paths against the working directory, so run as if
# started from ``src`` the way ``manage.py`` is
os.chdir(_SRC_DIR)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from app.models import Book, Status  # noqa: E402  (needs django.setup)

CEILING = 100
PROTECTED_ITEM_IDS = {2669}

# Buckets of the progress distribution, in the order they are printed
BUCKETS = (
    ("0", lambda p: p == 0),
    ("1-25", lambda p: 1 <= p <= 25),
    ("26-50", lambda p: 26 <= p <= 50),
    ("51-75", lambda p: 51 <= p <= 75),
    ("76-99", lambda p: 76 <= p <= 99),
    ("100", lambda p: p == 100),
    (">100", lambda p: p > 100),
)


def distribution():
    """Return ``{bucket: count}`` of every book, plus the total."""
    books = list(Book.objects.values_list("progress", flat=True))
    counts = Counter()
    for progress in books:
        for label, matches in BUCKETS:
            if matches(progress):
                counts[label] += 1
                break
    return {label: counts.get(label, 0) for label, _ in BUCKETS}, len(books)


def print_distribution(title):
    """Print the distribution of the reading progress of every book."""
    counts, total = distribution()
    print(f"{title} (共 {total} 本)")
    for label, _ in BUCKETS:
        print(f"  {label:>6}: {counts[label]:>3}")
    return counts


def is_percent_book(book):
    """Return whether the book counts in percent, never in pages.

    The unit follows ``Item.total_pages`` alone, exactly as the details page and
    ``Book.is_percent_book`` decide it: no page count means the stored number is
    a percentage.
    """
    return book.item.media_type == "book" and not book.item.total_pages


def collect():
    """Return the rows to fix, split by the rule that caught them."""
    over = []
    unfinished = []
    skipped_paged = []
    skipped_protected = []

    for book in Book.objects.select_related("item").order_by("id"):
        if book.item_id in PROTECTED_ITEM_IDS:
            skipped_protected.append(book)
            continue

        if not is_percent_book(book):
            # A book counted in pages has nothing to do with this percentage
            if book.progress > CEILING or (
                book.status == Status.COMPLETED.value and book.progress != CEILING
            ):
                skipped_paged.append(book)
            continue

        if book.progress > CEILING:
            over.append(book)
        elif book.status == Status.COMPLETED.value and book.progress != CEILING:
            unfinished.append(book)

    return over, unfinished, skipped_paged, skipped_protected


def apply(rows, label):
    """Set the progress of the given rows to 100, return how many were written."""
    written = 0
    for book in rows:
        if book.progress == CEILING:
            continue
        book.progress = CEILING
        # ``save`` keeps the history table in step; no status changes here, so no
        # calendar reload is triggered by the model
        book.save(update_fields=["progress"])
        written += 1
    print(f"  {label}: 写入 {written} 本")
    return written


def main():
    """Repair the progress of the affected books."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写库；不加这个参数只做 dry run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只报告不写库（默认行为，留着这个开关是为了明确表达意图）",
    )
    args = parser.parse_args()
    dry_run = not args.apply or args.dry_run

    print("=" * 60)
    print("书籍进度修正" + ("（dry run，不写库）" if dry_run else "（真跑，会写库）"))
    print("=" * 60)

    over, unfinished, skipped_paged, skipped_protected = collect()

    print_distribution("改前分布")
    print()
    print(f"规则 1：progress > 100 的书：{len(over)} 本")
    for book in over:
        print(f"  Book#{book.id} item#{book.item_id} {book.item.title}"
              f" progress={book.progress} status={book.status}")
    print(f"规则 2：已完成但进度不是 100 的书：{len(unfinished)} 本")
    buckets = Counter()
    for book in unfinished:
        for label, matches in BUCKETS:
            if matches(book.progress):
                buckets[label] += 1
                break
    for label, _ in BUCKETS:
        if buckets.get(label):
            print(f"  原进度 {label:>6}: {buckets[label]} 本")
    if skipped_paged:
        print(f"跳过（按页计数的书，不归这个百分比管）：{len(skipped_paged)} 本")
        for book in skipped_paged[:5]:
            print(f"  Book#{book.id} {book.item.title} "
                  f"progress={book.progress} pages={book.item.total_pages}")
    if skipped_protected:
        print(f"跳过（受保护条目）：{len(skipped_protected)} 本")
        for book in skipped_protected:
            print(f"  Book#{book.id} item#{book.item_id} {book.item.title}")

    if dry_run:
        print()
        print("dry run 结束，数据库没有改动。确认无误后加 --apply 再跑一遍。")
        return

    print()
    print("开始写库……")
    changed = apply(over, "规则 1（>100 → 100）")
    changed += apply(unfinished, "规则 2（已完成 → 100）")
    print()
    print(f"共写入 {changed} 本")
    print()
    print_distribution("改后分布")
    print()
    print("校验：")
    print(f"  progress > 100 的书：{Book.objects.filter(progress__gt=CEILING).count()} 本（应为 0）")
    remaining = (
        Book.objects.filter(status=Status.COMPLETED.value)
        .exclude(progress=CEILING)
        .count()
    )
    print(f"  已完成但进度不是 100 的书：{remaining} 本（应为 0）")
    print("  游戏 / 电影 / 剧集 数据：本脚本从未读写，未改动")


if __name__ == "__main__":
    main()
