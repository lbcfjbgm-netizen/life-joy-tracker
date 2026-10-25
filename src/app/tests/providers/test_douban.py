"""Tests for the Douban Books provider.

The HTML fixtures are trimmed copies of a real Douban book page, so the
regexes are exercised against the markup Douban actually serves.
"""

from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase

from app.models import MediaTypes, Sources
from app.providers import douban, services

BOOK_HTML = """
<html><body>
<span property="v:itemreviewed">三体</span>
<div id="info">

    <span>
      <span class="pl"> 作者</span>:

            <a href="/author/4561353">刘慈欣</a>
    </span><br/>

    <span class="pl">出版社:</span>

      <a href="/press/1">重庆出版社</a><br/>

    <span class="pl">出版年:</span> 2008-1<br/>

    <span class="pl">ISBN:</span> 9787536692930<br/>

    <span class="pl">页数:</span> 302<br/>

</div>
<div id="interest_sectl">
  <strong class="ll rating_num" property="v:average">8.9</strong>
  <span property="v:votes">518838</span>
</div>
<div class="pic">
  <a class="nbg" href="https://book.douban.com/subject/2567698/">
    <img src="https://img3.doubanio.com/view/subject/s/public/s2768378.jpg"
         rel="v:photo" alt="三体">
  </a>
</div>
<div class="intro">
  <p>文化大革命如火如荼进行的同时。</p>
</div>
</body></html>
"""

LOGIN_HTML = "<html><body><h1>登录跳转</h1></body></html>"

SUGGEST_JSON = [
    {
        "title": "三体",
        "url": "https://book.douban.com/subject/2567698/",
        "pic": "https://img3.doubanio.com/view/subject/s/public/s2768378.jpg",
        "author_name": "刘慈欣",
        "year": "2008",
        "type": "b",
        "id": "2567698",
    },
    {
        "title": "三体: 电影版",
        "url": "https://movie.douban.com/subject/1234/",
        "pic": "https://img1.doubanio.com/view/photo/s/public/p1.jpg",
        "author_name": "某导演",
        "year": "2020",
        "type": "m",
        "id": "1234",
    },
]


def html_response(text):
    """Return a fake response carrying an HTML body."""
    response = MagicMock()
    response.content = text.encode()
    response.raise_for_status.return_value = None
    return response


def json_response(payload):
    """Return a fake response carrying a JSON body."""
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class CacheIsolatedTestCase(TestCase):
    """Drop the provider cache between tests, they reuse the same keys."""

    def setUp(self):
        """Clear the cache before each test."""
        super().setUp()
        cache.clear()


class SearchTests(CacheIsolatedTestCase):
    """Searching Douban through the suggestion endpoint."""

    @patch("app.providers.douban.get")
    def test_only_books_are_kept(self, mock_get):
        """Films and music of the suggestion endpoint are dropped."""
        mock_get.return_value = json_response(SUGGEST_JSON)

        data = douban.search("三体", 1)

        self.assertEqual(data["total_results"], 1)
        self.assertEqual(len(data["results"]), 1)
        result = data["results"][0]
        self.assertEqual(result["media_id"], "2567698")
        self.assertEqual(result["title"], "三体")
        self.assertEqual(result["source"], Sources.DOUBAN.value)
        self.assertEqual(result["media_type"], MediaTypes.BOOK.value)

    @patch("app.providers.douban.get")
    def test_cover_is_the_large_variant(self, mock_get):
        """The small cover of the suggestion is upgraded to the large one."""
        mock_get.return_value = json_response(SUGGEST_JSON)

        data = douban.search("三体", 1)

        image = data["results"][0]["image"]
        # The cover is served by this site because Douban blocks hot linking
        self.assertIn("/image_relay", image)
        self.assertIn("l%2Fpublic", image)

    @patch("app.providers.douban.get")
    def test_page_slicing(self, mock_get):
        """The endpoint has no paging, so a page is a local slice."""
        entries = [
            {**SUGGEST_JSON[0], "id": str(index), "title": f"书 {index}"}
            for index in range(settings.PER_PAGE + 5)
        ]
        mock_get.return_value = json_response(entries)

        first = douban.search("书", 1)
        second = douban.search("书", 2)

        self.assertEqual(first["total_results"], settings.PER_PAGE + 5)
        self.assertEqual(len(first["results"]), settings.PER_PAGE)
        self.assertEqual(len(second["results"]), 5)

    @patch("app.providers.douban.get")
    def test_empty_search(self, mock_get):
        """A query without matches returns an empty page."""
        mock_get.return_value = json_response([])

        data = douban.search("不存在的书", 1)

        self.assertEqual(data["results"], [])
        self.assertEqual(data["total_results"], 0)

    @patch("app.providers.douban.get")
    def test_malformed_json_is_not_a_crash(self, mock_get):
        """A non JSON body is treated as no result instead of an error."""
        response = MagicMock()
        response.json.side_effect = ValueError("not json")
        response.raise_for_status.return_value = None
        mock_get.return_value = response

        data = douban.search("三体", 1)

        self.assertEqual(data["results"], [])


