"""Tests for the Douban reading list importer.

The HTTP calls are mocked: the importer is about the parsing and the status
mapping, not about reaching Douban from the test suite.
"""

import contextlib
import os
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from app.mixins import disable_fetch_releases
from app.models import Book, Item, MediaTypes, Sources, Status
from app.providers import douban_movie as douban_movie_provider
from app.providers import services
from integrations import tasks
from integrations.imports import douban, douban_movie, douban_tv, helpers, weread
from integrations.imports.helpers import MediaImportError

User = get_user_model()

SHELF_HTML = """
<html><body>
<div class="top-nav-info">
  <a href="https://accounts.douban.com/passport/login?source=book">登录/注册</a>
</div>
<ol class="grid_view">
  <li class="subject-item">
    <div class="pic">
      <a href="https://book.douban.com/subject/2567698/">
        <img src="https://img3.doubanio.com/view/subject/s/public/s2768378.jpg">
      </a>
    </div>
    <div class="info">
      <h2><a href="https://book.douban.com/subject/2567698/" title="三体">三体</a></h2>
      <span class="rating_nums">8.9</span>
    </div>
  </li>
  <li class="subject-item">
    <div class="pic">
      <a href="https://book.douban.com/subject/3066477/">
        <img src="https://img1.doubanio.com/view/subject/s/public/s1.jpg">
      </a>
    </div>
    <div class="info">
      <h2>
        <a href="https://book.douban.com/subject/3066477/" title="三体Ⅱ">三体Ⅱ</a>
      </h2>
      <span class="rating_nums">9.3</span>
    </div>
  </li>
</ol>
</body></html>
"""
EMPTY_PAGE_HTML = """
<html><body>
<div class="top-nav-info">
  <a href="https://accounts.douban.com/passport/login?source=book">登录/注册</a>
</div>
<ol class="grid_view"></ol>
</body></html>
"""

LOGIN_WALL_HTML = """
<html><body>
<form id="lzform" action="https://accounts.douban.com/j/mobile/login/basic"
      method="post">
  <input name="form_password" type="password">
</form>
</body></html>
"""

DETAIL_HTML = '<span property="v:itemreviewed">三体</span><div id="info"></div>'


def html_response(text, status_code=200):
    """Return a fake response carrying an HTML body."""
    response = MagicMock()
    response.content = text.encode()
    response.status_code = status_code
    response.raise_for_status.return_value = None
    return response


def _error_response(status_code):
    """Return a fake response with the given status code."""
    response = MagicMock()
    response.status_code = status_code
    response.text = "error"
    return response


def douban_metadata(*_args, **_kwargs):
    """Stand in for the detail page lookup of the importer."""
    return {
        "media_id": "2567698",
        "source": Sources.DOUBAN.value,
        "media_type": "book",
        "title": "三体",
        "max_progress": 302,
        "image": "/image_relay?url=https%3A%2F%2Fimg3.doubanio.com%2Fa.jpg",
    }


class ImporterTestCase(TestCase):
    """Shared setup: a fresh user and a clean cache."""

    def setUp(self):
        """Create the test user, clear the cache and skip the throttle."""
        cache.clear()
        self.user = User.objects.create_user(username="doubanreader")

        # The importer waits between pages to stay polite to Douban, a unit
        # test has nothing to be polite to
        sleep_patch = patch("integrations.imports.douban.time.sleep")
        self.mock_sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def run_importer(self, mode="new"):
        """Run the importer with the release lookups disabled."""
        with disable_fetch_releases():
            return douban.importer("ahbei", self.user, mode)


