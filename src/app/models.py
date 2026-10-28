import logging
import uuid

from django.apps import apps
from django.conf import settings
from django.core.validators import (
    DecimalValidator,
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models
from django.db.models import (
    CheckConstraint,
    Count,
    F,
    Max,
    Prefetch,
    Q,
    UniqueConstraint,
    Window,
)
from django.db.models.functions import RowNumber
from django.utils import timezone
from model_utils import FieldTracker
from model_utils.fields import MonitorField
from simple_history.models import HistoricalRecords
from simple_history.utils import bulk_create_with_history, bulk_update_with_history

import app
import events
import users
from app import providers, status_filters
from app.mixins import CalendarTriggerMixin

logger = logging.getLogger(__name__)


class Sources(models.TextChoices):
    """Choices for the source of the item."""

    TMDB = "tmdb", "The Movie Database"
    MAL = "mal", "MyAnimeList"
    MANGAUPDATES = "mangaupdates", "MangaUpdates"
    IGDB = "igdb", "Internet Game Database"
    OPENLIBRARY = "openlibrary", "Open Library"
    HARDCOVER = "hardcover", "Hardcover"
    COMICVINE = "comicvine", "Comic Vine"
    BGG = "bgg", "BoardGameGeek"
    STEAM = "steam", "Steam"
    DOUBAN = "douban", "Douban"
    WEREAD = "weread", "WeRead"
    MANUAL = "manual", "Manual"


class MediaTypes(models.TextChoices):
    """Choices for the media type of the item."""

    TV = "tv", "TV Show"
    SEASON = "season", "TV Season"
    EPISODE = "episode", "Episode"
    MOVIE = "movie", "Movie"
    ANIME = "anime", "Anime"
    MANGA = "manga", "Manga"
    GAME = "game", "Game"
    BOOK = "book", "Book"
    COMIC = "comic", "Comic"
    BOARDGAME = "boardgame", "Boardgame"


# A book tracked from a source that sends no page count keeps its reading
# position as a 0-100 percentage, so 100 is its ceiling and a finished book sits
# exactly there. The unit of a book is decided by ``Item.total_pages`` alone and
# the details page reads it the same way: a book with a page count counts in
# pages, a book without one counts in percent (WeRead only ever reports a
# percent, and a hand typed book has nothing else to count).
BOOK_PROGRESS_PERCENT = 100


class Item(CalendarTriggerMixin, models.Model):
    """Model to store basic information about media items."""

    # limited by uuid for manual entries
    media_id = models.CharField(max_length=36)
    source = models.CharField(
        max_length=20,
        choices=Sources,
    )
    media_type = models.CharField(
        max_length=10,
        choices=MediaTypes,
        default=MediaTypes.MOVIE.value,
    )
    title = models.TextField()
    image = models.URLField()  # if add default, custom media entry will show the value
    # Local copy of the Steam store description, already cleaned to plain text.
    # It is kept on the item because the Steam provider never calls the network:
    # the text is seeded from the local library cache instead.
    synopsis = models.TextField(blank=True, default="")

    # Steam store genres, already translated to Chinese (e.g. ["动作", "独立"]).
    # Stored on the item for the same reason as synopsis: no network at read time.
    genres = models.JSONField(default=list, blank=True)

    # Directors of a movie, kept on the item so the card can show them without
    # hitting TMDB again. Plain list of names, e.g. ["Christopher Nolan"].
    directors = models.JSONField(default=list, blank=True)

    # Cast list, each entry shaped {"name": str, "character": str | None}.
    # "character" is optional and simply left out when the provider omits it.
    cast = models.JSONField(default=list, blank=True)

    # The store release date. This is NOT the user's play window; that stays on
    # start_date / end_date of the media row.
    release_date = models.DateField(null=True, blank=True)

    # Official English title as reported by the store. Kept separate from title so
    # a Chinese query can fall back to the English name when no Chinese name exists.
    title_en = models.CharField(max_length=500, blank=True, default="")

    # Chinese title. Empty when the store has no localized name; the user can also
    # type one in by hand, so this is never auto-overwritten.
    title_zh = models.CharField(max_length=500, blank=True, default="")

    # Book page count, mirrored onto the item so a reading progress bar can be
    # drawn without joining the book row. Null means the book has no page count.
    total_pages = models.PositiveIntegerField(null=True, blank=True)

    season_number = models.PositiveIntegerField(null=True, blank=True)
    episode_number = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        """Meta options for the model."""

        constraints = [
            # Ensures items without season/episode numbers are unique
            UniqueConstraint(
                fields=["media_id", "source", "media_type"],
                condition=Q(season_number__isnull=True, episode_number__isnull=True),
                name="unique_item_without_season_episode",
            ),
            # Ensures seasons are unique within a show
            UniqueConstraint(
                fields=["media_id", "source", "media_type", "season_number"],
                condition=Q(season_number__isnull=False, episode_number__isnull=True),
                name="unique_item_with_season",
            ),
            # Ensures episodes are unique within a season
            UniqueConstraint(
                fields=[
                    "media_id",
                    "source",
                    "media_type",
                    "season_number",
                    "episode_number",
                ],
                condition=Q(season_number__isnull=False, episode_number__isnull=False),
                name="unique_item_with_season_episode",
            ),
            # Enforces that season items must have a season number but no episode number
            CheckConstraint(
                condition=Q(
                    media_type=MediaTypes.SEASON.value,
                    season_number__isnull=False,
                    episode_number__isnull=True,
                )
                | ~Q(media_type=MediaTypes.SEASON.value),
                name="season_number_required_for_season",
            ),
            # Enforces that episode items must have both season and episode numbers
            CheckConstraint(
                condition=Q(
                    media_type=MediaTypes.EPISODE.value,
                    season_number__isnull=False,
                    episode_number__isnull=False,
                )
                | ~Q(media_type=MediaTypes.EPISODE.value),
                name="season_and_episode_required_for_episode",
            ),
            # Prevents season/episode numbers from being set on non-TV media types
            CheckConstraint(
                condition=Q(
                    ~Q(
                        media_type__in=[
                            MediaTypes.SEASON.value,
                            MediaTypes.EPISODE.value,
                        ],
                    ),
                    season_number__isnull=True,
                    episode_number__isnull=True,
                )
                | Q(media_type__in=[MediaTypes.SEASON.value, MediaTypes.EPISODE.value]),
                name="no_season_episode_for_other_types",
            ),
            # Validate source choices
            CheckConstraint(
                condition=Q(source__in=Sources.values),
                name="%(app_label)s_%(class)s_source_valid",
            ),
            # Validate media_type choices
            CheckConstraint(
                condition=Q(media_type__in=MediaTypes.values),
                name="%(app_label)s_%(class)s_media_type_valid",
            ),
        ]
        ordering = ["media_id"]

    def __str__(self):
        """Return the name of the item."""
        name = self.title
        if self.season_number is not None:
            name += f" S{self.season_number}"
            if self.episode_number is not None:
                name += f"E{self.episode_number}"
        return name

    @classmethod
    def generate_manual_id(cls):
        """Generate a new ID for manual items.

        Uses a UUID to ensure uniqueness.
        """
        return str(uuid.uuid4())

    def fetch_releases(self, delay):
        """Fetch releases for the item."""
        if self._disable_calendar_triggers:
            return

        if self.media_type == MediaTypes.SEASON.value:
            # Get or create the TV item for this season
            try:
                tv_item = Item.objects.get(
                    media_id=self.media_id,
                    source=self.source,
                    media_type=MediaTypes.TV.value,
                )
            except Item.DoesNotExist:
                # Get metadata for the TV show
                tv_metadata = providers.services.get_media_metadata(
                    MediaTypes.TV.value,
                    self.media_id,
                    self.source,
                )
                tv_item = Item.objects.create(
                    media_id=self.media_id,
                    source=self.source,
                    media_type=MediaTypes.TV.value,
                    title=tv_metadata["title"],
                    image=tv_metadata["image"],
                )
                logger.info("Created TV item %s for season %s", tv_item, self)

            # Process the TV item instead of the season
            items_to_process = [tv_item]
        else:
            items_to_process = [self]

        if delay:
            events.tasks.reload_calendar.delay(items_to_process=items_to_process)
        else:
            events.tasks.reload_calendar(items_to_process=items_to_process)


def home_media_is_hidden(section_key, is_incoming, max_progress, progress):
    """Whether the hide-unreleased filter drops one home row.

    Mirrors ``app.home.is_active_in_progress_media``: on the in-progress shelf a
    title with an upcoming release still stays as long as there is released
    backlog left to watch.
    """
    if section_key != Status.IN_PROGRESS.value:
        return is_incoming
    if not is_incoming:
        return False
    return not (max_progress is not None and progress < max_progress)


class MediaManager(models.Manager):
    """Custom manager for media models."""

    # Columns the home page reads while it is still deciding *which* rows to
    # show. Turning a media row into a model instance costs ~0.14ms because
    # model_utils' FieldTracker copies every field on every instance, while the
    # same row read as a plain dict costs ~0.009ms. The planning shelf of this
    # library holds 2086 games, so the page would otherwise spend its whole
    # budget building objects that never reach the template.
    HOME_ROW_FIELDS = (
        "id",
        "progress",
        "progressed_at",
        "created_at",
        "item_id",
        "item__title",
        "item__media_type",
    )

    # Season and TV replace these two columns with properties computed from
    # their episodes, so their ordering cannot be derived from a plain row.
    _HOME_COMPUTED_FIELDS = ("progress", "progressed_at")

    def get_historical_models(self):
        """Return list of historical model names."""
        return [f"historical{media_type}" for media_type in MediaTypes.values]

    def get_media_list(self, user, media_type, status_filter, sort_filter, search=None):
        """Get media list based on filters, sorting and search."""
        model = apps.get_model(app_label="app", model_name=media_type)
        queryset = model.objects.filter(user=user.id)

        # 下拉的四个档位（全成就 / 已通关 / 进行中 / 已暂停）在 status_filters
        # 里翻译成真正的查询条件：全成就 = Completed + is_all_achievements，
        # 已通关 = Completed 全量（含全成就），已暂停同时命中 Paused 和 Planning。
        status_condition = status_filters.status_q(status_filter, model)
        if status_condition is not None:
            queryset = queryset.filter(status_condition)

        if search:
            # The stored Chinese (title_zh) and English (title_en) store names are
            # aliases of the display title, so a query typed in either language
            # has to match them too, not just the title column.
            search_filter = (
                Q(item__title__icontains=search)
                | Q(item__title_zh__icontains=search)
                | Q(item__title_en__icontains=search)
            )
            if any(field.name == "notes" for field in model._meta.get_fields()):
                search_filter |= Q(notes__icontains=search)
            queryset = queryset.filter(search_filter)

        queryset = queryset.annotate(
            repeats=Window(
                expression=Count("id"),
                partition_by=[F("item")],
            ),
            row_number=Window(
                expression=RowNumber(),
                partition_by=[F("item")],
                order_by=F("created_at").desc(),
            ),
        ).filter(row_number=1)

        queryset = queryset.select_related("item")
        queryset = self._apply_prefetch_related(queryset, media_type)

        if sort_filter:
            return self._sort_media_list(queryset, sort_filter, media_type)
        return queryset

    def _apply_prefetch_related(self, queryset, media_type):
        """Apply appropriate prefetch_related based on media type."""
        # Apply media-specific prefetches
        if media_type == MediaTypes.TV.value:
            return queryset.prefetch_related(
                Prefetch(
                    "seasons",
                    queryset=Season.objects.select_related("item"),
                ),
                Prefetch(
                    "seasons__episodes",
                    queryset=Episode.objects.select_related("item"),
                ),
            )

        base_queryset = queryset.prefetch_related(
            Prefetch(
                "item__event_set",
                queryset=events.models.Event.objects.all(),
                to_attr="prefetched_events",
            ),
        )

        if media_type == MediaTypes.SEASON.value:
            return base_queryset.prefetch_related(
                Prefetch(
                    "episodes",
                    queryset=Episode.objects.select_related("item"),
                ),
            )

        return base_queryset

    def _sort_media_list(self, queryset, sort_filter, media_type=None):
        """Sort media list using SQL sorting with annotations for calculated fields."""
        if media_type == MediaTypes.TV.value:
            return self._sort_tv_media_list(queryset, sort_filter)
        if media_type == MediaTypes.SEASON.value:
            return self._sort_season_media_list(queryset, sort_filter)

        return self._sort_generic_media_list(queryset, sort_filter)

    def _sort_tv_media_list(self, queryset, sort_filter):
        """Sort TV media list based on the sort criteria."""
        if sort_filter == "start_date":
            # Annotate with the minimum start_date from related seasons/episodes
            queryset = queryset.annotate(
                calculated_start_date=models.Min(
                    "seasons__episodes__end_date",
                    filter=models.Q(seasons__item__season_number__gt=0),
                ),
            )
            return queryset.order_by(
                models.F("calculated_start_date").asc(nulls_last=True),
                models.functions.Lower("item__title"),
            )

        if sort_filter == "end_date":
            # Annotate with the maximum end_date from related seasons/episodes
            queryset = queryset.annotate(
                calculated_end_date=models.Max(
                    "seasons__episodes__end_date",
                    filter=models.Q(seasons__item__season_number__gt=0),
                ),
            )
            return queryset.order_by(
                models.F("calculated_end_date").desc(nulls_last=True),
                models.functions.Lower("item__title"),
            )

        if sort_filter == "progress":
            # Annotate with the sum of episodes watched (excluding season 0)
            queryset = queryset.annotate(
                # Count episodes in regular seasons (season_number > 0)
                calculated_progress=models.Count(
                    "seasons__episodes",
                    filter=models.Q(seasons__item__season_number__gt=0),
                ),
            )
            return queryset.order_by(
                "-calculated_progress",
                models.functions.Lower("item__title"),
            )

        # Default to generic sorting
        return self._sort_generic_media_list(queryset, sort_filter)

    def _sort_season_media_list(self, queryset, sort_filter):
        """Sort Season media list based on the sort criteria."""
        if sort_filter == "start_date":
            # Annotate with the minimum end_date from related episodes
            queryset = queryset.annotate(
                calculated_start_date=models.Min("episodes__end_date"),
            )
            return queryset.order_by(
                models.F("calculated_start_date").asc(nulls_last=True),
                models.functions.Lower("item__title"),
            )

        if sort_filter == "end_date":
            # Annotate with the maximum end_date from related episodes
            queryset = queryset.annotate(
                calculated_end_date=models.Max("episodes__end_date"),
            )
            return queryset.order_by(
                models.F("calculated_end_date").desc(nulls_last=True),
                models.functions.Lower("item__title"),
            )

        if sort_filter == "progress":
            # Annotate with the maximum episode number
            queryset = queryset.annotate(
                calculated_progress=models.Max("episodes__item__episode_number"),
            )
            return queryset.order_by(
                "-calculated_progress",
                models.functions.Lower("item__title"),
            )

        # Default to generic sorting
        return self._sort_generic_media_list(queryset, sort_filter)

    def _sort_generic_media_list(self, queryset, sort_filter):
        """Apply generic sorting logic for all media types."""
        # Handle sorting by date fields with special null handling
        if sort_filter in ("start_date", "end_date"):
            # For start_date, sort ascending (earliest first)
            if sort_filter == "start_date":
                return queryset.order_by(
                    models.F(sort_filter).asc(nulls_last=True),
                    models.functions.Lower("item__title"),
                )
            # For other date fields, sort descending (latest first)
            return queryset.order_by(
                models.F(sort_filter).desc(nulls_last=True),
                models.functions.Lower("item__title"),
            )

        # Handle sorting by Item fields
        item_fields = [f.name for f in Item._meta.fields]
        if sort_filter in item_fields:
            if sort_filter == "title":
                # Case-insensitive title sorting
                return queryset.order_by(models.functions.Lower("item__title"))
            # Default sorting for other Item fields
            return queryset.order_by(
                f"-item__{sort_filter}",
                models.functions.Lower("item__title"),
            )

        # Default sorting by media field
        return queryset.order_by(
            models.F(sort_filter).desc(nulls_last=True),
            models.functions.Lower("item__title"),
        )

    # Columns the home page needs while it is only deciding *which* rows to show.
    # Instantiating a media row costs ~0.14ms (model_utils' FieldTracker copies
    # every field on every instance); reading the same row as a plain dict costs
    # ~0.009ms. The planning shelf of this library holds 2086 games, so the page
    # spends its whole budget turning rows into objects that never reach the
    # template.
    HOME_ROW_FIELDS = (
        "id",
        "progress",
        "progressed_at",
        "created_at",
        "item_id",
        "item__title",
        "item__media_id",
        "item__source",
        "item__media_type",
    )

    def get_home_status(
        self,
        user,
        status,
        sort_by,
        items_limit=None,
        specific_media_type=None,
        hide_unreleased=False,
    ):
        """Get a home media list for a specific status grouped by media type.

        ``items_limit`` may be ``None`` to ask for the whole shelf, and that old
        path is kept as it was. Callers that know how many rows the page renders
        get the paged path instead, which orders plain dicts and only builds the
        handful of model instances that are actually rendered.
        """
        if items_limit is not None:
            return self._get_home_status_paged(
                user=user,
                status=status,
                sort_by=sort_by,
                items_limit=items_limit,
                specific_media_type=specific_media_type,
                hide_unreleased=hide_unreleased,
            )

        list_by_type = {}
        media_types = self._get_media_types_to_process(user, specific_media_type)

        for media_type in media_types:
            # Get base media list for the requested status
            media_list = self.get_media_list(
                user=user,
                media_type=media_type,
                status_filter=status,
                sort_filter=None,
            )

            if not media_list:
                continue

            # Annotate with max_progress and next_event
            self.annotate_max_progress(media_list, media_type)
            self._annotate_next_event(media_list)

            # Sort the media list
            sorted_list = self._sort_home_media(media_list, sort_by)

            # Apply pagination
            total_count = len(sorted_list)
            if items_limit is None:
                paginated_list = sorted_list
            elif specific_media_type:
                paginated_list = sorted_list[items_limit:]
            else:
                paginated_list = sorted_list[:items_limit]

            list_by_type[media_type] = {
                "items": paginated_list,
                "total": total_count,
            }

        return list_by_type

    def _get_home_status_paged(
        self,
        user,
        status,
        sort_by,
        items_limit,
        specific_media_type,
        hide_unreleased,
    ):
        """Same payload as the all-at-once path, without materialising the shelf.

        The rows that decide the *order* are read as dicts; only the slice that
        is actually rendered is turned into model instances, so the number of
        instances built is the number of cards shown.
        """
        list_by_type = {}

        for media_type in self._get_media_types_to_process(user, specific_media_type):
            page_start = items_limit if specific_media_type else 0

            if self._home_row_fields(media_type) is None:
                # Computed-column media (seasons, shows) keep the instance path.
                rows = self._home_instance_rows(
                    user=user,
                    media_type=media_type,
                    status=status,
                    sort_by=sort_by,
                    hide_unreleased=hide_unreleased,
                )
                page_rows = rows[page_start : page_start + items_limit]
                items = page_rows
            else:
                rows = self._home_rows(
                    user=user,
                    media_type=media_type,
                    status=status,
                    sort_by=sort_by,
                    hide_unreleased=hide_unreleased,
                )
                page_rows = rows[page_start : page_start + items_limit]
                items = self._home_instances(user, media_type, page_rows)

            total_count = len(rows)
            if not page_rows and not (page_start > 0 and total_count):
                continue

            list_by_type[media_type] = {
                "items": items,
                "total": total_count,
            }

        return list_by_type

    def _home_instance_rows(self, user, media_type, status, sort_by, hide_unreleased):
        """The original all-at-once path, for media with computed columns."""
        media_list = self.get_media_list(
            user=user,
            media_type=media_type,
            status_filter=status,
            sort_filter=None,
        )
        if not media_list:
            return []

        self.annotate_max_progress(media_list, media_type)
        self._annotate_next_event(media_list)

        if hide_unreleased:
            media_list = [
                media
                for media in media_list
                if not home_media_is_hidden(
                    status,
                    bool(media.next_event),
                    media.max_progress,
                    media.progress,
                )
            ]

        return self._sort_home_media(media_list, sort_by)

    def _home_row_fields(self, media_type):
        """Columns the paging path may read, or ``None`` when it cannot be used.

        Season and TV compute ``progress`` / ``progressed_at`` from their
        episodes instead of reading them from a column, so their shelves keep
        using the instance path. Those shelves are small (170 rows here) and the
        huge one is games, so this costs nothing measurable.
        """
        model = apps.get_model(app_label="app", model_name=media_type)
        field_names = {field.name for field in model._meta.get_fields()}
        if not set(self._HOME_COMPUTED_FIELDS).issubset(field_names):
            return None
        return self.HOME_ROW_FIELDS

    def _home_rows(self, user, media_type, status, sort_by, hide_unreleased):
        """Read the whole shelf as plain dicts and return it in display order."""
        fields = self._home_row_fields(media_type)
        if fields is None:
            return None

        rows = list(
            self.get_media_list(
                user=user,
                media_type=media_type,
                status_filter=status,
                sort_filter=None,
            ).values(*fields),
        )
        if not rows:
            return []

        item_ids = {row["item_id"] for row in rows}
        max_progress_by_item = self._home_max_progress(item_ids, media_type)
        next_event_by_item = self._home_next_event_datetimes(item_ids)

        incoming_by_item = {
            item_id: bool(next_event_by_item.get(item_id))
            for item_id in item_ids
        }

        if hide_unreleased:
            rows = [
                row
                for row in rows
                if not home_media_is_hidden(
                    status,
                    incoming_by_item.get(row["item_id"]),
                    max_progress_by_item.get(row["item_id"]),
                    row["progress"],
                )
            ]

        # Fold the two annotations into the rows so the shared sort key reads a
        # single shape on both paths.
        for row in rows:
            row["max_progress"] = max_progress_by_item.get(row["item_id"])
            row["next_event_at"] = next_event_by_item.get(row["item_id"])

        return sorted(rows, key=self._home_sort_key(sort_by, self._home_row_sort_values))

    def _home_instances(self, user, media_type, page_rows):
        """Build the rendered models, carrying the home annotations on them."""
        if not page_rows:
            return []

        ordered_ids = [row["id"] for row in page_rows]

        queryset = self.get_media_list(
            user=user,
            media_type=media_type,
            status_filter=None,
            sort_filter=None,
        ).filter(pk__in=ordered_ids)

        self.annotate_max_progress(queryset, media_type)
        self._annotate_next_event(queryset)

        by_id = {media.pk: media for media in queryset}
        return [by_id[media_id] for media_id in ordered_ids if media_id in by_id]

    def _home_max_progress(self, item_ids, media_type):
        """Return ``{item_id: max released content number}`` for a media type."""
        current_datetime = timezone.now()

        if media_type == MediaTypes.MOVIE.value:
            return dict.fromkeys(item_ids, 1)

        if media_type == MediaTypes.TV.value:
            return self._home_tv_max_progress(item_ids, current_datetime)

        events_data = events.models.Event.objects.filter(
            item_id__in=item_ids,
            datetime__lte=current_datetime,
        ).values("item_id", "content_number")

        max_progress = {}
        for event in events_data:
            content_number = event["content_number"]
            if content_number is None:
                continue
            item_id = event["item_id"]
            max_progress[item_id] = max(max_progress.get(item_id, 0), content_number)
        return max_progress

    def _home_tv_max_progress(self, show_item_ids, current_datetime):
        """Released episode count per show, keyed by the show's own ``item_id``.

        ``show_item_ids`` are TV rows' ``item_id`` values, so the lookup goes
        through the show's ``media_id`` exactly like ``_annotate_tv_released_episodes``
        does, and the result is re-keyed back to ``item_id`` for the row dicts.
        """
        tv_rows = list(
            TV.objects.filter(item_id__in=show_item_ids).values(
                "item_id",
                "item__media_id",
            ),
        )
        if not tv_rows:
            return {}

        media_id_by_item_id = {
            row["item_id"]: row["item__media_id"] for row in tv_rows
        }

        released = events.models.Event.objects.filter(
            item__media_id__in=set(media_id_by_item_id.values()),
            item__media_type=MediaTypes.SEASON.value,
            item__season_number__gt=0,
            datetime__lte=current_datetime,
            content_number__isnull=False,
        ).values("item__media_id", "item__season_number", "content_number")

        per_show = {}
        for event in released:
            seasons = per_show.setdefault(event["item__media_id"], {})
            season_number = event["item__season_number"]
            season_max = seasons.get(season_number, 0)
            if event["content_number"] > season_max:
                seasons[season_number] = event["content_number"]

        return {
            item_id: sum(per_show.get(media_id, {}).values())
            for item_id, media_id in media_id_by_item_id.items()
        }

    def _home_next_event_datetimes(self, item_ids):
        """Return ``{item_id: earliest future event datetime}``.

        Events on the far-future sentinel are skipped: they mean "release date
        unknown" and the home page treats that as no upcoming release at all.
        """
        current_time = timezone.now()
        sentinel = events.models.SentinelDatetime.max_datetime()

        future_events = events.models.Event.objects.filter(
            item_id__in=item_ids,
            datetime__gt=current_time,
            datetime__lt=sentinel,
        ).values("item_id", "datetime")

        next_event = {}
        for event in future_events:
            item_id = event["item_id"]
            current = next_event.get(item_id)
            if current is None or event["datetime"] < current:
                next_event[item_id] = event["datetime"]
        return next_event

    def _home_model_sort_values(self, media):
        """Sort inputs for a model instance on the original home path."""
        return (
            getattr(media, "max_progress", None),
            getattr(media, "next_event", None),
            media.progress,
            media.progressed_at,
            media.created_at,
            media.item.title.lower(),
        )

    def _home_row_sort_values(self, row):
        """Sort inputs for a plain row read with ``HOME_ROW_FIELDS``."""
        return (
            row.get("max_progress"),
            row.get("next_event_at"),
            row["progress"],
            row["progressed_at"],
            row["created_at"],
            row["item__title"].lower(),
        )

    def _home_sort_key(self, sort_by, values_of):
        """Return the ordering key built from one row's sort inputs.

        Both home paths funnel their inputs through ``values_of`` and this one
        table, so the model path and the plain-row path cannot drift apart.

        ``values_of`` must return ``(max_progress, next_event, progress,
        progressed_at, created_at, lowercase_title)``. ``next_event`` is an
        Event on the model path and an aware datetime on the row path.
        """
        choices = users.models.HomeSortChoices

        def recent(values):
            max_progress, next_event, progress, progressed_at, created_at, title = (
                values
            )
            return -timezone.datetime.timestamp(
                progressed_at if progressed_at is not None else created_at,
            )

        def completion(values):
            max_progress, next_event, progress, _p, _c, _t = values
            return (
                max_progress is None,
                -(
                    (progress / max_progress * 100)
                    if max_progress and max_progress > 0
                    else 0
                ),
            )

        def episodes_left(values):
            max_progress, next_event, progress, _p, _c, _t = values
            return (
                max_progress is None,
                (max_progress - progress if max_progress else 0),
            )

        def upcoming(values):
            _m, next_event, _pr, _p, _c, _t = values
            if next_event is None:
                return (True, None)
            return (False, getattr(next_event, "datetime", next_event))

        def title(values):
            return values[5]

        primary_sort_functions = {
            choices.UPCOMING: upcoming,
            choices.RECENT: recent,
            choices.COMPLETION: completion,
            choices.EPISODES_LEFT: episodes_left,
            choices.TITLE: title,
        }

        primary_sort_function = primary_sort_functions[sort_by]

        def key(value):
            values = values_of(value)
            return (
                primary_sort_function(values),
                recent(values),
                values[5],
            )

        return key

    def _get_media_types_to_process(self, user, specific_media_type):
        """Determine which media types to process based on user settings."""
        if specific_media_type:
            return [specific_media_type]

        # Get active types excluding TV
        return [
            media_type
            for media_type in user.get_active_media_types()
            if media_type != MediaTypes.TV.value
        ]

    def _annotate_next_event(self, media_list):
        """Annotate next_event for media items."""
        current_time = timezone.now()

        for media in media_list:
            # Get future events sorted by datetime
            future_events = sorted(
                [
                    event
                    for event in getattr(media.item, "prefetched_events", [])
                    if event.datetime > current_time
                ],
                key=lambda e: e.datetime,
            )

            media.next_event = future_events[0] if future_events else None

    def _sort_home_media(self, media_list, sort_by):
        """Sort home media based on the selected sort criteria."""
        # Both home paths order through this table, so they cannot drift apart.
        return sorted(
            media_list,
            key=self._home_sort_key(sort_by, self._home_model_sort_values),
        )

    def annotate_max_progress(self, media_list, media_type):
        """Annotate max_progress for all media items."""
        current_datetime = timezone.now()

        if media_type == MediaTypes.MOVIE.value:
            for media in media_list:
                media.max_progress = 1
            return

        if media_type == MediaTypes.TV.value:
            self._annotate_tv_released_episodes(media_list, current_datetime)
            return

        # For other media types, calculate max_progress from events
        # Create a dictionary mapping item_id to max content_number
        max_progress_dict = {}

        item_ids = [media.item.id for media in media_list]

        # Fetch all relevant events in a single query
        # .order_by() 清掉 Event.Meta.ordering 带来的默认排序：这里只逐个条目取
        # 最大进度，遍历顺序无意义；保留排序会逼 SQLite 建临时文件，本机系统临时
        # 目录对 Python 不可写，会报 unable to open database file 导致整页 500。
        events_data = (
            events.models.Event.objects.filter(
                item_id__in=item_ids,
                datetime__lte=current_datetime,
            )
            .values("item_id", "content_number")
            .order_by()
        )

        # Process events to find max content number per item
        for event in events_data:
            item_id = event["item_id"]
            content_number = event["content_number"]
            if content_number is not None:
                current_max = max_progress_dict.get(item_id, 0)
                max_progress_dict[item_id] = max(current_max, content_number)

        for media in media_list:
            media.max_progress = max_progress_dict.get(media.item.id)

    def _annotate_tv_released_episodes(self, tv_list, current_datetime):
        """Annotate TV shows with the number of released episodes."""
        # Prefetch all relevant events in one query
        released_events = events.models.Event.objects.filter(
            item__media_id__in=[tv.item.media_id for tv in tv_list],
            item__source=tv_list[0].item.source if tv_list else None,
            item__media_type=MediaTypes.SEASON.value,
            item__season_number__gt=0,
            datetime__lte=current_datetime,
            content_number__isnull=False,
            # 显式清掉 Event.Meta.ordering 带来的 ORDER BY：下面只按字典逐条取
            # 每个季度的最大集数，遍历顺序完全不影响结果；而带上排序时 SQLite
            # 要对几千行宽行（JOIN item 全列）建临时文件，本机系统临时目录不可写，
            # 会直接报 unable to open database file，导致 /admin/tv 500。
        ).select_related("item").order_by()

        # Create a dictionary to store max episode numbers per season per show
        released_episodes = {}

        for event in released_events:
            media_id = event.item.media_id
            season_number = event.item.season_number
            episode_number = event.content_number

            if media_id not in released_episodes:
                released_episodes[media_id] = {}

            if (
                season_number not in released_episodes[media_id]
                or episode_number > released_episodes[media_id][season_number]
            ):
                released_episodes[media_id][season_number] = episode_number

        # Calculate total released episodes per TV show
        for tv in tv_list:
            tv_episodes = released_episodes.get(tv.item.media_id, {})
            tv.max_progress = sum(tv_episodes.values()) if tv_episodes else 0

    def fetch_media_for_items(self, media_types, item_ids, user, status_filter=None):
        """Fetch media objects for given items, optionally filtering by status.

        Args:
            media_types: Iterable of media type strings to query
            item_ids: QuerySet or list of item IDs to fetch media for
            user: User to filter media by
            status_filter: Optional status value to filter by

        Returns:
            dict mapping item_id to media object
        """
        media_by_item_id = {}

        for media_type in media_types:
            model = apps.get_model("app", media_type)
            prefix = (
                "related_season__"
                if media_type == MediaTypes.EPISODE.value
                else ""
            )
            status_condition = status_filters.status_q(
                status_filter,
                model,
                prefix=prefix,
            )

            if media_type == MediaTypes.EPISODE.value:
                filter_kwargs = {
                    "item__in": item_ids,
                    "related_season__user": user,
                }
            else:
                filter_kwargs = {
                    "item__in": item_ids,
                    "user": user,
                }

            queryset = model.objects.filter(**filter_kwargs)
            if status_condition is not None:
                queryset = queryset.filter(status_condition)
            queryset = queryset.select_related("item")
            queryset = self._apply_prefetch_related(queryset, media_type)
            self.annotate_max_progress(queryset, media_type)

            for entry in queryset:
                media_by_item_id.setdefault(entry.item_id, entry)

        return media_by_item_id

    def get_media(
        self,
        user,
        media_type,
        instance_id,
    ):
        """Get user media object given the media type and item."""
        model = apps.get_model(app_label="app", model_name=media_type)
        params = self._get_media_params(
            user,
            media_type,
            instance_id,
        )

        return model.objects.get(**params)

    def get_media_prefetch(
        self,
        user,
        media_type,
        instance_id,
    ):
        """Get user media object with prefetch_related applied."""
        model = apps.get_model(app_label="app", model_name=media_type)
        params = self._get_media_params(
            user,
            media_type,
            instance_id,
        )

        queryset = model.objects.filter(**params)

        queryset = self._apply_prefetch_related(queryset, media_type)
        media = queryset.get()
        self.annotate_max_progress([media], media_type)

        return media

    def _get_media_params(
        self,
        user,
        media_type,
        instance_id,
    ):
        """Get the common filter parameters for media queries."""
        params = {"id": instance_id}

        if media_type == MediaTypes.EPISODE.value:
            params["related_season__user"] = user
        else:
            params["user"] = user

        return params

    def filter_media(
        self,
        user,
        media_id,
        media_type,
        source,
        season_number=None,
        episode_number=None,
    ):
        """Filter media objects based on parameters."""
        model = apps.get_model(app_label="app", model_name=media_type)
        params = self._filter_media_params(
            media_type,
            media_id,
            source,
            user,
            season_number,
            episode_number,
        )

        return model.objects.filter(**params)

    def filter_media_prefetch(
        self,
        user,
        media_id,
        media_type,
        source,
        season_number=None,
        episode_number=None,
    ):
        """Filter user media object with prefetch_related applied."""
        queryset = self.filter_media(
            user,
            media_id,
            media_type,
            source,
            season_number,
            episode_number,
        ).select_related("item")
        queryset = self._apply_prefetch_related(queryset, media_type)
        self.annotate_max_progress(queryset, media_type)

        return queryset

    def _filter_media_params(
        self,
        media_type,
        media_id,
        source,
        user,
        season_number=None,
        episode_number=None,
    ):
        """Get the common filter parameters for media queries."""
        params = {
            "item__media_type": media_type,
            "item__source": source,
            "item__media_id": media_id,
        }

        if media_type == MediaTypes.SEASON.value:
            params["item__season_number"] = season_number
            params["user"] = user
        elif media_type == MediaTypes.EPISODE.value:
            params["item__season_number"] = season_number
            params["item__episode_number"] = episode_number
            params["related_season__user"] = user
        else:
            params["user"] = user

        return params


class Status(models.TextChoices):
    """Choices for item status."""

    COMPLETED = "Completed", "Completed"
    IN_PROGRESS = "In progress", "In Progress"
    PLANNING = "Planning", "Planning"
    PAUSED = "Paused", "Paused"
    DROPPED = "Dropped", "Dropped"


class UserMessageLevel(models.TextChoices):
    """Choices for persistent user messages."""

    SUCCESS = "success", "Success"
    WARNING = "warning", "Warning"
    ERROR = "error", "Error"
    INFO = "info", "Info"


class UserMessage(models.Model):
    """Persistent user notification shown in the toast UI."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    level = models.CharField(
        max_length=20,
        choices=UserMessageLevel,
        default=UserMessageLevel.INFO,
    )
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    shown_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Meta options for the model."""

        ordering = ["created_at"]
        indexes = [
            models.Index(
                fields=["user", "shown_at"],
                name="app_umsg_user_shown_idx",
            ),
        ]

    def __str__(self):
        """Return the message text."""
        return self.message

    @property
    def tags(self):
        """Return a Django-messages-compatible level tag."""
        return self.level


class Media(models.Model):
    """Abstract model for all media types."""

    history = HistoricalRecords(
        cascade_delete_history=True,
        inherit=True,
        excluded_fields=[
            "item",
            "progressed_at",
            "user",
            "related_tv",
            "created_at",
        ],
    )

    created_at = models.DateTimeField(auto_now_add=True)
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    score = models.DecimalField(
        null=True,
        blank=True,
        max_digits=3,
        decimal_places=1,
        validators=[
            DecimalValidator(3, 1),
            MinValueValidator(-10),
            MaxValueValidator(10),
        ],
    )
    progress = models.PositiveIntegerField(default=0)
    progressed_at = MonitorField(monitor="progress")
    status = models.CharField(
        max_length=20,
        choices=Status,
        default=Status.COMPLETED.value,
    )
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        """Meta options for the model."""

        abstract = True
        ordering = ["user", "item", "-created_at"]

    def __str__(self):
        """Return the title of the media."""
        return self.item.__str__()

    def save(self, *args, **kwargs):
        """Save the media instance."""
        if self.tracker.has_changed("progress"):
            self.process_progress()

        if self.tracker.has_changed("status"):
            self.process_status()

        super().save(*args, **kwargs)

    def create_user_message(self, message, level):
        """Create a persistent user notification."""
        message_context = str(self)
        if message_context and not message.startswith(message_context):
            message = f"{message_context} {message}"

        logger.info("Creating user message for %s: %s", self.user, message)

        UserMessage.objects.create(
            user=self.user,
            level=level,
            message=message,
        )

    @property
    def is_percent_book(self):
        """Return whether this media counts a book in percent instead of pages."""
        return (
            self.item.media_type == MediaTypes.BOOK.value
            and not self.item.total_pages
        )

    def process_progress(self):
        """Update fields depending on the progress of the media."""
        if self.progress < 0:
            self.progress = 0
        elif self.is_percent_book:
            # A book whose page count is unknown keeps its reading position as a
            # 0-100 percentage (WeRead sends nothing else), so 100 is its
            # ceiling, here and nowhere else. A finished book is pinned at 100
            # even while a decrease is being written, because "finished" is the
            # stronger statement and the two must not disagree. A book with a
            # page count is counted in pages and keeps the metadata path below.
            # The other media types are untouched: a game counts minutes, a
            # movie counts one show and a TV season counts episodes, and their
            # ceiling comes from the metadata or the calendar.
            if self.status == Status.COMPLETED.value:
                self.progress = BOOK_PROGRESS_PERCENT
            else:
                self.progress = min(self.progress, BOOK_PROGRESS_PERCENT)
        elif self.status == Status.IN_PROGRESS.value:
            max_progress = providers.services.get_media_metadata(
                self.item.media_type,
                self.item.media_id,
                self.item.source,
            )["max_progress"]

            if max_progress:
                self.progress = min(self.progress, max_progress)

                if self.progress == max_progress:
                    self.status = Status.COMPLETED.value

                    now = timezone.now().replace(second=0, microsecond=0)
                    self.end_date = now

    def process_status(self):
        """Update fields depending on the status of the media."""
        if self.status == Status.COMPLETED.value and self.is_percent_book:
            # A finished book is read cover to cover, so its percentage is 100.
            # Only a book counted in percent is pinned: a book with a page count
            # takes its ceiling from the metadata below, and the other media
            # types (a game in minutes, a movie in one show, a season in
            # episodes) never reach this branch. Switching a book back to
            # another status leaves the number alone, so a second read can start
            # below 100 without the tracker resetting it.
            self.progress = BOOK_PROGRESS_PERCENT
        elif self.status == Status.COMPLETED.value:
            max_progress = providers.services.get_media_metadata(
                self.item.media_type,
                self.item.media_id,
                self.item.source,
            )["max_progress"]

            if max_progress:
                self.progress = max_progress

        self.item.fetch_releases(delay=True)

    @property
    def formatted_score(self):
        """Return as int if score is 10.0 or 0.0, otherwise show decimal."""
        if self.score is not None:
            max_score = 10
            min_score = 0
            if self.score in (max_score, min_score):
                return int(self.score)
            return self.score
        return None

    @property
    def formatted_progress(self):
        """Return the progress of the media in a formatted string."""
        return str(self.progress)

    def increase_progress(self):
        """Increase the progress of the media by one."""
        self.progress += 1
        self.save()
        logger.info("Increased progress of %s to %s", self, self.progress)

    def decrease_progress(self):
        """Decrease the progress of the media by one."""
        self.progress -= 1
        self.save()
        logger.info("Decreased progress of %s to %s", self, self.progress)


class BasicMedia(Media):
    """Model for basic media types."""

    objects = MediaManager()


class TV(Media):
    """Model for TV shows."""

    tracker = FieldTracker()

    class Meta:
        """Meta options for the model."""

        ordering = ["user", "item"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "item"],
                name="%(app_label)s_%(class)s_unique_item_user",
            ),
        ]

    @tracker  # postpone field reset until after the save
    def save(self, *args, **kwargs):
        """Save the media instance."""
        super(Media, self).save(*args, **kwargs)

        if self.tracker.has_changed("status"):
            if self.status == Status.COMPLETED.value:
                self._completed()

            elif self.status == Status.DROPPED.value:
                self._mark_in_progress_seasons_as_dropped()

            elif (
                self.status == Status.IN_PROGRESS.value
                and not self.seasons.filter(status=Status.IN_PROGRESS.value).exists()
            ):
                self._start_next_available_season()

            self.item.fetch_releases(delay=True)

    @property
    def progress(self):
        """Return the total episodes watched for the TV show."""
        return sum(
            season.progress
            for season in self.seasons.all()
            if season.item.season_number != 0
        )

    def increase_progress(self):
        """Watch the next episode in the first season that is not finished.

        ``progress`` on a TV show is derived from its seasons and therefore
        read-only, so the increment is handed to the season. Specials (season
        zero) are skipped, the same way ``progress`` skips them.
        """
        seasons = sorted(
            (
                season
                for season in self.seasons.all()
                if season.item.season_number != 0
                and season.status != Status.COMPLETED.value
            ),
            key=lambda season: season.item.season_number,
        )

        if not seasons:
            logger.info("No more episodes to watch.")
            return

        seasons[0].increase_progress()

    def decrease_progress(self):
        """Unwatch the last episode of the season that has progress."""
        seasons = sorted(
            (
                season
                for season in self.seasons.all()
                if season.item.season_number != 0 and season.progress > 0
            ),
            key=lambda season: season.item.season_number,
            reverse=True,
        )

        if not seasons:
            logger.info("No episodes to unwatch.")
            return

        seasons[0].decrease_progress()

    @property
    def last_watched(self):
        """Return the latest watched episode in SxxExx format."""
        watched_episodes = [
            {
                "season": season.item.season_number,
                "episode": episode.item.episode_number,
                "end_date": episode.end_date,
            }
            for season in self.seasons.all()
            if hasattr(season, "episodes") and season.item.season_number != 0
            for episode in season.episodes.all()
            if episode.end_date is not None
        ]

        if not watched_episodes:
            return ""

        latest_episode = max(
            watched_episodes,
            key=lambda x: (x["end_date"], x["season"], x["episode"]),
        )

        return f"S{latest_episode['season']:02d}E{latest_episode['episode']:02d}"

    @property
    def progressed_at(self):
        """Return the date when the last episode was watched."""
        dates = [
            season.progressed_at
            for season in self.seasons.all()
            if season.progressed_at and season.item.season_number != 0
        ]
        return max(dates) if dates else None

    @property
    def start_date(self):
        """Return the date of the first episode watched."""
        dates = [
            season.start_date
            for season in self.seasons.all()
            if season.start_date and season.item.season_number != 0
        ]
        return min(dates) if dates else None

    @property
    def end_date(self):
        """Return the date of the last episode watched."""
        dates = [
            season.end_date
            for season in self.seasons.all()
            if season.end_date and season.item.season_number != 0
        ]
        return max(dates) if dates else None

    def _completed(self):
        """Create remaining seasons and episodes for a TV show."""
        tv_metadata = providers.services.get_media_metadata(
            self.item.media_type,
            self.item.media_id,
            self.item.source,
        )
        max_progress = tv_metadata["max_progress"]

        if not max_progress or self.progress > max_progress:
            return

        seasons_to_create = []
        seasons_to_update = []
        episodes_to_create = []
        current_date = timezone.localdate()
        tv_completed = True

        season_numbers = [
            season["season_number"]
            for season in tv_metadata["related"]["seasons"]
            if season["season_number"] != 0
        ]
        tv_with_seasons_metadata = providers.services.get_media_metadata(
            "tv_with_seasons",
            self.item.media_id,
            self.item.source,
            season_numbers,
        )
        for season_number in season_numbers:
            season_metadata = tv_with_seasons_metadata.get(f"season/{season_number}")
            if season_metadata is None:
                # The source describes the show but not its seasons; leaving
                # this season out is better than failing the whole save.
                logger.info(
                    "No season %s metadata for %s, skipped",
                    season_number,
                    self.item.media_id,
                )
                continue

            item, _ = Item.objects.get_or_create(
                media_id=self.item.media_id,
                source=self.item.source,
                media_type=MediaTypes.SEASON.value,
                season_number=season_number,
                defaults={
                    "title": self.item.title,
                    "image": season_metadata["image"],
                },
            )
            try:
                season_instance = Season.objects.get(
                    item=item,
                    user=self.user,
                )
                target_status = season_instance.get_completion_status(
                    season_metadata,
                    unreleased_only_status=Status.PLANNING.value,
                    current_date=current_date,
                )

                if season_instance.status != target_status:
                    season_instance.status = target_status
                    seasons_to_update.append(season_instance)

            except Season.DoesNotExist:
                season_instance = Season(
                    item=item,
                    score=None,
                    notes="",
                    related_tv=self,
                    user=self.user,
                )
                target_status = season_instance.get_completion_status(
                    season_metadata,
                    unreleased_only_status=Status.PLANNING.value,
                    current_date=current_date,
                )
                season_instance.status = target_status
                seasons_to_create.append(
                    season_instance,
                )

            if target_status != Status.COMPLETED.value:
                tv_completed = False

        bulk_create_with_history(seasons_to_create, Season)
        bulk_update_with_history(seasons_to_update, Season, ["status"])

        for season_instance in seasons_to_create + seasons_to_update:
            season_metadata = tv_with_seasons_metadata.get(
                f"season/{season_instance.item.season_number}"
            )
            if season_metadata is None:
                # No per-season data from this source: nothing can be filled in
                # for the season, so it is left exactly as it is.
                continue
            episodes_to_create.extend(
                season_instance.get_remaining_eps(
                    season_metadata,
                    current_date=current_date,
                ),
            )
        bulk_create_with_history(episodes_to_create, Episode)

        if episodes_to_create:
            created_episodes_count = len(episodes_to_create)
            episode_label = "episode" if created_episodes_count == 1 else "episodes"
            self.create_user_message(
                f"had {created_episodes_count} released {episode_label} marked "
                "as watched automatically.",
                level=UserMessageLevel.INFO,
            )

        if not tv_completed:
            self.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [self],
                TV,
                fields=["status"],
            )
            self.create_user_message(
                "was left in progress because unreleased episodes or seasons remain.",
                level=UserMessageLevel.WARNING,
            )

    def _mark_in_progress_seasons_as_dropped(self):
        """Mark all in-progress seasons as dropped."""
        in_progress_seasons = list(
            self.seasons.filter(status=Status.IN_PROGRESS.value),
        )

        for season in in_progress_seasons:
            season.status = Status.DROPPED.value

        if in_progress_seasons:
            bulk_update_with_history(
                in_progress_seasons,
                Season,
                fields=["status"],
            )

    def _start_next_available_season(
        self,
        min_season_number=0,
    ):
        """Find the next available season to watch and set it to in-progress."""
        min_season_number = int(min_season_number or 0)
        current_date = timezone.localdate()
        existing_seasons = {
            season.item.season_number: season
            for season in self.seasons.filter(
                item__season_number__gt=min_season_number,
            ).order_by("item__season_number")
        }
        tv_metadata = providers.services.get_media_metadata(
            self.item.media_type,
            self.item.media_id,
            self.item.source,
        )
        related_seasons = tv_metadata.get("related", {}).get("seasons", [])

        season_started = False
        started_season_number = None

        for season_data in related_seasons:
            season_number = season_data["season_number"]
            if season_number <= min_season_number:
                continue

            next_unwatched_season = existing_seasons.get(season_number)
            if (
                next_unwatched_season
                and next_unwatched_season.status == Status.COMPLETED.value
            ):
                continue

            if not app.helpers.is_released_date(
                season_data.get("first_air_date"),
                current_date,
            ):
                continue

            if next_unwatched_season is None:
                item, _ = Item.objects.get_or_create(
                    media_id=self.item.media_id,
                    source=self.item.source,
                    media_type=MediaTypes.SEASON.value,
                    season_number=season_number,
                    defaults={
                        "title": self.item.title,
                        "image": season_data["image"],
                    },
                )

                next_unwatched_season = Season(
                    item=item,
                    user=self.user,
                    related_tv=self,
                    status=Status.IN_PROGRESS.value,
                )
                bulk_create_with_history([next_unwatched_season], Season)
                season_started = True
                started_season_number = season_number
                break

            if next_unwatched_season.status != Status.IN_PROGRESS.value:
                next_unwatched_season.status = Status.IN_PROGRESS.value
                bulk_update_with_history(
                    [next_unwatched_season],
                    Season,
                    fields=["status"],
                )
                season_started = True
                started_season_number = season_number
            else:
                season_started = True
            break

        if season_started and self.status != Status.IN_PROGRESS.value:
            self.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [self],
                TV,
                fields=["status"],
            )

        if started_season_number is not None:
            self.create_user_message(
                f"Season {started_season_number} was marked as in progress "
                "automatically.",
                level=UserMessageLevel.INFO,
            )

        return season_started

    def _handle_completed_season(
        self,
        completed_season_number,
    ):
        """Start the next season, or complete the TV show if no seasons remain."""
        if self._start_next_available_season(
            completed_season_number,
        ):
            return

        incomplete_seasons_exist = (
            self.seasons.filter(
                item__season_number__gt=0,
            )
            .exclude(
                status=Status.COMPLETED.value,
            )
            .exists()
        )

        if incomplete_seasons_exist and self.status != Status.IN_PROGRESS.value:
            self.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [self],
                TV,
                fields=["status"],
            )
            self.create_user_message(
                "remains in progress because another season is still "
                "pending or has not aired yet.",
                level=UserMessageLevel.INFO,
            )

        elif not incomplete_seasons_exist and self.status != Status.COMPLETED.value:
            self.status = Status.COMPLETED.value
            bulk_update_with_history(
                [self],
                TV,
                fields=["status"],
            )
            self.create_user_message(
                "was marked as completed automatically.",
                level=UserMessageLevel.SUCCESS,
            )


