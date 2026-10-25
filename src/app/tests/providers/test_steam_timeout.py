"""Tests for the fast-fail behaviour of the provider requests.

The Steam store host refuses the connection from this machine, and with the
old single 120 seconds timeout every search waited for it. These tests pin the
three pieces of the fix: the split connect/read timeout, the network errors
surfacing as ``ProviderUnavailableError``, and the 60 seconds short circuit.
"""

from unittest.mock import MagicMock, patch

import requests
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase

from app.models import Sources
from app.providers import services, steam

STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"


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


class ApiRequestTimeoutTest(TestCase):
    """Test the connect/read timeout split and the network error mapping."""

    def setUp(self):
        """Clear the cache so a short circuit never leaks between tests."""
        cache.clear()

    @patch("app.providers.services.session.get")
    def test_default_timeout_caps_connect_only(self, mock_get):
        """The default is a tuple: tight connect, generous read."""
        mock_get.return_value = _response({"ok": True})

        services.api_request("TEST", "GET", "https://example.com/api")

        _, kwargs = mock_get.call_args
        self.assertEqual(
            kwargs["timeout"],
            (settings.API_CONNECT_TIMEOUT, settings.REQUEST_TIMEOUT),
        )
        # The read timeout must stay the wide one: some payloads are huge
        self.assertEqual(kwargs["timeout"][1], 120)

    @patch("app.providers.services.session.get")
    def test_caller_timeout_overrides_the_default(self, mock_get):
        """A caller may pass a number or its own tuple."""
        mock_get.return_value = _response({"ok": True})

        services.api_request(
            "TEST",
            "GET",
            "https://example.com/api",
            timeout=(2, 30),
        )
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["timeout"], (2, 30))

        services.api_request("TEST", "GET", "https://example.com/api", timeout=7)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["timeout"], 7)

    @patch("app.providers.services.session.get")
    def test_connect_timeout_raises_provider_unavailable(self, mock_get):
        """A connect timeout is not a provider error response, it is no answer."""
        mock_get.side_effect = requests.exceptions.ConnectTimeout("timed out")

        with self.assertRaises(services.ProviderUnavailableError) as cm:
            services.api_request("STEAM", "GET", STORE_SEARCH_URL)

        error = cm.exception
        # The search view catches the subclass first, then the base one
        self.assertIsInstance(error, services.ProviderAPIError)
        self.assertIsInstance(error, services.ProviderUnavailableError)
        # Importers still treat it as a plain unreachable-host error
        self.assertIsInstance(error, requests.exceptions.RequestException)
        self.assertEqual(error.provider, "STEAM")
        self.assertEqual(error.provider_label, Sources.STEAM.label)
        self.assertIsNone(error.status_code)

    @patch("app.providers.services.session.get")
    def test_connection_error_raises_provider_unavailable(self, mock_get):
        """A refused connection is mapped the same way as a timeout."""
        mock_get.side_effect = requests.exceptions.ConnectionError("refused")

        with self.assertRaises(services.ProviderUnavailableError):
            services.api_request("STEAM", "GET", STORE_SEARCH_URL)


class ProviderShortCircuitTest(TestCase):
    """Test the 60 seconds short circuit around a dead provider."""

    def setUp(self):
        """Clear the cache so the short circuit starts from a clean state."""
        cache.clear()

    @patch("app.providers.services.session.get")
    def test_second_call_does_not_touch_the_network(self, mock_get):
        """After one network failure the next call fails without a request."""
        mock_get.side_effect = requests.exceptions.ConnectTimeout("timed out")

        with self.assertRaises(services.ProviderUnavailableError):
            services.api_request("STEAM", "GET", STORE_SEARCH_URL)
        self.assertEqual(mock_get.call_count, 1)

        with self.assertRaises(services.ProviderUnavailableError):
            services.api_request("STEAM", "GET", STORE_SEARCH_URL)

        # The whole point: the dead host costs exactly one timeout, not one
        # per keystroke
        self.assertEqual(mock_get.call_count, 1)
        self.assertIsNotNone(cache.get(f"provider_down:{Sources.STEAM.value}"))

    @patch("app.providers.services.session.get")
    def test_http_error_does_not_trip_the_short_circuit(self, mock_get):
        """An HTTP status is an answer: it must not disable the provider."""
        mock_get.side_effect = _http_error(requests.codes.bad_gateway)

        with self.assertRaises(requests.exceptions.HTTPError):
            services.api_request("STEAM", "GET", STORE_SEARCH_URL)

        self.assertIsNone(cache.get(f"provider_down:{Sources.STEAM.value}"))

    @patch("app.providers.services.session.get")
    def test_success_clears_the_short_circuit(self, mock_get):
        """Any successful answer drops the flag of the provider."""
        cache.set(f"provider_down:{Sources.STEAM.value}", value=True, timeout=60)
        mock_get.return_value = _response({"ok": True})

        # Pretend the TTL has run out so the call is allowed through
        with patch("app.providers.services.is_provider_down", return_value=False):
            services.api_request("STEAM", "GET", STORE_SEARCH_URL)

        self.assertIsNone(cache.get(f"provider_down:{Sources.STEAM.value}"))

    @patch("app.providers.steam._cover_exists", return_value=True)
    @patch("app.providers.services.session.get")
    def test_steam_search_recovers_after_the_short_circuit(
        self,
        mock_get,
        _mock_cover,
    ):
        """A store search succeeds again once the flag is gone."""
        mock_get.side_effect = requests.exceptions.ConnectTimeout("timed out")
        with self.assertRaises(services.ProviderUnavailableError):
            steam.search("portal", 1)
        self.assertEqual(mock_get.call_count, 1)

        # The store came back: drop the flag the way the TTL would
        cache.delete(f"provider_down:{Sources.STEAM.value}")
        mock_get.side_effect = None
        mock_get.return_value = _response(
            {"total": 1, "items": [{"type": "app", "name": "Portal 2", "id": 620}]},
        )

        data = steam.search("portal", 1)

        self.assertEqual(len(data["results"]), 1)
        self.assertIsNone(cache.get(f"provider_down:{Sources.STEAM.value}"))

    @patch("app.providers.steam._cover_exists", return_value=True)
    @patch("app.providers.services.session.get")
    def test_steam_search_network_error_is_unavailable(self, mock_get, _mock_cover):
        """The store search no longer bubbles a raw request error as a 500."""
        mock_get.side_effect = requests.exceptions.ConnectionError("refused")

        with self.assertRaises(services.ProviderUnavailableError):
            steam.search("portal", 1)