class ImportTests(ImporterTestCase):
    """The importer reads the shelves and stores the books."""

    @patch("integrations.imports.douban.services.get_media_metadata")
    @patch("integrations.imports.douban.requests.get")
    def test_books_are_imported(self, mock_shelf_get, mock_metadata):
        """Two entries of a shelf become two tracked books."""
        mock_shelf_get.return_value = html_response(SHELF_HTML)
        mock_metadata.side_effect = douban_metadata

        counts, warnings = self.run_importer()

        self.assertEqual(counts["book"], 2)
        self.assertEqual(warnings, "")
        self.assertEqual(Book.objects.filter(user=self.user).count(), 2)

    @patch("integrations.imports.douban.services.get_media_metadata")
    @patch("integrations.imports.douban.requests.get")
    def test_status_mapping_per_shelf(self, mock_shelf_get, mock_metadata):
        """Each shelf is imported with the status it stands for."""
        mock_shelf_get.return_value = html_response(SHELF_HTML)
        mock_metadata.side_effect = douban_metadata

        importer = douban.DoubanImporter("ahbei", self.user, "new")
        with disable_fetch_releases():
            for shelf, status in douban.SHELVES:
                books = importer._fetch_shelf(shelf)
                self.assertTrue(books, f"shelf {shelf} should have books")
                for book in books:
                    importer._process_book(book, status)
            importer._flush()

        by_media_id = {
            book.item.media_id: book.status
            for book in Book.objects.filter(user=self.user).select_related("item")
        }
        # The same two books appear on every shelf and the importer walks the
        # shelves in order, so the first shelf that holds a book decides its
        # status. That shelf is "collect", the books already read.
        self.assertEqual(set(by_media_id.values()), {Status.COMPLETED.value})

    @patch("integrations.imports.douban.services.get_media_metadata")
    @patch("integrations.imports.douban.requests.get")
    def test_media_id_is_the_douban_subject_id(self, mock_shelf_get, mock_metadata):
        """The Douban subject id is what identifies the book."""
        mock_shelf_get.return_value = html_response(SHELF_HTML)
        mock_metadata.side_effect = douban_metadata

        self.run_importer()

        media_ids = set(
            Item.objects.filter(source=Sources.DOUBAN.value).values_list(
                "media_id",
                flat=True,
            ),
        )
        self.assertEqual(media_ids, {"2567698", "3066477"})

    @patch("integrations.imports.douban.requests.get")
    def test_private_list_raises_a_clear_error(self, mock_shelf_get):
        """A login wall is reported as a private list, not as empty."""
        mock_shelf_get.return_value = html_response(LOGIN_WALL_HTML)

        with self.assertRaises(MediaImportError) as context:
            self.run_importer()

        self.assertIn("not public", str(context.exception))

    @patch("integrations.imports.douban.requests.get")
    def test_empty_first_page_is_not_an_error(self, mock_shelf_get):
        """A shelf without books imports nothing instead of failing."""
        mock_shelf_get.return_value = html_response(EMPTY_PAGE_HTML)

        counts, warnings = self.run_importer()

        self.assertEqual(counts["book"], 0)
        self.assertEqual(warnings, "")

    @patch("integrations.imports.douban.services.get_media_metadata")
    @patch("integrations.imports.douban.requests.get")
    def test_second_run_imports_nothing_in_new_mode(
        self,
        mock_shelf_get,
        mock_metadata,
    ):
        """Existing books are left alone in new mode."""
        mock_shelf_get.return_value = html_response(SHELF_HTML)
        mock_metadata.side_effect = douban_metadata

        self.run_importer()
        first = Book.objects.filter(user=self.user).count()
        counts, _ = self.run_importer()

        self.assertEqual(counts["book"], 0)
        self.assertEqual(Book.objects.filter(user=self.user).count(), first)

    @patch("integrations.imports.douban.requests.get")
    def test_pagination_walks_until_an_empty_page(self, mock_shelf_get):
        """Every shelf asks for page one, then stops on the empty page."""
        mock_shelf_get.return_value = html_response(EMPTY_PAGE_HTML)

        self.run_importer()

        self.assertEqual(mock_shelf_get.call_count, 3)

    @patch("integrations.imports.douban.services.get_media_metadata")
    @patch("integrations.imports.douban.requests.get")
    def test_detail_failure_falls_back_to_the_list_entry(
        self,
        mock_shelf_get,
        mock_metadata,
    ):
        """A book whose detail page fails is still imported."""
        mock_shelf_get.return_value = html_response(SHELF_HTML)
        mock_metadata.side_effect = services.ProviderAPIError(
            Sources.DOUBAN.value,
            requests.exceptions.HTTPError(response=_error_response(404)),
        )

        counts, _ = self.run_importer()

        self.assertEqual(counts["book"], 2)
        item = Item.objects.filter(source=Sources.DOUBAN.value).first()
        self.assertEqual(item.title, "三体")
        # The list cover is upgraded and relayed even without the detail page
        self.assertIn("/image_relay", item.image)

    @patch("integrations.imports.douban.services.get_media_metadata")
    @patch("integrations.imports.douban.requests.get")
    def test_rate_limit_stops_the_import(self, mock_shelf_get, mock_metadata):
        """A 403 from Douban ends the import with a message instead of looping."""
        mock_shelf_get.return_value = html_response(SHELF_HTML)
        mock_metadata.side_effect = services.ProviderAPIError(
            Sources.DOUBAN.value,
            requests.exceptions.HTTPError(response=_error_response(403)),
        )

        with self.assertRaises(MediaImportError) as context:
            self.run_importer()

        self.assertIn("rate limited", str(context.exception))


