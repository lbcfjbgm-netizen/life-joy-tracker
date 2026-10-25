"""Tests for the local image relay used by providers behind a referer check."""

from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.urls import reverse

from app import image_relay

DOUBAN_IMG = "https://img3.doubanio.com/view/subject/l/public/s2768378.jpg"
STEAM_IMG = "https://media.steampowered.com/steam/apps/730/library_600x900.jpg"
STEAM_FASTLY_IMG = (
    "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/730/movie.jpg"
)
OTHER_IMG = "https://cdn.example.com/a.jpg"


def image_response(content=b"\xff\xd8\xff\xe0", content_type="image/jpeg"):
    """Return a fake response carrying image bytes."""
    response = MagicMock()
    response.content = content
    response.headers = {"Content-Type": content_type}
    response.raise_for_status.return_value = None
    return response


class RefererForTests(TestCase):
    """Which hosts get a referer and which are left alone."""

    def test_douban_image_gets_a_douban_referer(self):
        """A Douban cover needs a Douban page as the referer."""
        self.assertEqual(
            image_relay.referer_for(DOUBAN_IMG),
            "https://book.douban.com/",
        )

    def test_plain_douban_domain_also_gets_the_referer(self):
        """The apex domain is treated like the book subdomain."""
        self.assertEqual(
            image_relay.referer_for("https://www.douban.com/x.jpg"),
            "https://book.douban.com/",
        )

    def test_steam_needs_no_referer_but_is_still_cached(self):
        """Steam serves without a referer, yet the cover goes through the cache."""
        self.assertIsNone(image_relay.referer_for(STEAM_IMG))
        self.assertTrue(image_relay.should_cache(STEAM_IMG))
        self.assertTrue(image_relay.should_cache(STEAM_FASTLY_IMG))

    def test_unknown_host_is_left_alone(self):
        """An unknown host is never fetched through this site."""
        self.assertIsNone(image_relay.referer_for("https://example.com/a.jpg"))
        self.assertFalse(image_relay.should_cache("https://example.com/a.jpg"))


class HostAllowedTests(TestCase):
    """The relay endpoint only answers for the hosts it is meant to serve."""

    def test_douban_and_steam_are_allowed(self):
        """Both the referer hosts and the Steam CDNs pass the whitelist."""
        for url in (DOUBAN_IMG, STEAM_IMG, STEAM_FASTLY_IMG):
            with self.subTest(url=url):
                self.assertTrue(image_relay.host_allowed(url))

    def test_lookalike_and_foreign_hosts_are_rejected(self):
        """A suffix that only ends with a whitelisted name is not enough."""
        for url in (
            "https://evil.com/x.jpg",
            "https://doubanio.com.evil.com/x.jpg",
            "https://notsteampowered.com/x.jpg",
        ):
            with self.subTest(url=url):
                self.assertFalse(image_relay.host_allowed(url))


class LocalizeTests(TestCase):
    """Cached images are rewritten to the relay, everything else is untouched."""

    def test_douban_image_is_rewritten(self):
        """A Douban cover is served through this site."""
        url = image_relay.localize(DOUBAN_IMG)
        self.assertTrue(url.startswith(reverse("image_relay")))
        self.assertIn("img3.doubanio.com", url)

    def test_steam_image_is_rewritten(self):
        """A Steam cover now comes from the local cache instead of Steam."""
        url = image_relay.localize(STEAM_IMG)
        self.assertTrue(url.startswith(reverse("image_relay")))
        self.assertIn("media.steampowered.com", url)

    def test_other_hosts_are_untouched(self):
        """A host outside the whitelist keeps its original URL."""
        self.assertEqual(image_relay.localize(OTHER_IMG), OTHER_IMG)

    def test_empty_and_placeholder_are_untouched(self):
        """Missing covers stay as they are instead of becoming a relay URL."""
        self.assertEqual(image_relay.localize(""), "")
        self.assertIsNone(image_relay.localize(None))

    def test_hero_url_is_built_from_the_appid(self):
        """The wide background image is derived from the appid, then cached."""
        url = image_relay.localize(image_relay.steam_hero_url("730"))
        self.assertIn("library_hero.jpg", url)
        self.assertEqual(image_relay.steam_hero_url("not-a-number"), "")


