"""Register the weekly rescan of the played games' achievements, once.

Run with the Django shell of the project, from ``src``::

    ..\\.venv\\Scripts\\python.exe -X utf8 manage.py shell -c "exec(open('../.tools/add_weekly_achievement_task.py').read())"

It is idempotent: an existing schedule is updated in place instead of being
duplicated, and none of the other periodic tasks is touched. At the end the
row as it stands in ``django_celery_beat_periodictask`` is printed, so what the
scheduler will actually read can be checked without a second query.
"""

import json
import sys

from decouple import config
from django.utils import timezone
from django_celery_beat.models import CrontabSchedule, PeriodicTask

# The Steam ID lives in .env, the same key the site itself reads, so nothing
# personal is hard-coded here.
STEAM_ID = config("STEAM_ID", default="")
USER_ID = 1

if not STEAM_ID:
    print("No STEAM_ID in .env, nothing was changed.")
    sys.exit(1)

# Sunday 03:00, an hour before the 03:50 token refresh and well before the
# 04:20 import, so a run that overruns does not collide with either.
NAME = f"Refresh played Steam achievements for {STEAM_ID} at 03:00:00 weekly"
TASK = "Refresh played Steam achievements"

crontab, _ = CrontabSchedule.objects.get_or_create(
    minute="0",
    hour="3",
    day_of_month="*",
    month_of_year="*",
    day_of_week="0",
    timezone=timezone.get_default_timezone(),
)

task, created = PeriodicTask.objects.update_or_create(
    name=NAME,
    defaults={
        "task": TASK,
        "crontab": crontab,
        "interval": None,
        "clocked": None,
        "solar": None,
        "one_off": False,
        "enabled": True,
        "start_time": timezone.now(),
        "kwargs": json.dumps({"user_id": USER_ID, "steam_id": STEAM_ID}),
        "description": (
            "Weekly rescan of the achievements of every Steam game with a "
            "playtime above zero, whether it was synced before or not. The "
            "backfill only walks the games that never answered, so this is "
            "the only thing that keeps an old game's counters from freezing."
        ),
    },
)

print("weekly achievement refresh:", "created" if created else "updated")

row = PeriodicTask.objects.get(name=NAME)
print("id           :", row.id)
print("name         :", row.name)
print("task         :", row.task)
print("enabled      :", row.enabled)
print("one_off      :", row.one_off)
print("crontab      :", row.crontab)
print("crontab zone :", row.crontab.timezone if row.crontab else None)
print("interval     :", row.interval)
print("kwargs       :", row.kwargs)
print("last_run_at  :", row.last_run_at)
print("total_run    :", row.total_run_count)