def douban_movie_importer(user, mode="new"):
    """Return an importer whose network calls never leave the test."""
    return douban_movie.DoubanMovieImporter("253426192", user, mode)


def douban_tv_importer(user, mode="new"):
    """Return a show importer whose network calls never leave the test."""
    return douban_tv.DoubanTVImporter("253426192", user, mode)


def network_error(message="Unable to connect to proxy"):
    """Return a provider error as it looks when the host cannot be reached."""
    return services.ProviderAPIError(
        Sources.DOUBAN.value,
        requests.exceptions.ConnectionError(message),
    )


def rate_limit_error():
    """Return a provider error carrying Douban's IP rate limit marker."""
    return services.ProviderAPIError(
        Sources.DOUBAN.value,
        requests.exceptions.HTTPError(response=_error_response(400)),
        douban_movie_provider.RATE_LIMIT_MARKER,
    )


def blocked_error():
    """Return a provider error as Douban's hard 403 block looks."""
    return services.ProviderAPIError(
        Sources.DOUBAN.value,
        requests.exceptions.HTTPError(response=_error_response(403)),
    )


class FailedCollectionTests(ImporterTestCase):
    """What a run that could not read the collection at all reports.

    Only the two limits a user can wait out keep the old "0 imported, here is
    the message" answer. Everything else imported nothing and must raise, so
    Celery marks the task FAILED instead of succeeded with an empty result.
    """

    def test_movies_network_error_raises(self):
        """A dead proxy or no network fails the run instead of reporting zero."""
        importer = douban_movie_importer(self.user)

        with patch.object(
            douban_movie.DoubanMovieImporter,
            "_fetch_interests",
            side_effect=network_error(),
        ), self.assertRaises(helpers.MediaImportNetworkError) as context:
            importer.import_data()

        message = str(context.exception)
        self.assertIn("proxy", message.lower())
        # The message has to be readable enough to say it is a network problem
        self.assertIn("网络", message)

    def test_movies_network_error_is_a_retryable_network_error(self):
        """The failure must be the type the Celery retry policy catches.

        Anything narrower - a plain ``MediaImportError``, an exception that
        inherits nothing in ``autoretry_for`` - fails the task for the day
        instead of trying again an hour later.
        """
        importer = douban_movie_importer(self.user)

        with patch.object(
            douban_movie.DoubanMovieImporter,
            "_fetch_interests",
            side_effect=network_error(),
        ), self.assertRaises(helpers.MediaImportNetworkError) as context:
            importer.import_data()

        # The retry of the task is wired through MediaImportUnexpectedError
        self.assertIsInstance(context.exception, helpers.MediaImportUnexpectedError)
        # ... and it must *not* be the type reserved for what the user fixes,
        # which would be retried an hour later to the same answer
        self.assertNotIsInstance(context.exception, MediaImportError)

    def test_movies_network_error_does_not_return_a_count(self):
        """The old behaviour returned ``({movie: 0}, msg)``; it must not."""
        importer = douban_movie_importer(self.user)

        with patch.object(
            douban_movie.DoubanMovieImporter,
            "_fetch_interests",
            side_effect=network_error(),
        ), contextlib.suppress(helpers.MediaImportNetworkError):
            result = importer.import_data()
            self.fail("a failed run must not also return a count, got " + repr(result))

    def test_movies_rate_limit_still_returns_a_message(self):
        """Douban's IP rate limit is the user's to wait out, so it is reported."""
        importer = douban_movie_importer(self.user)

        with patch.object(
            douban_movie.DoubanMovieImporter,
            "_fetch_interests",
            side_effect=rate_limit_error(),
        ):
            counts, warnings = importer.import_data()

        self.assertEqual(counts, {MediaTypes.MOVIE.value: 0})
        self.assertEqual(warnings, douban_movie_provider.RATE_LIMIT_MESSAGE)

    def test_movies_block_still_returns_a_message(self):
        """A hard 403 is also reported instead of failing the task."""
        importer = douban_movie_importer(self.user)

        with patch.object(
            douban_movie.DoubanMovieImporter,
            "_fetch_interests",
            side_effect=blocked_error(),
        ):
            counts, warnings = importer.import_data()

        self.assertEqual(counts, {MediaTypes.MOVIE.value: 0})
        self.assertEqual(warnings, douban_movie_provider.BLOCKED_MESSAGE)

    def test_tv_network_error_raises(self):
        """The show importer fails the run the same way."""
        importer = douban_tv_importer(self.user)

        with patch.object(
            douban_tv.DoubanTVImporter,
            "_fetch_interests",
            side_effect=network_error(),
        ), self.assertRaises(helpers.MediaImportNetworkError) as context:
            importer.import_data()

        self.assertIn("proxy", str(context.exception).lower())

    def test_tv_network_error_is_a_retryable_network_error(self):
        """The show importer raises the type the Celery retry policy catches."""
        importer = douban_tv_importer(self.user)

        with patch.object(
            douban_tv.DoubanTVImporter,
            "_fetch_interests",
            side_effect=network_error(),
        ), self.assertRaises(helpers.MediaImportNetworkError) as context:
            importer.import_data()

        self.assertIsInstance(context.exception, helpers.MediaImportUnexpectedError)
        self.assertNotIsInstance(context.exception, MediaImportError)

    def test_tv_rate_limit_still_returns_a_message(self):
        """The show importer keeps the old answer for a limit the user waits out."""
        importer = douban_tv_importer(self.user)

        with patch.object(
            douban_tv.DoubanTVImporter,
            "_fetch_interests",
            side_effect=rate_limit_error(),
        ):
            counts, warnings = importer.import_data()

        self.assertEqual(counts, {MediaTypes.TV.value: 0})
        self.assertEqual(warnings, douban_movie_provider.RATE_LIMIT_MESSAGE)

    def test_tv_block_still_returns_a_message(self):
        """A hard 403 on the show import is reported, not retried."""
        importer = douban_tv_importer(self.user)

        with patch.object(
            douban_tv.DoubanTVImporter,
            "_fetch_interests",
            side_effect=blocked_error(),
        ):
            counts, warnings = importer.import_data()

        self.assertEqual(counts, {MediaTypes.TV.value: 0})
        self.assertEqual(warnings, douban_movie_provider.BLOCKED_MESSAGE)