class Season(Media):
    """Model for seasons of TV shows."""

    related_tv = models.ForeignKey(
        TV,
        on_delete=models.CASCADE,
        related_name="seasons",
    )

    tracker = FieldTracker()

    class Meta:
        """Limit the uniqueness of seasons.

        Only one season per media can have the same season number.
        """

        constraints = [
            models.UniqueConstraint(
                fields=["related_tv", "item"],
                name="%(app_label)s_season_unique_tv_item",
            ),
        ]

    def __str__(self):
        """Return the title of the media and season number."""
        return f"{self.item.title} S{self.item.season_number}"

    @tracker  # postpone field reset until after the save
    def save(self, *args, **kwargs):
        """Save the media instance."""
        # if related_tv is not set
        if self.related_tv_id is None:
            self.related_tv = self.get_tv()

        super(Media, self).save(*args, **kwargs)

        if self.tracker.has_changed("status"):
            if self.status == Status.COMPLETED.value:
                season_metadata = providers.services.get_media_metadata(
                    MediaTypes.SEASON.value,
                    self.item.media_id,
                    self.item.source,
                    [self.item.season_number],
                )
                current_date = timezone.localdate()
                target_status = self.get_completion_status(
                    season_metadata,
                    unreleased_only_status=Status.IN_PROGRESS.value,
                    current_date=current_date,
                )
                episodes_to_create = self.get_remaining_eps(
                    season_metadata,
                    current_date=current_date,
                )
                if episodes_to_create:
                    bulk_create_with_history(
                        episodes_to_create,
                        Episode,
                    )
                    created_episodes_count = len(episodes_to_create)
                    episode_label = (
                        "episode" if created_episodes_count == 1 else "episodes"
                    )
                    self.create_user_message(
                        f"had {created_episodes_count} released {episode_label} "
                        "marked as watched automatically.",
                        level=UserMessageLevel.INFO,
                    )

                if target_status == Status.COMPLETED.value:
                    self.related_tv._handle_completed_season(
                        self.item.season_number,
                    )
                else:
                    self.status = target_status
                    bulk_update_with_history(
                        [self],
                        Season,
                        fields=["status"],
                    )
                    self.create_user_message(
                        "was left in progress because unreleased episodes remain.",
                        level=UserMessageLevel.WARNING,
                    )

                    if self.related_tv.status != Status.IN_PROGRESS.value:
                        self.related_tv.status = Status.IN_PROGRESS.value
                        bulk_update_with_history(
                            [self.related_tv],
                            TV,
                            fields=["status"],
                        )

            elif (
                self.status == Status.DROPPED.value
                and self.related_tv.status != Status.DROPPED.value
                and self.item.season_number != 0
            ):
                self.related_tv.status = Status.DROPPED.value
                bulk_update_with_history(
                    [self.related_tv],
                    TV,
                    fields=["status"],
                )

            elif (
                self.status == Status.IN_PROGRESS.value
                and self.related_tv.status != Status.IN_PROGRESS.value
            ):
                self.related_tv.status = Status.IN_PROGRESS.value
                bulk_update_with_history(
                    [self.related_tv],
                    TV,
                    fields=["status"],
                )

            self.item.fetch_releases(delay=True)

    def _get_latest_watched_episode_number(self):
        """Return the highest watched episode number for the season."""
        if self.pk is None:
            return 0

        latest_watched_ep_num = Episode.objects.filter(related_season=self).aggregate(
            latest_watched_ep_num=Max("item__episode_number"),
        )["latest_watched_ep_num"]

        return latest_watched_ep_num or 0

    def get_completion_status(
        self,
        season_metadata,
        unreleased_only_status,
        current_date,
    ):
        """Return the season status after completing all already released episodes."""
        latest_watched_ep_num = self._get_latest_watched_episode_number()
        released_remaining_exists = False
        unreleased_remaining_exists = False

        for episode in season_metadata["episodes"]:
            if episode["episode_number"] <= latest_watched_ep_num:
                continue

            if app.helpers.is_released_date(episode.get("air_date"), current_date):
                released_remaining_exists = True
            else:
                unreleased_remaining_exists = True

        if not unreleased_remaining_exists:
            return Status.COMPLETED.value

        if latest_watched_ep_num > 0 or released_remaining_exists:
            return Status.IN_PROGRESS.value

        return unreleased_only_status

    @property
    def progress(self):
        """Return the current episode number of the season."""
        episodes = self.episodes.all()
        if not episodes:
            return 0

        if self.status == Status.IN_PROGRESS.value:
            # Sort by most recently watched, then by episode number
            sorted_episodes = sorted(
                episodes,
                key=lambda e: (
                    e.end_date is not None,
                    e.end_date.timestamp() if e.end_date else 0,
                    e.item.episode_number,
                ),
                reverse=True,
            )
        else:
            # Default sorting by episode_number
            sorted_episodes = sorted(
                episodes,
                key=lambda e: -e.item.episode_number,
            )

        return sorted_episodes[0].item.episode_number

    @property
    def progressed_at(self):
        """Return the date when the last episode was watched."""
        dates = [
            episode.end_date
            for episode in self.episodes.all()
            if episode.end_date is not None
        ]
        return max(dates) if dates else None

    @property
    def start_date(self):
        """Return the date of the first episode watched."""
        dates = [
            episode.end_date
            for episode in self.episodes.all()
            if episode.end_date is not None
        ]
        return min(dates) if dates else None

    @property
    def end_date(self):
        """Return the date of the last episode watched."""
        dates = [
            episode.end_date
            for episode in self.episodes.all()
            if episode.end_date is not None
        ]
        return max(dates) if dates else None

    def increase_progress(self):
        """Watch the next episode of the season."""
        season_metadata = providers.services.get_media_metadata(
            MediaTypes.SEASON.value,
            self.item.media_id,
            self.item.source,
            [self.item.season_number],
        )
        episodes = season_metadata["episodes"]

        if self.progress == 0:
            # start watching from the first episode
            next_episode_number = episodes[0]["episode_number"]
        else:
            next_episode_number = providers.tmdb.find_next_episode(
                self.progress,
                episodes,
            )

        now = timezone.now().replace(second=0, microsecond=0)

        if next_episode_number:
            self.watch(next_episode_number, now)
        else:
            logger.info("No more episodes to watch.")

    def watch(self, episode_number, end_date, notes=""):
        """Create or add a repeat to an episode of the season."""
        item = self.get_episode_item(episode_number)

        episode = Episode.objects.create(
            related_season=self,
            item=item,
            end_date=end_date,
            notes=notes,
        )
        logger.info(
            "%s created successfully.",
            episode,
        )

    def decrease_progress(self):
        """Unwatch the current episode of the season."""
        self.unwatch(self.progress)

    def unwatch(self, episode_number):
        """Unwatch the episode instance."""
        item = self.get_episode_item(episode_number)

        episodes = Episode.objects.filter(
            related_season=self,
            item=item,
        ).order_by("-end_date")

        episode = episodes.first()

        if episode is None:
            logger.warning(
                "Episode %s does not exist.",
                self.item,
            )
            return

        # Get count before deletion for logging
        remaining_count = episodes.count() - 1

        episode.delete()
        logger.info(
            "Deleted %s S%02dE%02d (%d remaining instances)",
            self.item.title,
            self.item.season_number,
            episode_number,
            remaining_count,
        )

    def get_tv(self):
        """Get related TV instance for a season and create it if it doesn't exist."""
        try:
            tv = TV.objects.get(
                item__media_id=self.item.media_id,
                item__media_type=MediaTypes.TV.value,
                item__season_number=None,
                item__source=self.item.source,
                user=self.user,
            )
        except TV.DoesNotExist:
            tv_metadata = providers.services.get_media_metadata(
                MediaTypes.TV.value,
                self.item.media_id,
                self.item.source,
            )

            # creating tv with multiple seasons from a completed season
            if (
                self.status == Status.COMPLETED.value
                and tv_metadata["details"]["seasons"] > 1
            ):
                status = Status.IN_PROGRESS.value
            else:
                status = self.status

            item, _ = Item.objects.get_or_create(
                media_id=self.item.media_id,
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
                defaults={
                    "title": tv_metadata["title"],
                    "image": tv_metadata["image"],
                },
            )

            tv = TV(
                item=item,
                score=None,
                status=status,
                notes="",
                user=self.user,
            )

            # save_base to avoid custom save method
            TV.save_base(tv)

            logger.info("%s did not exist, it was created successfully.", tv)

        return tv

    def get_remaining_eps(
        self,
        season_metadata,
        current_date,
    ):
        """Return episodes needed to complete a season."""
        latest_watched_ep_num = self._get_latest_watched_episode_number()
        episodes_to_create = []

        # Calculate current time once before the loop
        now = timezone.now().replace(second=0, microsecond=0)

        # Create Episode objects for the remaining episodes
        for episode in reversed(season_metadata["episodes"]):
            if episode["episode_number"] <= latest_watched_ep_num:
                break

            if not app.helpers.is_released_date(
                episode.get("air_date"),
                current_date,
            ):
                continue

            item = self.get_episode_item(episode["episode_number"], season_metadata)

            # Resolve end_date based on user preference
            end_date = self.user.resolve_watch_date(now, episode.get("air_date"))

            episode_db = Episode(
                related_season=self,
                item=item,
                end_date=end_date,
            )
            episodes_to_create.append(episode_db)

        return episodes_to_create

    def get_episode_item(self, episode_number, season_metadata=None):
        """Get the episode item instance, create it if it doesn't exist."""
        if not season_metadata:
            season_metadata = providers.services.get_media_metadata(
                MediaTypes.SEASON.value,
                self.item.media_id,
                self.item.source,
                [self.item.season_number],
            )

        image = settings.IMG_NONE
        for episode in season_metadata["episodes"]:
            if episode["episode_number"] == int(episode_number):
                if episode.get("still_path"):
                    image = (
                        f"https://image.tmdb.org/t/p/original{episode['still_path']}"
                    )
                elif "image" in episode:
                    # for manual seasons
                    image = episode["image"]
                else:
                    image = settings.IMG_NONE
                break

        item, _ = Item.objects.get_or_create(
            media_id=self.item.media_id,
            source=self.item.source,
            media_type=MediaTypes.EPISODE.value,
            season_number=self.item.season_number,
            episode_number=episode_number,
            defaults={
                "title": self.item.title,
                "image": image,
            },
        )

        return item


