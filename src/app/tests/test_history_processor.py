from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from app import config
from app.history_processor import (
    _fetch_prev_records,
    format_description,
    process_history_entry,
)
from app.models import HistoricalMovie, Item, MediaTypes, Movie, Sources, Status


class HistoryProcessorTests(TestCase):
    """Test the history processor functions."""

    def test_get_verb_covers_all_media_types(self):
        """Test that get_verb covers all media types defined in MediaTypes."""
        # Get all media types from the MediaTypes enum
        for media_type in MediaTypes:
            # Ensure both present and past tense verbs are defined
            try:
                config.get_verb(media_type.value, past_tense=False)
                config.get_verb(media_type.value, past_tense=True)
            except KeyError:
                self.fail(f"Media type {media_type.name} not defined in get_verb")

    def test_format_description_status_initial(self):
        """Test format_description for initial status changes."""
        # Test initial status settings
        self.assertEqual(
            format_description(
                "status",
                None,
                Status.IN_PROGRESS.value,
                MediaTypes.TV.value,
            ),
            "Marked as currently watching",
        )
        self.assertEqual(
            format_description(
                "status",
                None,
                Status.COMPLETED.value,
                MediaTypes.MANGA.value,
            ),
            "Marked as finished reading",
        )
        self.assertEqual(
            format_description(
                "status",
                None,
                Status.PLANNING.value,
                MediaTypes.GAME.value,
            ),
            "Added to playing list",
        )
        self.assertEqual(
            format_description(
                "status",
                None,
                Status.DROPPED.value,
                MediaTypes.BOOK.value,
            ),
            "Marked as dropped",
        )
        self.assertEqual(
            format_description(
                "status",
                None,
                Status.PAUSED.value,
                MediaTypes.ANIME.value,
            ),
            "Marked as paused watching",
        )

    def test_format_description_status_transitions(self):
        """Test format_description for status transitions."""
        # Test status transitions
        self.assertEqual(
            format_description(
                "status",
                Status.PLANNING.value,
                Status.IN_PROGRESS.value,
                MediaTypes.TV.value,
            ),
            "Currently watching",
        )
        self.assertEqual(
            format_description(
                "status",
                Status.IN_PROGRESS.value,
                Status.COMPLETED.value,
                MediaTypes.MANGA.value,
            ),
            "Finished reading",
        )
        self.assertEqual(
            format_description(
                "status",
                Status.IN_PROGRESS.value,
                Status.PAUSED.value,
                MediaTypes.GAME.value,
            ),
            "Paused playing",
        )
        self.assertEqual(
            format_description(
                "status",
                Status.PAUSED.value,
                Status.IN_PROGRESS.value,
                MediaTypes.BOOK.value,
            ),
            "Resumed reading",
        )
        self.assertEqual(
            format_description(
                "status",
                Status.IN_PROGRESS.value,
                Status.DROPPED.value,
                MediaTypes.ANIME.value,
            ),
            "Stopped watching",
        )
        self.assertEqual(
            format_description("status", "Custom1", "Custom2", MediaTypes.TV.value),
            "Changed status from Custom1 to Custom2",
        )

    def test_format_description_score(self):
        """Test format_description for score changes."""
        # Initial score
        self.assertEqual(
            format_description("score", None, 8.5, MediaTypes.TV.value),
            "Rated 8.5/10",
        )
        self.assertEqual(
            format_description("score", 0, 7.0, MediaTypes.ANIME.value),
            "Rated 7.0/10",
        )
        # Score change
        self.assertEqual(
            format_description("score", 6.5, 8.0, MediaTypes.MOVIE.value),
            "Changed rating from 6.5 to 8.0",
        )

    def test_format_description_progress(self):
        """Test format_description for progress changes."""
        # Initial progress
        self.assertEqual(
            format_description("progress", None, 120, MediaTypes.GAME.value),
            "Played for 2h 00min",
        )
        self.assertEqual(
            format_description("progress", None, 5, MediaTypes.BOOK.value),
            "Read up to page 5",
        )
        self.assertEqual(
            format_description("progress", None, 10, MediaTypes.MANGA.value),
            "Read up to chapter 10",
        )

        # Progress change
        self.assertEqual(
            format_description("progress", 60, 90, MediaTypes.GAME.value),
            "Added 30min of playtime",
        )
        self.assertEqual(
            format_description("progress", 90, 60, MediaTypes.GAME.value),
            "Removed 30min of playtime",
        )
        self.assertEqual(
            format_description("progress", 10, 15, MediaTypes.BOOK.value),
            "Progress set to 15 pages",
        )
        self.assertEqual(
            format_description("progress", 5, 10, MediaTypes.MANGA.value),
            "Progress set to 10 chapters",
        )

    def test_format_description_notes(self):
        """Test format_description for notes changes."""
        # Initial notes
        self.assertEqual(
            format_description("notes", None, "Test notes"),
            "Added notes",
        )

        # Update notes
        self.assertEqual(
            format_description("notes", "Old notes", "New notes"),
            "Updated notes",
        )

        # Remove notes
        self.assertEqual(
            format_description("notes", "Old notes", ""),
            "Removed notes",
        )

    def test_format_description_generic(self):
        """Test format_description for generic field changes."""
        self.assertEqual(
            format_description("custom_field", "old", "new"),
            "Updated custom field from old to new",
        )


