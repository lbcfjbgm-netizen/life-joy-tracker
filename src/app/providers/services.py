import contextvars
import logging
import time
from contextlib import contextmanager

import requests
from defusedxml import ElementTree
from django.conf import settings
from django.core.cache import cache
from pyrate_limiter import RedisBucket
from redis import Redis
from requests.adapters import HTTPAdapter
from requests_ratelimiter import LimiterAdapter, LimiterSession
from urllib3.util.retry import Retry, reraise

from app.models import MediaTypes, Sources
from app.providers import (
    bgg,
    comicvine,
    douban,
    douban_movie,
    douban_tv,
    hardcover,
    igdb,
    mal,
    mangaupdates,
    manual,
    openlibrary,
    steam,
    tmdb,
    weread,
)

logger = logging.getLogger(__name__)


def get_redis_client():
    """Return a Redis client."""
    if settings.TESTING:
        import fakeredis  # noqa: PLC0415

        return fakeredis.FakeRedis()
    return Redis.from_url(settings.REDIS_URL)


redis_db = get_redis_client()
bucket_key = f"{settings.REDIS_PREFIX}_api" if settings.REDIS_PREFIX else "api"

session = LimiterSession(
    per_second=5,
    bucket_class=RedisBucket,
    bucket_kwargs={"redis": redis_db, "bucket_key": bucket_key},
)

# Connecting is not retried: the tight connect timeout is already the budget of
# a whole call, and retrying a dead host multiplied it by four (one attempt plus
# three retries) before the failure even surfaced. Reads keep the retries that
# were always configured, since a dropped body is worth one more attempt.
#
# Interactive requests (a person waiting on the search box) are the exception:
# urllib3 retries a read timeout like any other transport error, so the
# configured retries turned one short read budget into four attempts (measured:
# a 2 s read timeout took 8.07 s to surface). ``_InteractiveRetry`` gives up
# after the first attempt while a person is waiting, and behaves exactly like
# the plain policy for every background call, where a second attempt is worth
# the wait.
_interactive_request = contextvars.ContextVar(
    "provider_interactive_request",
    default=False,
)
# Wall clock stop signal of the interactive request, on ``time.monotonic``.
# Per request timeouts alone cannot bound a search: a search is several
# request hops in a row (the store call, then one cover lookup per game, run
# in rounds of the thread pool), and a timeout on each hop still adds up.
_interactive_deadline = contextvars.ContextVar(
    "provider_interactive_deadline",
    default=None,
)


def _retry_error(args, kwargs):
    """Return the ``(error, stacktrace)`` an ``increment`` call was given."""
    error = kwargs.get("error")
    if error is None:
        error = next((arg for arg in args if isinstance(arg, BaseException)), None)
    return error, kwargs.get("_stacktrace")


class _InteractiveRetry(Retry):
    """Retry policy that stops retrying inside an interactive request."""

    def increment(self, *args, **kwargs):
        """Refuse to retry when a person is waiting for the answer."""
        if _interactive_request.get():
            error, stacktrace = _retry_error(args, kwargs)
            if error is not None:
                raise reraise(type(error), error, stacktrace)
            # A response based retry (redirect, status): nothing to re-raise,
            # so just signal that the budget is spent.
            return self.new(total=0, connect=0, read=0, redirect=0, status=0)
        return super().increment(*args, **kwargs)


@contextmanager
def interactive_requests(budget=None, deadline=None):
    """Mark every request made inside the block as user facing (search).

    Only the waiting time of a person is shortened: the read timeout drops to
    ``settings.SEARCH_READ_TIMEOUT`` and the transport stops retrying. Calls
    outside the block (imports, syncs, achievements) keep the wide read timeout
    and the retries, because losing an item there is worse than being slow.

    ``budget`` caps the whole block, not one request: a search is several
    requests in a row (the store call, then one cover lookup per game), so a
    per request timeout alone still adds up. Inside the block every timeout is
    clamped to what is left of the budget, and a call that would start after
    the budget is spent fails immediately instead of waiting.

    ``deadline`` is that same cap as an absolute ``time.monotonic`` value, for
    callers that run on another thread: a context variable is not carried into
    a ``ThreadPoolExecutor`` worker, so the cover lookups of a search are
    handed the deadline of the search they belong to instead of silently
    starting a fresh budget each.
    """
    request_token = _interactive_request.set(True)
    existing = _interactive_deadline.get()
    if deadline is not None:
        chosen = deadline
    elif existing is not None:
        # A nested block must not restart the clock: the deadline of the
        # outermost block is the one the person is actually waiting on.
        chosen = existing
    else:
        if budget is None:
            budget = getattr(settings, "SEARCH_TOTAL_BUDGET", 9)
        chosen = time.monotonic() + budget
    deadline_token = _interactive_deadline.set(chosen)
    try:
        yield
    finally:
        _interactive_deadline.reset(deadline_token)
        _interactive_request.reset(request_token)


