"""Read only checks for the achievement failure counter.

Run with the Django shell of the project, from ``src``::

    ..\\.venv\\Scripts\\python.exe -X utf8 manage.py shell -c "exec(open('../.tools/verify_achievement_failures.py').read())"

Nothing is written and no Steam request is made: the three querysets of the
sync jobs are only counted, never walked.
"""

from django_celery_beat.models import PeriodicTask

from app.models import Game, MediaTypes, Sources
from integrations.imports import steam_recent

USER_ID = 1
STEAM_ID = "76561199487774529"

print("=== 1. the new field, on a real row ===")
game = Game.objects.select_related("item").order_by("?").first()
print("random game   :", game.item.title, "| appid", game.item.media_id)
print("failures      :", game.achievement_sync_failures)
print(
    "type / default:",
    type(game.achievement_sync_failures).__name__,
    "|",
    Game._meta.get_field("achievement_sync_failures").default,
)
print("field class   :", type(Game._meta.get_field("achievement_sync_failures")).__name__)

print()
print("=== 2. weekly rescan queryset (played games) ===")
played = Game.objects.filter(
    user_id=USER_ID,
    item__source=Sources.STEAM.value,
    item__media_type=MediaTypes.GAME.value,
    progress__gt=0,
    achievement_sync_failures__lt=steam_recent.MAX_ACHIEVEMENT_FAILURES,
)
print("MAX_ACHIEVEMENT_FAILURES :", steam_recent.MAX_ACHIEVEMENT_FAILURES)
print("weekly rescan count      :", played.count())
first = played.select_related("item").first()
print("first row                :", first.item.title if first else None,
      "| appid", first.item.media_id if first else None,
      "| progress", first.progress if first else None,
      "| failures", first.achievement_sync_failures if first else None)

# the same filter without the failure cap, to show what the cap removes
played_all = Game.objects.filter(
    user_id=USER_ID,
    item__source=Sources.STEAM.value,
    item__media_type=MediaTypes.GAME.value,
    progress__gt=0,
)
print("played, cap removed      :", played_all.count())

print()
print("=== 3. backfill queryset ===")
backfill = Game.objects.filter(
    user_id=USER_ID,
    item__source=Sources.STEAM.value,
    achievements_synced_at__isnull=True,
    achievement_sync_failures__lt=steam_recent.MAX_ACHIEVEMENT_FAILURES,
)
print("backfill count           :", backfill.count())
backfill_all = Game.objects.filter(
    user_id=USER_ID,
    item__source=Sources.STEAM.value,
    achievements_synced_at__isnull=True,
)
print("backfill, cap removed    :", backfill_all.count())
print("already blacklisted      :", Game.objects.filter(
    achievement_sync_failures__gte=steam_recent.MAX_ACHIEVEMENT_FAILURES,
).count())
print("games with a failure>0   :", Game.objects.filter(
    achievement_sync_failures__gt=0,
).count())

print()
print("=== 4. the weekly periodic task ===")
row = PeriodicTask.objects.filter(task="Refresh played Steam achievements").first()
if row is None:
    print("NOT FOUND")
else:
    print("id            :", row.id)
    print("name          :", row.name)
    print("enabled       :", row.enabled)
    print("crontab       :", row.crontab)
    print("last_run_at   :", row.last_run_at)
    print("total_run     :", row.total_run_count)

print()
print("=== 5. task registered with Celery ===")
from integrations import tasks  # noqa: E402

print("task name     :", tasks.refresh_played_steam_achievements.name)
print("wrapped so    :", tasks.refresh_played_steam_achievements.__class__.__name__)
print("done")
