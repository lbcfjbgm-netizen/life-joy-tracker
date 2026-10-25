from unittest.mock import MagicMock, patch

import requests
from django.core.cache import cache
from django.test import TestCase

from app.models import MediaTypes, Sources
from app.providers import services, weread

SEARCH_URL = "https://weread.qq.com/web/search/global"
BOOK_INFO_URL = "https://weread.qq.com/web/book/info"

# The title the live endpoint returned for 三体, kept verbatim so the test
# data matches what the parser really sees.
TITLE = "三体全集（全三册）"  # noqa: RUF001

COVER = (
    "https://cdn.weread.qq.com/weread/cover/80/yuewen_695233/"
    "s_yuewen_6952331784884457.jpg"
)

# A trimmed but faithful slice of a real `/web/search/global` answer: the
# fields are the ones the live endpoint returned for 三体, including a book
# that has no rating and no publisher at all, and the junk shapes the endpoint
# mixes into the list.
SEARCH_PAYLOAD = {
    "books": [
        {
            "bookInfo": {
                "bookId": "695233",
                "title": TITLE,
                "author": "刘慈欣",
                "publisher": "重庆出版社",
                "cover": COVER,
                "intro": "荣获世界科幻大奖雨果奖长篇小说奖。\n每本书架上都该有套三体。",
                "newRating": 930,
                "newRatingCount": 295681,
                "deepLink": "https://weread.qq.com/book-detail?type=1&v=ce032b305a",
                "readerLink": "https://weread.qq.com/web/reader/ce032b305a",
            },
            "readingCount": 10745,
        },
        {
            "bookInfo": {
                "bookId": "54655039",
                "title": "三体：飞升归来我缔造了神话时代",  # noqa: RUF001
                "author": "某某",
                "cover": "https://cdn.weread.qq.com/weread/cover/1/x.jpg",
            },
        },
        {"bookInfo": {"title": "no media id"}},
        {"notABook": True},
    ],
    "totalCount": 57,
    "hasMore": 1,
}


def _response(payload):
    """Return a fake requests response carrying ``payload``."""
    response = MagicMock()
    response.json.return_value = payload
    response.status_code = requests.codes.ok
    return response


def _http_error(status_code):
    """Return an HTTPError carrying a response with ``status_code``."""
    response = MagicMock()
    response.status_code = status_code
    return requests.exceptions.HTTPError(response=response)


class WeReadSearchTest(TestCase):
    """Test the keyless WeRead search."""

    def setUp(self):
        """Clear the provider cache between tests."""
        cache.clear()

    @patch("app.providers.services.session.get")
    def test_search_parses_books(self, mock_get):
        """The books of the answer become search results."""
        mock_get.return_value = _response(SEARCH_PAYLOAD)

        data = weread.search("三体", 1)

        self.assertEqual(len(data["results"]), 2)
        first = data["results"][0]
        self.assertEqual(first["media_id"], "695233")
        self.assertEqual(first["source"], Sources.WEREAD.value)
        self.assertEqual(first["media_type"], MediaTypes.BOOK.value)
        self.assertEqual(first["title"], TITLE)

    @patch("app.providers.services.session.get")
    def test_search_keeps_chinese_text(self, mock_get):
        """Chinese titles survive the round trip without mojibake."""
        mock_get.return_value = _response(SEARCH_PAYLOAD)

        data = weread.search("三体", 1)

        self.assertEqual(data["results"][0]["title"], TITLE)

    @patch("app.providers.services.session.get")
    def test_search_cover_is_untouched(self, mock_get):
        """The cover URL is used exactly as the API sent it."""
        mock_get.return_value = _response(SEARCH_PAYLOAD)

        data = weread.search("三体", 1)

        self.assertEqual(data["results"][0]["image"], COVER)

    @patch("app.providers.services.session.get")
    def test_search_requests_the_page_offset(self, mock_get):
        """Page two asks the server for the matching offset."""
        mock_get.return_value = _response(SEARCH_PAYLOAD)

        weread.search("三体", 2)

        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["url"], SEARCH_URL)
        self.assertEqual(kwargs["params"]["keyword"], "三体")
        self.assertEqual(kwargs["params"]["maxIdx"], 24)
        self.assertEqual(kwargs["params"]["count"], 24)

    @patch("app.providers.services.session.get")
    def test_search_is_cached(self, mock_get):
        """A repeated search is served from the cache."""
        mock_get.return_value = _response(SEARCH_PAYLOAD)

        weread.search("三体", 1)
        weread.search("三体", 1)

        self.assertEqual(mock_get.call_count, 1)

    @patch("app.providers.services.session.get")
    def test_search_empty(self, mock_get):
        """An empty answer gives an empty result list."""
        mock_get.return_value = _response({"books": [], "totalCount": 0})

        data = weread.search("没有这本书", 1)

        self.assertEqual(data["results"], [])
        self.assertEqual(data["total_results"], 0)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["total_pages"], 1)

    @patch("app.providers.services.session.get")
    def test_search_drops_entries_without_book_info(self, mock_get):
        """Entries without a usable payload are skipped, not crashed on."""
        mock_get.return_value = _response(SEARCH_PAYLOAD)

        data = weread.search("三体", 1)

        self.assertEqual(
            [result["media_id"] for result in data["results"]],
            ["695233", "54655039"],
        )

    @patch("app.providers.services.session.get")
    def test_search_missing_count_is_not_invented(self, mock_get):
        """A response without totalCount still yields a usable page count."""
        mock_get.return_value = _response({"books": SEARCH_PAYLOAD["books"]})

        data = weread.search("三体", 1)

        self.assertEqual(data["total_results"], 2)

    @patch("app.providers.services.session.get")
    def test_search_http_error_raises_provider_error(self, mock_get):
        """A rejected request becomes a provider error."""
        mock_get.side_effect = _http_error(requests.codes.internal_server_error)

        with self.assertRaises(services.ProviderAPIError) as context:
            weread.search("三体", 1)

        self.assertEqual(context.exception.provider, Sources.WEREAD.value)