class Episode(models.Model):
    """Model for episodes of a season."""

    history = HistoricalRecords(
        cascade_delete_history=True,
        excluded_fields=["item", "related_season", "created_at"],
    )

    created_at = models.DateTimeField(auto_now_add=True)
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    related_season = models.ForeignKey(
        Season,
        on_delete=models.CASCADE,
        related_name="episodes",
    )
    end_date = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        """Meta options for the model."""

        ordering = [
            "related_season",
            "item__episode_number",
            "-end_date",
            "-created_at",
        ]

    def __str__(self):
        """Return the season and episode number."""
        return self.item.__str__()

    def save(self, *args, **kwargs):
        """Save the episode instance."""
        super().save(*args, **kwargs)

        season_number = self.item.season_number
        tv_with_seasons_metadata = providers.services.get_media_metadata(
            "tv_with_seasons",
            self.item.media_id,
            self.item.source,
            [season_number],
        )
        # A source without per-season data (Douban) returns the show alone, so
        # the episode count falls back to what the season already holds. Without
        # this every episode save raised a KeyError and the whole request 500ed.
        season_metadata = tv_with_seasons_metadata.get(f"season/{season_number}")
        if season_metadata is None:
            season_metadata = {
                "episodes": [
                    {"episode_number": episode.item.episode_number}
                    for episode in self.related_season.episodes.all()
                ],
            }
        max_progress = len(season_metadata["episodes"])

        # clear prefetch cache to get the updated episodes
        self.related_season.refresh_from_db()

        is_finale = self.item.episode_number == max_progress
        season_just_completed = False
        if is_finale:
            if self.related_season.status != Status.COMPLETED.value:
                self.related_season.status = Status.COMPLETED.value
                bulk_update_with_history(
                    [self.related_season],
                    Season,
                    fields=["status"],
                )
                season_just_completed = True
                self.related_season.create_user_message(
                    "was marked as completed automatically.",
                    level=UserMessageLevel.SUCCESS,
                )

        elif self.related_season.status != Status.IN_PROGRESS.value:
            self.related_season.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [self.related_season],
                Season,
                fields=["status"],
            )

        if season_just_completed:
            self.related_season.related_tv._handle_completed_season(season_number)
        elif (
            not is_finale
            and self.related_season.related_tv.status != Status.IN_PROGRESS.value
        ):
            self.related_season.related_tv.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [self.related_season.related_tv],
                TV,
                fields=["status"],
            )

    def delete(self, *args, **kwargs):
        """Delete the episode instance and update parent statuses if needed."""
        season = self.related_season
        tv = season.related_tv
        deleted_episode_number = self.item.episode_number

        super().delete(*args, **kwargs)

        self._update_parent_statuses_after_delete(season, tv, deleted_episode_number)

    def _update_parent_statuses_after_delete(self, season, tv, deleted_episode_number):
        """Move completed parents back to in progress after unwatching progress."""
        season.refresh_from_db()
        tv.refresh_from_db()

        if (
            season.status == Status.COMPLETED.value
            and season.progress < deleted_episode_number
        ):
            season.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [season],
                Season,
                fields=["status"],
                default_user=season.user,
            )

        if (
            season.status != Status.COMPLETED.value
            and tv.status == Status.COMPLETED.value
        ):
            tv.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [tv],
                TV,
                fields=["status"],
                default_user=season.user,
            )