class RetryWiringTests(ImporterTestCase):
    """Prove the retry policy of the import tasks actually catches the failure.

    ``IMPORT_RETRY_KWARGS['autoretry_for']`` is the list Celery matches the
    raised exception against. A test that only checks the class *name*, or that
    the importer raises "some exception", would stay green while a network
    failure fell through the policy and the task was lost for the day. These
    tests compare the types themselves, so they go red when the wiring breaks.
    """

    def test_douban_network_error_is_caught_by_the_retry_policy(self):
        """The raised type is in, or is a subclass of, an ``autoretry_for`` one."""
        importer = douban_movie_importer(self.user)

        with patch.object(
            douban_movie.DoubanMovieImporter,
            "_fetch_interests",
            side_effect=network_error(),
        ), self.assertRaises(helpers.MediaImportNetworkError) as context:
            importer.import_data()

        retried = tasks.IMPORT_RETRY_KWARGS["autoretry_for"]

        self.assertTrue(
            any(
                issubclass(type(context.exception), retried_type)
                for retried_type in retried
            ),
            f"{type(context.exception).__name__} is not caught by {retried}",
        )

    def test_the_plain_import_error_stays_out_of_the_retry_policy(self):
        """A wrong key or a private profile must not be retried three times."""
        retried = tasks.IMPORT_RETRY_KWARGS["autoretry_for"]

        for retried_type in retried:
            self.assertFalse(
                issubclass(MediaImportError, retried_type),
                "MediaImportError is what the user has to fix, not a transient "
                "failure: retrying it an hour later repeats the same answer",
            )

    def test_weread_network_error_is_caught_by_the_retry_policy(self):
        """The WeRead gateway failure is wired the same way.

        ``WeReadNetworkError`` keeps ``WeReadError`` as a parent so the
        "a bonus call failed, carry on" handlers still work; this asserts the
        other parent is the one the retry policy matches on.
        """
        retried = tasks.IMPORT_RETRY_KWARGS["autoretry_for"]

        self.assertTrue(
            any(
                issubclass(weread.WeReadNetworkError, retried_type)
                for retried_type in retried
            ),
            f"WeReadNetworkError is not caught by {retried}",
        )
        # The two answers the user has to fix keep escaping the retry policy
        for not_retried in (weread.WeReadAuthError,):
            for retried_type in retried:
                self.assertFalse(issubclass(not_retried, retried_type))

    def test_weread_gateway_failure_raises_instead_of_reporting_zero(self):
        """A dead gateway is a failed run, not "the shelf was empty"."""
        importer = weread.WeReadImporter("a-key", self.user, "new")

        with patch.object(
            importer.session,
            "post",
            side_effect=requests.exceptions.ConnectionError("no route to host"),
        ), self.assertRaises(weread.WeReadNetworkError) as context:
            importer.import_data()

        self.assertNotIsInstance(context.exception, MediaImportError)
        self.assertIsInstance(context.exception, helpers.MediaImportUnexpectedError)