class WeReadBookTest(TestCase):
    """Test the metadata of a single WeRead book."""

    def setUp(self):
        """Clear the provider cache between tests."""
        cache.clear()

    @patch("app.providers.services.session.get")
    def test_book_uses_search_payload_without_extra_request(self, mock_get):
        """A book opened from a search result needs no second call."""
        mock_get.return_value = _response(SEARCH_PAYLOAD)
        weread.search("三体", 1)

        book = weread.book("695233")

        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(book["title"], TITLE)

    @patch("app.providers.services.session.get")
    def test_book_returns_the_full_shape(self, mock_get):
        """Every key of the Open Library shape is present and typed."""
        mock_get.return_value = _response(SEARCH_PAYLOAD)
        weread.search("三体", 1)

        book = weread.book("695233")

        self.assertEqual(book["media_id"], "695233")
        self.assertEqual(book["source"], Sources.WEREAD.value)
        self.assertEqual(book["media_type"], MediaTypes.BOOK.value)
        self.assertEqual(
            book["source_url"],
            "https://weread.qq.com/web/reader/ce032b305a",
        )
        self.assertIsNone(book["max_progress"])
        self.assertEqual(book["genres"], [])
        self.assertEqual(book["related"], {})
        self.assertEqual(book["details"]["author"], "刘慈欣")
        self.assertEqual(book["details"]["publishers"], ["重庆出版社"])

    @patch("app.providers.services.session.get")
    def test_book_score_is_divided_by_one_hundred(self, mock_get):
        """A rating of 930 is shown as 9.3."""
        mock_get.return_value = _response(SEARCH_PAYLOAD)
        weread.search("三体", 1)

        book = weread.book("695233")

        self.assertEqual(book["score"], 9.3)
        self.assertEqual(book["score_count"], 295681)

    @patch("app.providers.services.session.get")
    def test_book_without_rating_has_no_score(self, mock_get):
        """A book with no rating gets None instead of a fake zero."""
        mock_get.return_value = _response(SEARCH_PAYLOAD)
        weread.search("三体", 1)

        book = weread.book("54655039")

        self.assertIsNone(book["score"])
        self.assertIsNone(book["score_count"])
        self.assertEqual(book["details"]["publishers"], [])

    @patch("app.providers.services.session.get")
    def test_book_missing_fields_fall_back(self, mock_get):
        """A payload missing author, cover and intro still renders."""
        mock_get.return_value = _response(
            {"books": [{"bookInfo": {"bookId": 42, "title": "只有书名"}}]},
        )
        weread.search("只有书名", 1)

        with self.settings(IMG_NONE="https://example.com/none.jpg"):
            book = weread.book("42")

        self.assertEqual(book["media_id"], "42")
        self.assertEqual(book["title"], "只有书名")
        self.assertEqual(book["image"], "https://example.com/none.jpg")
        self.assertEqual(book["synopsis"], "No synopsis available.")
        self.assertIsNone(book["details"]["author"])
        self.assertEqual(book["details"]["publishers"], [])
        self.assertEqual(book["source_url"], "https://weread.qq.com/web/bookDetail/42")
        self.assertIsNone(book["score"])
        self.assertIsNone(book["score_count"])

    @patch("app.providers.services.session.get")
    def test_book_detail_endpoint_is_used_when_it_answers(self, mock_get):
        """A logged in cookie would let the detail endpoint win."""
        mock_get.return_value = _response(
            {
                "bookId": "695233",
                "title": TITLE,
                "author": "刘慈欣",
                "publisher": "重庆出版社",
                "cover": COVER,
                "intro": "intro",
                "newRating": 930,
                "newRatingCount": 10,
                "readerLink": "https://weread.qq.com/web/reader/abc",
            },
        )

        book = weread.book("695233")

        self.assertEqual(book["title"], TITLE)
        self.assertEqual(book["score"], 9.3)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["url"], BOOK_INFO_URL)
        self.assertEqual(kwargs["params"]["bookId"], "695233")

    @patch("app.providers.services.session.get")
    def test_book_detail_login_error_falls_back_to_search(self, mock_get):
        """A gated detail endpoint defers to the public search."""
        mock_get.side_effect = [
            _response({"errCode": -2010, "errMsg": "用户不存在", "info": ""}),
            _response(SEARCH_PAYLOAD),
        ]

        book = weread.book("695233")

        self.assertEqual(book["title"], TITLE)
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(mock_get.call_args_list[1][1]["url"], SEARCH_URL)

    @patch("app.providers.services.session.get")
    def test_book_detail_http_error_falls_back_to_search(self, mock_get):
        """A broken detail endpoint still lets the book be described."""
        mock_get.side_effect = [
            _http_error(requests.codes.bad_gateway),
            _response(SEARCH_PAYLOAD),
        ]

        book = weread.book("695233")

        self.assertEqual(book["title"], TITLE)

    @patch("app.providers.services.session.get")
    def test_book_not_found_raises(self, mock_get):
        """A book that no source knows raises a not found error."""
        mock_get.side_effect = [
            _response({"errCode": -2010, "errMsg": "用户不存在", "info": ""}),
            _response({"books": [], "totalCount": 0}),
        ]

        with self.assertRaises(services.ProviderAPIError) as context:
            weread.book("does-not-exist")

        self.assertEqual(context.exception.provider, Sources.WEREAD.value)
        self.assertEqual(context.exception.status_code, 404)

    @patch("app.providers.services.session.get")
    def test_book_matches_the_exact_id_only(self, mock_get):
        """A search hit for another book must not be mistaken for this one."""
        mock_get.side_effect = [
            _response({"errCode": -2010, "errMsg": "用户不存在", "info": ""}),
            _response(SEARCH_PAYLOAD),
        ]

        with self.assertRaises(services.ProviderAPIError):
            weread.book("999999")

    @patch("app.providers.services.session.get")
    def test_book_is_cached(self, mock_get):
        """The assembled metadata is cached, so a second view costs nothing."""
        mock_get.return_value = _response(SEARCH_PAYLOAD)

        first = weread.book("695233")
        calls_after_first = mock_get.call_count
        second = weread.book("695233")

        self.assertEqual(second, first)
        self.assertEqual(mock_get.call_count, calls_after_first)


