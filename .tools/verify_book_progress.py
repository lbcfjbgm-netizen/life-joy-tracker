"""Verify the book progress ceiling end to end.

Every write happens inside a transaction that is rolled back at the end, so the
database is left exactly as it was found. Run from the project root::

    .venv\\Scripts\\python.exe .tools\\verify_book_progress.py
"""

from __future__ import annotations

import os
import sys

import django

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(os.path.dirname(_TOOLS_DIR), "src")
sys.path.insert(0, _SRC_DIR)
os.chdir(_SRC_DIR)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

import events.tasks  # noqa: E402,F401  (the model queues a calendar reload)
from django.db import transaction  # noqa: E402

from app.models import Book, Game, Movie, Season, Status  # noqa: E402


def head(title):
    """Print a section header."""
    print()
    print(f"----- {title} -----")


def main():
    """Run every check."""
    head("1) 那本 101% 的书现在是多少")
    book = Book.objects.select_related("item").get(item__media_id="814146")
    print(f"  item#{book.item_id} {book.item.title}")
    print(f"  Book#{book.id}  progress={book.progress}  status={book.status}")

    head("2) 库里还有没有超过 100 的书")
    print(f"  Book.objects.filter(progress__gt=100).count() = "
          f"{Book.objects.filter(progress__gt=100).count()}")

    head("3) 已读完的书是不是都 100")
    done = Book.objects.filter(status=Status.COMPLETED.value)
    print(f"  已完成 {done.count()} 本，其中进度不是 100 的："
          f"{done.exclude(progress=100).count()} 本")

    head("4) 在 100 的书上连点 +1%（事务回滚）")
    with transaction.atomic():
        row = Book.objects.select_related("item").get(id=book.id)
        print(f"  起点 progress={row.progress} status={row.status}")
        for i in range(3):
            row.increase_progress()
            row.refresh_from_db()
            print(f"  第 {i + 1} 次 +1% -> {row.progress}")
        row.decrease_progress()
        row.refresh_from_db()
        print(f"  再点一次 -1% -> {row.progress}（读完仍钉在 100）")
        transaction.set_rollback(True)
    print(f"  回滚后 progress={Book.objects.get(id=book.id).progress}")

    head("5) 拿一本 45% 的书走「标记读完」路径（事务回滚）")
    candidate = (
        Book.objects.filter(status=Status.IN_PROGRESS.value)
        .order_by("-progress")
        .first()
    )
    print(f"  样本 Book#{candidate.id} {candidate.item.title}"
          f"（本来就是 {candidate.progress}%）")
    with transaction.atomic():
        row = Book.objects.select_related("item").get(id=candidate.id)
        row.progress = 45
        row.save()
        row.refresh_from_db()
        print(f"  先设成 45% -> progress={row.progress} status={row.status}")
        row.status = Status.COMPLETED.value
        row.save()
        row.refresh_from_db()
        print(f"  标记读完 -> progress={row.progress} status={row.status}")
        transaction.set_rollback(True)
    row = Book.objects.get(id=candidate.id)
    print(f"  回滚后 progress={row.progress} status={row.status}")

    head("5b) 从「已读完」改回「在读」，进度保持不动（事务回滚）")
    with transaction.atomic():
        row = Book.objects.select_related("item").get(id=book.id)
        print(f"  改前 progress={row.progress} status={row.status}")
        row.status = Status.IN_PROGRESS.value
        row.save()
        row.refresh_from_db()
        print(f"  改后 progress={row.progress} status={row.status}")
        transaction.set_rollback(True)

    head("6) 游戏回归：同一个方法、同样的步长（事务回滚）")
    game = (
        Game.objects.filter(
            achievements_total__gt=0,
            status=Status.IN_PROGRESS.value,
        )
        .order_by("-achievements_total")
        .first()
    )
    print(f"  样本 Game#{game.id} {game.item.title}"
          f"（成就 {game.achievements_unlocked}/{game.achievements_total}）")
    print(f"  increase_progress 来自：{type(game).increase_progress.__qualname__}")
    with transaction.atomic():
        row = Game.objects.select_related("item").get(id=game.id)
        for label, action in (
            ("+30 分钟", "increase_progress"),
            ("+30 分钟", "increase_progress"),
            ("-30 分钟", "decrease_progress"),
        ):
            before = row.progress
            getattr(row, action)()
            row.refresh_from_db()
            print(f"  {label}: {before} -> {row.progress}"
                  f"（差 {row.progress - before:+d} 分钟）")
        transaction.set_rollback(True)
    print(f"  回滚后 progress={Game.objects.get(id=game.id).progress}")

    head("6b) 电影上限仍是 1，游戏/剧集不受 100 影响（事务回滚）")
    print(f"  Movie.increase_progress <- {Movie.increase_progress.__qualname__}")
    print(f"  Season.increase_progress <- {Season.increase_progress.__qualname__}")
    with transaction.atomic():
        movie = Movie.objects.order_by("id").first()
        row = Movie.objects.select_related("item").get(id=movie.id)
        row.status = Status.IN_PROGRESS.value
        row.progress = 7
        row.save()
        row.refresh_from_db()
        print(f"  电影设 progress=7 -> {row.progress}（被截到 1）")
        transaction.set_rollback(True)
    over = Game.objects.filter(progress__gt=100)
    print(f"  库里进度超过 100 的游戏仍有 {over.count()} 个，"
          f"最大 {Game.objects.order_by('-progress').first().progress} 分钟")

    head("6c) 按页计数的书不套 100 上限（临时造一本，回滚）")
    with transaction.atomic():
        from app.models import Item, MediaTypes, Sources

        tmp_item = Item.objects.create(
            media_id="VERIFY-PAGED-BOOK",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="临时页数书",
            total_pages=300,
        )
        tmp = Book.objects.create(
            item=tmp_item,
            user_id=book.user_id,
            progress=250,
            status=Status.IN_PROGRESS.value,
        )
        print(f"  250/300 页的书 -> progress={tmp.progress}")
        tmp.progress = 999
        tmp.save()
        tmp.refresh_from_db()
        print(f"  设成 999 页 -> progress={tmp.progress}（页数书不管 100）")
        transaction.set_rollback(True)
    print(f"  临时条目已回滚："
          f"{Book.objects.filter(item__media_id='VERIFY-PAGED-BOOK').count()} 条")

    head("7) 受保护条目")
    print(f"  Item#2669《我爱宝贝》的 Book 记录："
          f"{Book.objects.filter(item_id=2669).count()} 条（本来就没有，没被创建）")


if __name__ == "__main__":
    main()