class Manga(Media):
    """Model for manga."""

    tracker = FieldTracker()


class Anime(Media):
    """Model for anime."""

    tracker = FieldTracker()


class Movie(Media):
    """Model for movies."""

    tracker = FieldTracker()

    # Running time in minutes. Null when the provider does not report it.
    runtime = models.PositiveIntegerField(null=True, blank=True)


class Game(Media):
    """Model for games."""

    tracker = FieldTracker()

    # Steam achievements are fetched separately from the playtime import, so
    # they are kept on the game itself and never require an API call to show.
    achievements_total = models.PositiveIntegerField(default=0)
    achievements_unlocked = models.PositiveIntegerField(default=0)
    achievements_synced_at = models.DateTimeField(null=True, blank=True)

    # 连续失败次数：每同步一次拿不到成就就 +1，拿到就归零。
    # 用途是给补漏任务一个止损线 —— 同一个 appid 连试 10 次都空手而归，
    # 说明 Steam 那边确实没数据（403 私有 / 没有成就系统），再试也是白跑。
    achievement_sync_failures = models.PositiveSmallIntegerField(default=0)

    # Per-aspect ratings imported from Notion: each is -10..10, except visual
    # which cannot go below 0. They are separate from the overall score.
    story = models.DecimalField(
        null=True,
        blank=True,
        max_digits=3,
        decimal_places=1,
        validators=[
            DecimalValidator(3, 1),
            MinValueValidator(-10),
            MaxValueValidator(10),
        ],
    )
    mechanic = models.DecimalField(
        null=True,
        blank=True,
        max_digits=3,
        decimal_places=1,
        validators=[
            DecimalValidator(3, 1),
            MinValueValidator(-10),
            MaxValueValidator(10),
        ],
    )
    visual = models.DecimalField(
        null=True,
        blank=True,
        max_digits=3,
        decimal_places=1,
        validators=[
            DecimalValidator(3, 1),
            MinValueValidator(0),
            MaxValueValidator(10),
        ],
    )

    # True = all achievements / full collection done, False = only finished the
    # story. Both keep status Completed, matching the three-tier library view.
    is_all_achievements = models.BooleanField(default=False)

    # True when end_date was interpolated instead of coming from real data.
    completion_estimated = models.BooleanField(default=False)

    # Steam review percentage, kept apart from score on purpose.
    steam_review_pct = models.IntegerField(null=True, blank=True)

    @property
    def achievement_percent(self):
        """Return the unlocked share of the achievements, None when unknown."""
        if not self.achievements_total:
            return None
        return round(self.achievements_unlocked * 100 / self.achievements_total)

    @property
    def formatted_progress(self):
        """Return progress in hours:minutes format."""
        return app.helpers.minutes_to_hhmm(self.progress)

    def increase_progress(self):
        """Increase the progress of the media by 30 minutes."""
        self.progress += 30
        self.save()
        logger.info("Changed playtime of %s to %s", self, self.formatted_progress)

    def decrease_progress(self):
        """Decrease the progress of the media by 30 minutes."""
        self.progress -= 30
        self.save()
        logger.info("Changed playtime of %s to %s", self, self.formatted_progress)