class FetchImageTests(TestCase):
    """Fetching adds the referer and refuses anything that is not an image."""

    @patch("app.image_relay.cache")
    @patch("app.image_relay._read_from_disk", return_value=None)
    @patch("app.image_relay._write_to_disk")
    @patch("app.image_relay.requests.get")
    def test_fetch_sends_the_douban_referer(
        self, mock_get, _mock_write, _mock_read, mock_cache
    ):
        """The Douban referer is what makes the CDN answer with the image."""
        mock_cache.get.return_value = None
        mock_get.return_value = image_response(b"\xff\xd8\xff\xe0" + b"x" * 50_000)

        content, content_type = image_relay.fetch_image(DOUBAN_IMG)

        self.assertEqual(content_type, "image/jpeg")
        self.assertGreater(len(content), 40_000)
        sent_headers = mock_get.call_args.kwargs["headers"]
        self.assertEqual(sent_headers["Referer"], "https://book.douban.com/")

    @patch("app.image_relay.cache")
    @patch("app.image_relay._read_from_disk", return_value=None)
    @patch("app.image_relay._write_to_disk")
    @patch("app.image_relay.requests.get")
    def test_steam_fetch_sends_no_referer(self, mock_get, mock_write, _mock_read, mock_cache):
        """Steam gets the same cache, but without a fabricated referer."""
        mock_cache.get.return_value = None
        mock_get.return_value = image_response(b"\xff\xd8\xff\xe0" + b"x" * 50_000)

        content, content_type = image_relay.fetch_image(STEAM_IMG)

        self.assertEqual(content_type, "image/jpeg")
        sent_headers = mock_get.call_args.kwargs["headers"]
        self.assertNotIn("Referer", sent_headers)
        self.assertIn("User-Agent", sent_headers)
        # And the bytes are persisted for the next run.
        mock_write.assert_called_once()

    @patch("app.image_relay.cache")
    @patch("app.image_relay._read_from_disk", return_value=None)
    @patch("app.image_relay._write_to_disk")
    @patch("app.image_relay.requests.get")
    def test_html_instead_of_image_is_rejected(
        self, mock_get, _mock_write, _mock_read, mock_cache
    ):
        """An HTML error page is never passed on as if it were a cover."""
        mock_cache.get.return_value = None
        mock_get.return_value = image_response(
            b"<script>alert(1)</script>",
            content_type="text/html",
        )

        content, content_type = image_relay.fetch_image(DOUBAN_IMG)

        self.assertIsNone(content)
        self.assertIsNone(content_type)

    @patch("app.image_relay.cache")
    @patch("app.image_relay._read_from_disk", return_value=None)
    @patch("app.image_relay.requests.get")
    def test_host_outside_the_whitelist_is_not_fetched(
        self, mock_get, _mock_read, _mock_cache
    ):
        """Hosts that are neither cached nor allowed are not proxied at all."""
        content, content_type = image_relay.fetch_image(OTHER_IMG)

        self.assertIsNone(content)
        self.assertIsNone(content_type)
        mock_get.assert_not_called()

    @patch("app.image_relay._read_from_disk", return_value=None)
    @patch("app.image_relay.cache")
    @patch("app.image_relay.requests.get")
    def test_cached_image_skips_the_request(self, mock_get, mock_cache, _mock_read):
        """A cover fetched once is served from the cache afterwards."""
        mock_cache.get.return_value = (b"cached-bytes", "image/jpeg")

        content, content_type = image_relay.fetch_image(DOUBAN_IMG)

        self.assertEqual(content, b"cached-bytes")
        self.assertEqual(content_type, "image/jpeg")
        mock_get.assert_not_called()