def interactive_deadline():
    """Return the absolute ``time.monotonic`` deadline, or None outside a block.

    Used to carry the budget into a worker thread, which does not inherit the
    context variables of the thread that spawned it.
    """
    return _interactive_deadline.get()


def interactive_budget_remaining(deadline=None):
    """Return the seconds left of the interactive budget, or None outside it."""
    if deadline is None:
        deadline = _interactive_deadline.get()
    if deadline is None:
        return None
    return deadline - time.monotonic()


CONNECT_RETRIES = _InteractiveRetry(total=3, connect=0)

session.mount("http://", HTTPAdapter(max_retries=CONNECT_RETRIES))
session.mount("https://", HTTPAdapter(max_retries=CONNECT_RETRIES))

session.mount(
    "https://api.myanimelist.net/v2",
    LimiterAdapter(per_minute=30),
)
session.mount(
    "https://graphql.anilist.co",
    LimiterAdapter(per_minute=85),
)
session.mount(
    "https://api.igdb.com/v4",
    LimiterAdapter(per_second=3),
)
session.mount(
    "https://api.tvmaze.com",
    LimiterAdapter(per_second=2),
)
session.mount(
    "https://comicvine.gamespot.com/api",
    LimiterAdapter(per_hour=190),
)
session.mount(
    "https://openlibrary.org",
    LimiterAdapter(per_minute=20),
)
session.mount(
    "https://api.hardcover.app/v1/graphql",
    LimiterAdapter(per_minute=50),
)
session.mount(
    "https://boardgamegeek.com/xmlapi2",
    LimiterAdapter(per_second=2),
)


class ProviderAPIError(Exception):
    """Exception raised when a provider API fails to respond."""

    def __init__(self, provider, error, details=None):
        """Initialize the exception with the provider name."""
        self.provider = provider
        response = getattr(error, "response", None)
        self.status_code = getattr(response, "status_code", None)
        try:
            provider_label = Sources(provider).label
        except ValueError:
            provider_label = provider.title()
        # Kept on the instance so callers can name the provider in their own
        # messages without re-implementing the Sources lookup.
        self.provider_label = provider_label

        error_text = getattr(response, "text", str(error))
        logger.error("%s error: %s", provider_label, error_text)

        message = f"There was an error contacting the {provider_label} API"
        if self.status_code is None:
            message += " (network error)"
        else:
            message += f" (HTTP {self.status_code})"
        if details:
            message += f": {details}"
        message += ". Check the logs for more details."
        super().__init__(message)


class ProviderUnavailableError(ProviderAPIError, requests.exceptions.RequestException):
    """Raised when a provider cannot be reached at all.

    This is a network-level failure (refused connection, connect timeout, read
    timeout, short circuit) rather than an answer from the provider, so it is
    kept apart from "the provider answered and found nothing".

    It is also a ``requests.RequestException`` on purpose: the importers and
    the best-effort helpers of the providers already treat that as "the host
    is unreachable, skip it", and they must keep behaving that way.
    """



def raise_not_found_error(provider, media_id, media_type="item"):
    """
    Raise a 404 ProviderAPIError for when a media item is not found.

    Args:
        provider: The provider source value (e.g., Sources.COMICVINE.value)
        media_id: The media ID that was not found
        media_type: The type of media (e.g., "comic", "game", "book")
    """
    error_msg = f"{media_type.capitalize()} with ID {media_id} not found"
    logger.error("%s: %s", provider, error_msg)

    # Create a mock 404 error response
    mock_response = type(
        "obj",
        (object,),
        {
            "status_code": 404,
            "text": error_msg,
        },
    )()
    mock_error = requests.exceptions.HTTPError(response=mock_response)

    raise ProviderAPIError(provider, mock_error, error_msg)


# A provider that timed out or refused the connection once is skipped for a
# minute instead of being retried on every keystroke: a dead host costs one
# connect timeout per request otherwise, which is what froze the search page.
PROVIDER_DOWN_TTL = 60  # seconds