class Book(Media):
    """Model for books."""

    tracker = FieldTracker()

    # Total reading time in seconds, accumulated from WeRead sessions.
    reading_seconds = models.PositiveIntegerField(default=0)


class Highlight(models.Model):
    """One passage highlighted inside a book.

    Imported from WeRead, so the fields mirror what its API returns: the raw
    ``range`` string plus the chapter and the quoted text.
    """

    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="highlights",
    )
    page = models.PositiveIntegerField(null=True, blank=True)
    chapter = models.CharField(max_length=255, blank=True, default="")
    text = models.TextField(blank=True, default="")
    # The untouched range string from WeRead, kept for re-syncing later.
    range = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Meta options for the model."""

        ordering = ["-created_at"]

    def __str__(self):
        """Return the item title plus a snippet of the highlighted text."""
        snippet = self.text[:40] or self.chapter or "-"
        return f"{self.item.title}: {snippet}"


class Note(models.Model):
    """A reading note written on an item, optionally quoting a highlight."""

    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="item_notes",
    )
    content = models.TextField(blank=True, default="")
    # The original highlighted passage this note was written against.
    abstract = models.TextField(blank=True, default="")
    chapter = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Meta options for the model."""

        ordering = ["-created_at"]

    def __str__(self):
        """Return the item title plus a snippet of the note body."""
        snippet = self.content[:40] or self.abstract[:40] or "-"
        return f"{self.item.title}: {snippet}"