class RelayViewTests(TestCase):
    """The view serves the bytes or falls back to a redirect."""

    @patch("app.image_relay.fetch_image")
    def test_serves_fetched_bytes(self, mock_fetch):
        """The fetched image is returned with its own content type."""
        mock_fetch.return_value = (b"jpeg-bytes", "image/jpeg")

        response = self.client.get(reverse("image_relay"), {"url": DOUBAN_IMG})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(response.content, b"jpeg-bytes")

    @patch("app.image_relay.fetch_image")
    def test_redirects_when_nothing_could_be_fetched(self, mock_fetch):
        """When the relay cannot fetch, the browser tries on its own."""
        mock_fetch.return_value = (None, None)

        response = self.client.get(reverse("image_relay"), {"url": DOUBAN_IMG})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], DOUBAN_IMG)

    def test_missing_url_is_a_bad_request(self):
        """An empty URL is rejected."""
        response = self.client.get(reverse("image_relay"))
        self.assertEqual(response.status_code, 400)

    def test_non_http_url_is_a_bad_request(self):
        """Local files and other schemes are not served."""
        url = reverse("image_relay")
        response = self.client.get(url, {"url": "file:///etc/passwd"})
        self.assertEqual(response.status_code, 400)

    @patch("app.image_relay.fetch_image")
    def test_foreign_host_is_a_bad_request(self, mock_fetch):
        """The endpoint refuses to proxy arbitrary third party hosts."""
        response = self.client.get(
            reverse("image_relay"),
            {"url": "https://evil.com/x.jpg"},
        )

        self.assertEqual(response.status_code, 400)
        mock_fetch.assert_not_called()

    @patch("app.image_relay.fetch_image")
    def test_steam_host_passes_the_whitelist(self, mock_fetch):
        """A whitelisted Steam cover reaches the fetch stage."""
        mock_fetch.return_value = (b"jpeg-bytes", "image/jpeg")

        response = self.client.get(reverse("image_relay"), {"url": STEAM_IMG})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        mock_fetch.assert_called_once_with(STEAM_IMG)