def _provider_down_key(provider):
    """Return the cache key holding the short-circuit flag of a provider.

    The provider is lowercased because the callers pass both the source value
    (``"steam"``) and the enum name (``"STEAM"``) for the same provider, and
    the flag has to be shared between them.
    """
    return f"provider_down:{str(provider).strip().lower()}"


def is_provider_down(provider):
    """Return whether the provider failed on the network within the TTL."""
    try:
        return cache.get(_provider_down_key(provider)) is not None
    except Exception:  # a cache outage must not break requests
        logger.exception("Could not read the short-circuit flag of %s", provider)
        return False


def mark_provider_down(provider):
    """Short-circuit a provider for the TTL after a network failure."""
    try:
        cache.set(_provider_down_key(provider), value=True, timeout=PROVIDER_DOWN_TTL)
    except Exception:  # a cache outage must not break requests
        logger.exception("Could not set the short-circuit flag of %s", provider)


def clear_provider_down(provider):
    """Drop the short-circuit flag of a provider after a success."""
    try:
        cache.delete(_provider_down_key(provider))
    except Exception:  # a cache outage must not break requests
        logger.exception("Could not clear the short-circuit flag of %s", provider)


def api_request(  # noqa: PLR0913 - the signature is part of the public API
    provider,
    method,
    url,
    *,
    params=None,
    data=None,
    headers=None,
    response_format="json",
    timeout=None,
):
    """Make a request to the API and return the response.

    Args:
        provider: Provider identifier for error messages
        method: HTTP method ("GET" or "POST")
        url: Request URL
        params: Query params for GET, JSON body for POST
        data: Raw data for POST
        headers: Request headers
        response_format: "json" (default) or "xml" for XML parsing
        timeout: Optional override, either a number of seconds or a
            ``(connect, read)`` tuple. Defaults to a tuple that caps only the
            connect phase, so a host that never answers fails fast while a
            slow but reachable API keeps the generous read timeout.

    Returns:
        Parsed JSON dict or ElementTree for XML

    Raises:
        ProviderUnavailableError: the provider could not be reached at all,
            either because it is short-circuited or because the request failed
            on the network.
    """
    if timeout is None:
        timeout = (
            getattr(settings, "API_CONNECT_TIMEOUT", 5),
            settings.SEARCH_READ_TIMEOUT
            if _interactive_request.get()
            else settings.REQUEST_TIMEOUT,
        )

    remaining = interactive_budget_remaining()
    if remaining is not None:
        # Never wait longer than the whole search is allowed to take: an
        # exhausted budget means this hop is not worth starting at all.
        if remaining <= 0:
            raise ProviderUnavailableError(
                provider,
                requests.exceptions.Timeout(
                    f"{provider} was skipped, the search ran out of time",
                ),
            )
        connect, read = (
            timeout if isinstance(timeout, tuple) else (timeout, timeout)
        )
        timeout = (min(connect, remaining), min(read, remaining))

    if is_provider_down(provider):
        logger.warning(
            "%s is short-circuited, the request to %s is skipped",
            provider,
            url,
        )
        raise ProviderUnavailableError(
            provider,
            requests.exceptions.ConnectionError(
                f"{provider} was unreachable moments ago "
                f"(short-circuited for up to {PROVIDER_DOWN_TTL} seconds)",
            ),
        )

    try:
        request_kwargs = {
            "url": url,
            "headers": headers,
            "timeout": timeout,
        }

        if method == "GET":
            request_kwargs["params"] = params
            request_func = session.get
        elif method == "POST":
            request_kwargs["data"] = data
            request_kwargs["json"] = params
            request_func = session.post

        response = request_func(**request_kwargs)
        response.raise_for_status()

    except requests.exceptions.HTTPError as error:
        error_resp = error.response
        status_code = error_resp.status_code

        # handle rate limiting
        if status_code == requests.codes.too_many_requests:
            seconds_to_wait = int(error_resp.headers.get("Retry-After", 5))
            logger.warning("Rate limited, waiting %s seconds", seconds_to_wait)
            time.sleep(seconds_to_wait + 3)
            logger.info("Retrying request")
            return api_request(
                provider,
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                response_format=response_format,
                timeout=timeout,
            )

        raise error from None

    except requests.exceptions.RequestException as error:
        # The provider answered nothing at all: refused, or timed out before a
        # single byte came back. An HTTP status is an answer and never lands
        # here, so the short circuit only ever trips on unreachable hosts.
        mark_provider_down(provider)
        raise ProviderUnavailableError(provider, error) from error

    clear_provider_down(provider)

    if response_format == "xml":
        return ElementTree.fromstring(response.text)
    return response.json()