class ProxyFallbackTests(ImporterTestCase):
    """A configured proxy that is not running must not end the import."""

    PROXY = "http://127.0.0.1:7899"
    PROXY_ERROR = "Unable to connect to proxy"
    NETWORK_DOWN = "no route to host"

    def setUp(self):
        """Point ``DOUBAN_PROXY`` at a port nothing listens on."""
        super().setUp()
        douban_movie_provider._proxy_failed_at = 0.0
        douban_movie_provider._proxy_failed_value = ""
        proxy = patch.dict(os.environ, {"DOUBAN_PROXY": self.PROXY})
        proxy.start()
        self.addCleanup(proxy.stop)
        throttle = patch.object(douban_movie_provider, "throttle")
        throttle.start()
        self.addCleanup(throttle.stop)

    def getter(self, calls, *, proxy_error=True):
        """Return a fake ``requests.get`` recording the proxies it was given."""
        response = MagicMock()
        response.status_code = 200

        def fake_get(_url, **kwargs):
            calls.append(kwargs.get("proxies"))
            if kwargs.get("proxies") and proxy_error:
                raise requests.exceptions.ProxyError(self.PROXY_ERROR)
            if not kwargs.get("proxies") and not proxy_error:
                raise requests.exceptions.ConnectionError(self.NETWORK_DOWN)
            return response

        return fake_get

    def test_dead_proxy_falls_back_to_the_direct_connection(self):
        """The direct request is made after the proxy refuses, and logged."""
        calls = []
        fake_get = self.getter(calls)
        logs = douban_movie_provider.logger

        with (
            patch.object(douban_movie_provider.requests, "get", fake_get),
            self.assertLogs(logs, "WARNING") as captured,
        ):
            result = douban_movie_provider.get("https://m.douban.com/x")

        self.assertEqual(result.status_code, 200)
        # First through the proxy, then without it
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[1])
        self.assertIn("direct connection", " ".join(captured.output))

    def test_fallback_is_remembered(self):
        """The dead proxy is not hit again for the next request."""
        calls = []
        fake_get = self.getter(calls)

        with patch.object(douban_movie_provider.requests, "get", fake_get):
            douban_movie_provider.get("https://m.douban.com/1")
            douban_movie_provider.get("https://m.douban.com/2")

        # One failed proxy attempt in total: the second call went straight on
        self.assertEqual(calls, [{"http": self.PROXY, "https": self.PROXY}, None, None])

    def test_direct_failure_is_raised(self):
        """When the direct connection fails too, the error is not swallowed."""
        calls = []

        def fake_get(_url, **kwargs):
            calls.append(kwargs.get("proxies"))
            if kwargs.get("proxies"):
                raise requests.exceptions.ProxyError(self.PROXY_ERROR)
            raise requests.exceptions.ConnectionError(self.NETWORK_DOWN)

        with patch.object(
            douban_movie_provider.requests, "get", fake_get
        ), self.assertRaises(requests.exceptions.ConnectionError):
            douban_movie_provider.get("https://m.douban.com/x")

        # The proxy was tried first, the direct call failed and was not hidden
        self.assertEqual(len(calls), 2)

    def test_no_proxy_configured_is_a_plain_direct_call(self):
        """Without ``DOUBAN_PROXY`` nothing goes through a proxy."""
        calls = []
        response = MagicMock()
        response.status_code = 200

        def fake_get(_url, **kwargs):
            calls.append(kwargs.get("proxies"))
            return response

        with patch.dict(os.environ, {"DOUBAN_PROXY": ""}), patch.object(
            douban_movie_provider.requests,
            "get",
            fake_get,
        ):
            douban_movie_provider.get("https://m.douban.com/x")

        self.assertEqual(calls, [None])

        self.assertEqual(calls, [None])


class ParseEntryTests(ImporterTestCase):
    """The markup of a single list entry."""

    def test_entry_fields(self):
        """The title, id, rating and cover are read from an entry."""
        importer = douban.DoubanImporter("ahbei", self.user, "new")
        entry = douban.RE_SUBJECT_ITEM.findall(SHELF_HTML)[0]

        parsed = importer._parse_entry(entry)

        self.assertEqual(parsed["media_id"], "2567698")
        self.assertEqual(parsed["title"], "三体")
        self.assertEqual(parsed["score"], 8.9)
        self.assertIn("img3.doubanio.com", parsed["image"])

    def test_entry_without_rating(self):
        """An unrated book keeps an empty score instead of a zero."""
        importer = douban.DoubanImporter("ahbei", self.user, "new")
        entry = (
            '<li class="subject-item"><h2><a href="/subject/1/" '
            'title="无评分书">无评分书</a></h2></li>'
        )

        parsed = importer._parse_entry(entry)

        self.assertEqual(parsed["title"], "无评分书")
        self.assertIsNone(parsed["score"])