class RelayConditionalRequestTests(TestCase):
    """A cached cover is revalidated with ETag / Last-Modified instead of refetched."""

    def setUp(self):
        self.url = reverse("image_relay")
        self.etag = image_relay.etag_for(DOUBAN_IMG)

    @patch("app.image_relay.fetch_image")
    def test_first_response_carries_an_etag(self, mock_fetch):
        """The first GET hands the browser a validator to send back later."""
        mock_fetch.return_value = (b"jpeg-bytes", "image/jpeg")

        response = self.client.get(self.url, {"url": DOUBAN_IMG})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["ETag"], self.etag)
        self.assertTrue(response["ETag"].startswith('"'))
        self.assertTrue(response["ETag"].endswith('"'))
        self.assertIn("max-age=", response["Cache-Control"])

    @patch("app.image_relay.fetch_image")
    def test_matching_etag_returns_304_without_a_body(self, mock_fetch):
        """Sending the same validator back gets a bodyless 304."""
        mock_fetch.return_value = (b"jpeg-bytes", "image/jpeg")

        response = self.client.get(
            self.url,
            {"url": DOUBAN_IMG},
            headers={"If-None-Match": self.etag},
        )

        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.content, b"")
        self.assertEqual(response["ETag"], self.etag)
        self.assertIn("max-age=", response["Cache-Control"])

    @patch("app.image_relay.fetch_image")
    def test_weak_and_listed_etags_also_match(self, mock_fetch):
        """``W/`` prefixes and comma separated lists are honoured."""
        mock_fetch.return_value = (b"jpeg-bytes", "image/jpeg")

        for sent in (
            "W/" + self.etag,
            f'"deadbeefdeadbeef", {self.etag}',
        ):
            with self.subTest(header=sent):
                response = self.client.get(
                    self.url,
                    {"url": DOUBAN_IMG},
                    headers={"If-None-Match": sent},
                )
                self.assertEqual(response.status_code, 304)

    @patch("app.image_relay.fetch_image")
    def test_star_etag_returns_304(self, mock_fetch):
        """``If-None-Match: *`` means any stored representation is fine."""
        mock_fetch.return_value = (b"jpeg-bytes", "image/jpeg")

        response = self.client.get(
            self.url,
            {"url": DOUBAN_IMG},
            headers={"If-None-Match": "*"},
        )

        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.content, b"")

    @patch("app.image_relay.fetch_image")
    def test_stale_etag_gets_the_full_image(self, mock_fetch):
        """A validator that does not belong to this image still gets the bytes."""
        mock_fetch.return_value = (b"jpeg-bytes", "image/jpeg")

        response = self.client.get(
            self.url,
            {"url": DOUBAN_IMG},
            headers={"If-None-Match": '"0000000000000000"'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"jpeg-bytes")

    @patch("app.image_relay._disk_mtime", return_value=None)
    @patch("app.image_relay.fetch_image")
    def test_redis_only_image_has_no_last_modified(self, mock_fetch, _mock_mtime):
        """Without a file on disk only the ETag is offered, never a fresh date."""
        mock_fetch.return_value = (b"jpeg-bytes", "image/jpeg")

        response = self.client.get(self.url, {"url": DOUBAN_IMG})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["ETag"], self.etag)
        self.assertNotIn("Last-Modified", response)

    @patch("app.image_relay._disk_mtime", return_value=1_700_000_000.0)
    @patch("app.image_relay.fetch_image")
    def test_disk_image_reports_last_modified_and_answers_304(
        self, mock_fetch, _mock_mtime
    ):
        """The file mtime becomes Last-Modified and a newer If-Modified-Since 304s."""
        mock_fetch.return_value = (b"jpeg-bytes", "image/jpeg")

        first = self.client.get(self.url, {"url": DOUBAN_IMG})
        self.assertEqual(first.status_code, 200)
        last_modified = first["Last-Modified"]
        self.assertTrue(last_modified)

        again = self.client.get(
            self.url,
            {"url": DOUBAN_IMG},
            headers={"If-Modified-Since": last_modified},
        )

        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b"")

    @patch("app.image_relay._disk_mtime", return_value=1_700_000_000.0)
    @patch("app.image_relay.fetch_image")
    def test_if_none_match_wins_over_if_modified_since(self, mock_fetch, _mock_mtime):
        """A present but stale ``If-None-Match`` is not rescued by a date."""
        mock_fetch.return_value = (b"jpeg-bytes", "image/jpeg")

        response = self.client.get(
            self.url,
            {"url": DOUBAN_IMG},
            headers={
                "If-None-Match": '"0000000000000000"',
                "If-Modified-Since": "Wed, 21 Oct 2099 07:28:00 GMT",
            },
        )

        self.assertEqual(response.status_code, 200)

    @patch("app.image_relay.fetch_image")
    def test_failed_fetch_is_not_reported_as_304(self, mock_fetch):
        """Nothing fetched stays a redirect, even with a matching validator."""
        mock_fetch.return_value = (None, None)

        response = self.client.get(
            self.url,
            {"url": DOUBAN_IMG},
            headers={"If-None-Match": "*"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], DOUBAN_IMG)

    def test_invalid_requests_stay_bad_requests(self):
        """The validators never mask the input checks."""
        self.assertEqual(
            self.client.get(self.url, headers={"If-None-Match": "*"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(
                self.url,
                {"url": "file:///etc/passwd"},
                headers={"If-None-Match": "*"},
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(
                self.url,
                {"url": "https://evil.com/x.jpg"},
                headers={"If-None-Match": "*"},
            ).status_code,
            400,
        )