def get_media_metadata(
    media_type,
    media_id,
    source,
    season_numbers=None,
    episode_number=None,
):
    """Return the metadata for the selected media."""
    if source == Sources.MANUAL.value:
        if media_type == MediaTypes.SEASON.value:
            return manual.season(media_id, season_numbers[0])
        if media_type == MediaTypes.EPISODE.value:
            return manual.episode(media_id, season_numbers[0], episode_number)
        if media_type == "tv_with_seasons":
            media_type = MediaTypes.TV.value
        return manual.metadata(media_id, media_type)

    if source == Sources.STEAM.value:
        # Steam games carry their own title and cover, no API to ask
        return steam.metadata(media_id, media_type)

    metadata_retrievers = {
        MediaTypes.ANIME.value: lambda: mal.anime(media_id),
        MediaTypes.MANGA.value: lambda: (
            mangaupdates.manga(media_id)
            if source == Sources.MANGAUPDATES.value
            else mal.manga(media_id)
        ),
        MediaTypes.TV.value: lambda: (
            douban_tv.tv(media_id)
            if source == Sources.DOUBAN.value
            else tmdb.tv(media_id)
        ),
        "tv_with_seasons": lambda: (
            douban_tv.tv_with_seasons(media_id, season_numbers)
            if source == Sources.DOUBAN.value
            else tmdb.tv_with_seasons(media_id, season_numbers)
        ),
        MediaTypes.SEASON.value: lambda: (
            douban_tv.tv_with_seasons(media_id, season_numbers)[
                f"season/{season_numbers[0]}"
            ]
            if source == Sources.DOUBAN.value
            else tmdb.tv_with_seasons(media_id, season_numbers)[
                f"season/{season_numbers[0]}"
            ]
        ),
        MediaTypes.EPISODE.value: lambda: tmdb.episode(
            media_id,
            season_numbers[0],
            episode_number,
        ),
        MediaTypes.MOVIE.value: lambda: (
            douban_movie.movie(media_id)
            if source == Sources.DOUBAN.value
            else tmdb.movie(media_id)
        ),
        MediaTypes.GAME.value: lambda: igdb.game(media_id),
        MediaTypes.BOOK.value: lambda: (
            douban.book(media_id)
            if source == Sources.DOUBAN.value
            else weread.book(media_id)
            if source == Sources.WEREAD.value
            else hardcover.book(media_id)
            if source == Sources.HARDCOVER.value
            else openlibrary.book(media_id)
        ),
        MediaTypes.COMIC.value: lambda: comicvine.comic(media_id),
        MediaTypes.BOARDGAME.value: lambda: bgg.boardgame(media_id),
    }
    return metadata_retrievers[media_type]()


def search(media_type, query, page, source=None):
    """Search for media based on the query and return the results."""
    search_handlers = {
        MediaTypes.MANGA.value: lambda: (
            mangaupdates.search(query, page)
            if source == Sources.MANGAUPDATES.value
            else mal.search(media_type, query, page)
        ),
        MediaTypes.ANIME.value: lambda: mal.search(media_type, query, page),
        MediaTypes.TV.value: lambda: (
            douban_tv.search(query, page)
            if source == Sources.DOUBAN.value
            else tmdb.search(media_type, query, page)
        ),
        MediaTypes.MOVIE.value: lambda: (
            douban_movie.search(query, page)
            if source == Sources.DOUBAN.value
            else tmdb.search(media_type, query, page)
        ),
        MediaTypes.SEASON.value: lambda: tmdb.search(MediaTypes.TV.value, query, page),
        MediaTypes.EPISODE.value: lambda: tmdb.search(MediaTypes.TV.value, query, page),
        MediaTypes.GAME.value: lambda: (
            steam.search(query, page)
            if source == Sources.STEAM.value
            else igdb.search(query, page)
        ),
        MediaTypes.BOOK.value: lambda: (
            douban.search(query, page)
            if source == Sources.DOUBAN.value
            else weread.search(query, page)
            if source == Sources.WEREAD.value
            else openlibrary.search(query, page)
            if source == Sources.OPENLIBRARY.value
            else hardcover.search(query, page)
        ),
        MediaTypes.COMIC.value: lambda: comicvine.search(query, page),
        MediaTypes.BOARDGAME.value: lambda: bgg.search(query, page),
    }
    # A person is waiting on this one, so the handler runs on the short,
    # retry-free budget of ``interactive_requests``. Every other caller of the
    # same providers (imports, syncs, achievements) stays on the wide one.
    with interactive_requests():
        return search_handlers[media_type]()