class Comic(Media):
    """Model for comics."""

    tracker = FieldTracker()


class BoardGame(Media):
    """Model for board games."""

    tracker = FieldTracker()


class ImportSources(models.TextChoices):
    """Where a bulk import task pulls its rows from."""

    NOTION = "notion", "Notion"
    STEAM = "steam", "Steam"
    MANUAL = "manual", "Manual"
    OTHER = "other", "Other"


class ImportTaskTypes(models.TextChoices):
    """What a bulk import task does."""

    NOTION_LIBRARY = "notion_library", "Notion library"
    STEAM_LIBRARY = "steam_library", "Steam library"
    STEAM_ACHIEVEMENTS = "steam_achievements", "Steam achievements"
    CN_TITLE_BACKFILL = "cn_title_backfill", "Chinese title backfill"
    OTHER = "other", "Other"


class ImportJobStatuses(models.TextChoices):
    """Lifecycle of one bulk import task."""

    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"


class ImportItemStatuses(models.TextChoices):
    """Outcome of importing a single source row."""

    MATCHED = "matched", "Matched"
    UNMATCHED = "unmatched", "Unmatched"
    CREATED = "created", "Created"
    FAILED = "failed", "Failed"


class ImportMatchMethods(models.TextChoices):
    """How a source row was tied to a local item."""

    EXACT = "exact", "Exact"
    FUZZY = "fuzzy", "Fuzzy"
    STORE_SEARCH = "store_search", "Store search"
    MANUAL = "manual", "Manual"