class FetchPrevRecordsSameTimestampTests(TestCase):
    """Predecessor lookup must work when two rows share one ``history_date``.

    Two saves can land inside the same millisecond, so their historical rows
    carry an identical ``history_date``. Matching only on
    ``history_date__lt`` then finds no predecessor and the journal falls back to
    the creation wording ("Marked as finished watching" instead of "Finished
    watching"). The tie-break on ``history_id`` (auto-increment, so the later
    write is larger) has to resolve it instead.
    """

    def setUp(self):
        """Create a user and an item to attach media rows to."""
        self.credentials = {"username": "sametimestamp", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )
        # Saving a media row asks the provider for metadata and schedules a
        # calendar reload. This suite is offline and none of that is part of the
        # predecessor logic under test, so both are stubbed out.
        metadata = mock.patch(
            "app.providers.services.get_media_metadata",
            return_value={
                "max_progress": 1,
                "title": "Test Movie",
                "image": "http://example.com/image.jpg",
            },
        )
        metadata.start()
        self.addCleanup(metadata.stop)
        releases = mock.patch.object(
            Item,
            "fetch_releases",
            lambda self, delay=False: None,  # noqa: ARG005
        )
        releases.start()
        self.addCleanup(releases.stop)

    def _create_movie(self):
        """Create the tracked movie the historical rows describe.

        Called from each test before the rows are written, because the rows need
        the movie's id and a single shared timestamp.
        """
        self.movie = Movie.objects.create(
            item=self.item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )
        self.movie_id = self.movie.id
        # Created with the movie, so it is the earliest row of this object, and
        # it is exactly the row a second save inside the same millisecond
        # collides with -> all rows below share its history_date.
        creation = self.movie.history.first()
        self.creation_history_id = creation.history_id
        self.history_date = creation.history_date

    def _same_timestamp_rows(self):
        """Return two history rows for one movie sharing a ``history_date``.

        The rows are written directly instead of via ``save()`` so the identical
        timestamp is deterministic rather than depending on how fast the machine
        happens to be. ``history_id`` still comes from the table's auto-increment
        primary key, exactly like two real saves inside one millisecond: the row
        written second gets the larger id.
        """
        first = HistoricalMovie.objects.create(
            id=self.movie_id,
            score=None,
            status=Status.IN_PROGRESS.value,
            notes="",
            progress=0,
            history_date=self.history_date,
            history_type="+",
            history_user=self.user,
        )
        second = HistoricalMovie.objects.create(
            id=self.movie_id,
            score=None,
            status=Status.COMPLETED.value,
            notes="",
            progress=1,
            history_date=self.history_date,
            history_type="~",
            history_user=self.user,
        )
        return first, second

    def test_same_timestamp_row_gets_predecessor(self):
        """The later row with an equal date must resolve to the earlier row."""
        self._create_movie()
        first, second = self._same_timestamp_rows()
        self.assertEqual(first.history_date, second.history_date)
        self.assertLess(first.history_id, second.history_id)

        records = {
            (MediaTypes.MOVIE.value, first.history_id): first,
            (MediaTypes.MOVIE.value, second.history_id): second,
        }
        prev = _fetch_prev_records(records)

        # ``_fetch_prev_records`` re-reads the rows through its own query, so the
        # successor is a distinct instance of the same row: compare by id.
        self.assertEqual(
            prev[(MediaTypes.MOVIE.value, second.history_id)].history_id,
            first.history_id,
        )

    def test_same_timestamp_description_is_not_degraded(self):
        """The equal-date entry renders the real change, not creation wording."""
        self._create_movie()
        first, second = self._same_timestamp_rows()
        self.assertEqual(first.history_date, second.history_date)

        records = {(MediaTypes.MOVIE.value, r.history_id): r for r in (first, second)}
        prev_record = _fetch_prev_records(records).get(
            (MediaTypes.MOVIE.value, second.history_id),
        )
        self.assertIsNotNone(prev_record)

        processed = process_history_entry(
            (second, prev_record),
            MediaTypes.MOVIE.value,
            self.user,
        )
        descriptions = [change["description"] for change in processed["changes"]]
        # The transition is IN_PROGRESS -> COMPLETED, so the predecessor makes it
        # read as a real finish; without one it degrades to "Marked as ...".
        self.assertIn("Finished watching", descriptions)
        self.assertNotIn("Marked as finished watching", descriptions)

    def test_earlier_row_does_not_borrow_the_later_row(self):
        """The earlier of two equal-date rows never gets the later one as prev."""
        self._create_movie()
        first, second = self._same_timestamp_rows()
        records = {
            (MediaTypes.MOVIE.value, first.history_id): first,
            (MediaTypes.MOVIE.value, second.history_id): second,
        }
        prev = _fetch_prev_records(records)
        prev_of_first = prev.get((MediaTypes.MOVIE.value, first.history_id))
        # A predecessor must be strictly before the row; the row written later
        # in the same millisecond (larger history_id) never qualifies. The only
        # row before this one is the movie creation, which the tie-break must
        # not skip over.
        self.assertIsNotNone(prev_of_first)
        self.assertNotEqual(prev_of_first.history_id, second.history_id)
        self.assertEqual(prev_of_first.history_id, self.creation_history_id)
