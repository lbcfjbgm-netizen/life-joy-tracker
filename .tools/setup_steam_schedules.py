"""Create the two Steam sync schedules, once.

Run with the Django shell of the project, from ``src``::

    ..\\.venv\\Scripts\\python.exe -X utf8 manage.py shell -c "exec(open('../.tools/setup_steam_schedules.py').read())"

It is idempotent: an existing schedule is updated in place instead of being
duplicated, and the eight pre-existing tasks are never touched.
"""

import json

from django.utils import timezone
from django_celery_beat.models import (
    CrontabSchedule,
    IntervalSchedule,
    PeriodicTask,
)

STEAM_ID = "STEAM_ID64"
USER_ID = 1

# Daily, half an hour after the full Steam import at 10:00, so the library is
# already up to date when the recent games are looked at.
RECENT_NAME = "Sync recent Steam games at 10:30:00 daily"
BACKFILL_NAME = "Backfill Steam achievements"

recent_crontab, _ = CrontabSchedule.objects.get_or_create(
    minute="30",
    hour="10",
    day_of_month="*",
    month_of_year="*",
    day_of_week="*",
    timezone=timezone.get_default_timezone(),
)

recent, recent_created = PeriodicTask.objects.update_or_create(
    name=RECENT_NAME,
    defaults={
        "task": "Sync recent Steam games",
        "crontab": recent_crontab,
        "interval": None,
        "clocked": None,
        "solar": None,
        "one_off": False,
        "enabled": True,
        "kwargs": json.dumps({"user_id": USER_ID, "steam_id": STEAM_ID}),
        "description": (
            "Read the games played during the last two weeks, import the new "
            "ones, and refresh the playtime and achievements of the others "
            "without overwriting a rating, a note, or a dropped/paused status."
        ),
    },
)
print(
    "recent sync:",
    "created" if recent_created else "updated",
    recent.name,
    recent.crontab,
)

# The backfill is a one-off: it walks every game that never had its
# achievements read, which is a long run, and it must not repeat tomorrow. A
# daily interval is used as the trigger only because Celery beat refuses a task
# without a schedule; ``one_off`` switches it back off after the single run.
backfill_interval, _ = IntervalSchedule.objects.get_or_create(
    every=1,
    period=IntervalSchedule.DAYS,
)

backfill, backfill_created = PeriodicTask.objects.update_or_create(
    name=BACKFILL_NAME,
    defaults={
        "task": "Backfill Steam achievements",
        "crontab": None,
        "interval": backfill_interval,
        "clocked": None,
        "solar": None,
        "one_off": True,
        "enabled": False,
        "start_time": timezone.now(),
        "kwargs": json.dumps({"user_id": USER_ID, "steam_id": STEAM_ID}),
        "description": (
            "One-time catch-up of the achievements of every Steam game that "
            "never had them. Disabled on purpose: enable it for a single run, "
            "Celery beat switches it back off afterwards."
        ),
    },
)
print(
    "backfill:",
    "created" if backfill_created else "updated",
    backfill.name,
    "one_off=",
    backfill.one_off,
    "enabled=",
    backfill.enabled,
)