class ImportJob(models.Model):
    """One long running bulk import, resumable via ``cursor``.

    The row is written once when the import starts and updated as it goes, so
    an interrupted run (Notion's 288 rows, a full Steam sync, ...) can be
    picked up again from its own bookkeeping instead of from scratch.
    """

    source = models.CharField(
        max_length=20,
        choices=ImportSources,
        default=ImportSources.OTHER,
    )
    task_type = models.CharField(
        max_length=32,
        choices=ImportTaskTypes,
        default=ImportTaskTypes.OTHER,
    )
    status = models.CharField(
        max_length=12,
        choices=ImportJobStatuses,
        default=ImportJobStatuses.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # Free form counters (total / matched / unmatched / created / failed ...)
    # kept in JSON so验收 can read them without schema churn.
    stats = models.JSONField(default=dict, blank=True)
    # Where to resume: last page, last id, offset, whatever the importer needs.
    cursor = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")

    class Meta:
        """Meta options for the model."""

        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["status", "-created_at"],
                name="app_iimportjob_status_idx",
            ),
            models.Index(
                fields=["source", "task_type"],
                name="app_iimportjob_src_type_idx",
            ),
        ]

    def __str__(self):
        """Return a short label of the job."""
        return f"{self.get_task_type_display()} #{self.pk} [{self.status}]"

    def mark_running(self, save: bool = True):
        """Flag the job as running and stamp the start time."""
        self.status = ImportJobStatuses.RUNNING
        if self.started_at is None:
            self.started_at = timezone.now()
        if save:
            self.save(update_fields=["status", "started_at"])
        return self

    def mark_done(self, stats: dict | None = None, save: bool = True):
        """Flag the job as done, optionally replacing the stats."""
        self.status = ImportJobStatuses.DONE
        self.finished_at = timezone.now()
        if stats is not None:
            self.stats = stats
        if save:
            self.save(update_fields=["status", "finished_at", "stats"])
        return self

    def mark_failed(self, message: str, save: bool = True):
        """Flag the job as failed and store the error text."""
        self.status = ImportJobStatuses.FAILED
        self.finished_at = timezone.now()
        self.error = message or ""
        if save:
            self.save(update_fields=["status", "finished_at", "error"])
        return self