class WeReadHelpersTest(TestCase):
    """Test the small parsing helpers on their own."""

    def test_get_score_scales_hundredths(self):
        """The hundred point rating becomes a ten point score."""
        self.assertEqual(weread.get_score({"newRating": 930}), 9.3)
        self.assertEqual(weread.get_score({"newRating": 800}), 8.0)
        self.assertIsNone(weread.get_score({"newRating": 0}))
        self.assertIsNone(weread.get_score({}))

    def test_get_score_count_rejects_missing(self):
        """A missing rating count is unknown, not zero."""
        self.assertEqual(weread.get_score_count({"newRatingCount": 5}), 5)
        self.assertIsNone(weread.get_score_count({"newRatingCount": 0}))
        self.assertIsNone(weread.get_score_count({}))

    def test_get_synopsis_collapses_whitespace(self):
        """Line breaks of the intro are flattened into one paragraph."""
        self.assertEqual(
            weread.get_synopsis({"intro": "第一行\n\n第二行"}),
            "第一行 第二行",
        )
        self.assertEqual(weread.get_synopsis({}), "No synopsis available.")

    def test_get_source_url_prefers_the_reader_link(self):
        """The reader link wins over the deep link and the fallback."""
        self.assertEqual(
            weread.get_source_url({"readerLink": "r", "deepLink": "d"}, "1"),
            "r",
        )
        self.assertEqual(weread.get_source_url({"deepLink": "d"}, "1"), "d")
        self.assertEqual(
            weread.get_source_url({}, "1"),
            "https://weread.qq.com/web/bookDetail/1",
        )