class BookTests(CacheIsolatedTestCase):
    """Parsing the Douban book page."""

    @patch("app.providers.douban.get")
    def test_fields_are_parsed(self, mock_get):
        """The main fields of the detail page are read."""
        mock_get.return_value = html_response(BOOK_HTML)

        meta = douban.book("2567698")

        self.assertEqual(meta["title"], "三体")
        self.assertEqual(meta["score"], 8.9)
        self.assertEqual(meta["score_count"], 518838)
        self.assertEqual(meta["max_progress"], 302)
        self.assertEqual(meta["source"], Sources.DOUBAN.value)
        self.assertEqual(meta["media_type"], MediaTypes.BOOK.value)
        self.assertEqual(
            meta["source_url"],
            "https://book.douban.com/subject/2567698/",
        )

    @patch("app.providers.douban.get")
    def test_details_use_the_info_block(self, mock_get):
        """Author and publisher are read from the info block."""
        mock_get.return_value = html_response(BOOK_HTML)

        details = douban.book("2567698")["details"]

        self.assertEqual(details["author"], "刘慈欣")
        self.assertEqual(details["publishers"], ["重庆出版社"])
        self.assertEqual(details["isbn"], "9787536692930")
        self.assertEqual(details["number_of_pages"], 302)

    @patch("app.providers.douban.get")
    def test_cover_is_relayed_and_large(self, mock_get):
        """The stored cover goes through the relay and uses the large file."""
        mock_get.return_value = html_response(BOOK_HTML)

        image = douban.book("2567698")["image"]

        self.assertIn("/image_relay", image)
        self.assertIn("l%2Fpublic", image)

    @patch("app.providers.douban.get")
    def test_login_page_raises_not_found(self, mock_get):
        """A page without a book title means the book is gone, not empty."""
        mock_get.return_value = html_response(LOGIN_HTML)

        with self.assertRaises(services.ProviderAPIError) as context:
            douban.book("2567698")

        self.assertIn("not found", str(context.exception))
        self.assertEqual(context.exception.status_code, 404)

    @patch("app.providers.douban.get")
    def test_missing_optional_fields_do_not_crash(self, mock_get):
        """A sparse page still yields a usable record."""
        minimal = (
            '<span property="v:itemreviewed">无名之书</span><div id="info"></div>'
        )
        mock_get.return_value = html_response(minimal)

        meta = douban.book("1")

        self.assertEqual(meta["title"], "无名之书")
        self.assertIsNone(meta["score"])
        self.assertIsNone(meta["max_progress"])
        self.assertEqual(meta["genres"], [])


class ImageTests(TestCase):
    """Cover URL cleanup."""

    def test_small_cover_becomes_large(self):
        """The small cover path is replaced by the large one."""
        url = douban.large_cover(
            "https://img3.doubanio.com/view/subject/s/public/s2768378.jpg",
        )
        self.assertIn("l/public", url)

    def test_unusable_shard_falls_back_to_placeholder(self):
        """img9 answers with an HTML page, so it is not used."""
        url = douban.large_cover(
            "https://img9.doubanio.com/view/subject/s/public/s2768378.jpg",
        )
        self.assertEqual(url, settings.IMG_NONE)

    def test_missing_cover_falls_back_to_placeholder(self):
        """A book without a cover gets the placeholder."""
        self.assertEqual(douban.large_cover(None), settings.IMG_NONE)