class ImportItem(models.Model):
    """Result of importing one source row into the local library."""

    job = models.ForeignKey(
        ImportJob,
        on_delete=models.CASCADE,
        related_name="items",
    )
    # The id on the source side, e.g. a Notion page id or a Steam appid.
    external_id = models.CharField(max_length=200, blank=True, default="")
    # Human readable title as the source spelled it, handy for reports.
    external_title = models.CharField(max_length=300, blank=True, default="")
    item = models.ForeignKey(
        Item,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="import_items",
    )
    status = models.CharField(
        max_length=12,
        choices=ImportItemStatuses,
        default=ImportItemStatuses.UNMATCHED,
    )
    match_method = models.CharField(
        max_length=20,
        choices=ImportMatchMethods,
        blank=True,
        default="",
    )
    note = models.TextField(blank=True, default="")
    # The untouched source row, so a bad mapping can be debugged later.
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Meta options for the model."""

        ordering = ["job", "id"]
        indexes = [
            models.Index(
                fields=["job", "status"],
                name="app_iitem_job_status_idx",
            ),
            models.Index(
                fields=["job", "external_id"],
                name="app_iitem_job_extid_idx",
            ),
        ]
        constraints = [
            # One row per source record inside a job, so a resume never duplicates.
            models.UniqueConstraint(
                fields=["job", "external_id"],
                condition=~models.Q(external_id=""),
                name="app_iitem_job_extid_unique",
            ),
        ]

    def __str__(self):
        """Return the source id and the outcome."""
        label = self.external_title or self.external_id or "-"
        return f"{label} [{self.status}]"


class AICopy(models.Model):
    """AI 生成的一句话总结与金句。与真实书摘（Highlight/Note）完全隔离。"""

    item = models.OneToOneField(
        "Item",
        on_delete=models.CASCADE,
        related_name="ai_copy",
        primary_key=True,
    )
    summary = models.CharField(max_length=200, blank=True, default="")
    quote = models.CharField(max_length=200, blank=True, default="")
    note = models.CharField(max_length=300, blank=True, default="")
    confidence = models.CharField(max_length=20, blank=True, default="")
    batch = models.CharField(max_length=20, blank=True, default="")
    reviewed = models.BooleanField(default=False)
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "AI copy"
        verbose_name_plural = "AI copies"

    def __str__(self):
        return f"{self.item.title} — {self.quote[:20]}"

class PlaytimeSnapshot(models.Model):
    """每个游戏每个月一条累计时长快照，相邻两条的差值就是当月游玩时长。

    为什么要这张表：Steam 只给「累计时长」这个数字，历史的每日游玩记录
    本站从来没有存过（导入的 historical 记录全是同一时刻的批量快照，
    时间戳是假的）。所以月度时长只能靠「从现在开始，每月存一次累计值，
    下个月减去这个月」这种只向前积累的方式得到，无法事后补算。
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="playtime_snapshots",
    )
    game = models.ForeignKey(
        Game,
        on_delete=models.CASCADE,
        related_name="playtime_snapshots",
    )
    captured_on = models.DateField(
        "快照日期",
        help_text="统一用当月 1 号，方便按月聚合。",
    )
    playtime_minutes = models.PositiveIntegerField(
        "累计游玩分钟数",
        default=0,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Meta options for the model."""

        verbose_name = "playtime snapshot"
        verbose_name_plural = "playtime snapshots"
        ordering = ["-captured_on", "game_id"]
        constraints = [
            UniqueConstraint(
                fields=["user", "game", "captured_on"],
                name="unique_playtime_snapshot_per_user_game_month",
            ),
        ]

    def __str__(self):
        """Return the game and month this snapshot belongs to."""
        return f"{self.game.item.title} @ {self.captured_on:%Y-%m} = {self.playtime_minutes}min"


class PlaytimeDaily(models.Model):
    """每个游戏每天一条累计时长快照，用来算「本周玩了多久」这类短期增量。

    为什么要单独一张日表：Steam 只给「累计时长」这一个数字，本站从来没存过
    逐日的游玩记录（导入进来的 historical 记录是同一时刻的批量快照，时间戳
    不可信，用它会算出大量假增量）。月度表 PlaytimeSnapshot 粒度太粗，
    要算「最近 7 天」就必须有逐日的累计值。

    口径：某天的增量 = 当天这条的累计分钟数 - 「7 天前或更早的最近一条」的累计
    分钟数。两个数字来自同一张表、同一套采集逻辑，所以相减是自洽的。
    副作用是站点上线后要攒够 7 天，才有第一条合法基线 —— 在那之前任何
    「本周增长」都是编的，调用方必须降级显示，不能用累计值冒充。
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="playtime_daily",
    )
    game = models.ForeignKey(
        Game,
        on_delete=models.CASCADE,
        related_name="playtime_daily",
    )
    captured_on = models.DateField(
        "快照日期",
        help_text="自然日，一天一条，靠唯一约束保证重跑不重复。",
    )
    playtime_minutes = models.IntegerField(
        "累计游玩分钟数",
        default=0,
        help_text="这一天的累计值（不是当天增量）。",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Meta options for the model."""

        verbose_name = "playtime daily snapshot"
        verbose_name_plural = "playtime daily snapshots"
        ordering = ["-captured_on", "game_id"]
        constraints = [
            UniqueConstraint(
                fields=["user", "game", "captured_on"],
                name="unique_playtime_daily_per_user_game_day",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user", "captured_on"],
                name="playtime_daily_user_day_idx",
            ),
        ]

    def __str__(self):
        """Return the game and day this snapshot belongs to."""
        return f"{self.game.item.title} @ {self.captured_on:%Y-%m-%d} = {self.playtime_minutes}min"
