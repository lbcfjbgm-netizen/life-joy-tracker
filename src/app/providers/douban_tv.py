"""Douban TV provider.

The second phase of the Douban import: films are handled by
``app.providers.douban_movie``, this module imports the shows.

The same ``rexxar`` API behind ``m.douban.com`` serves both. What was checked
by hand on 2026-10-22 for films still holds: an iPhone user agent is needed,
otherwise the mobile web page comes back instead of JSON, and
``Referer: https://m.douban.com/`` is required as well.

``GET /rexxar/api/v2/tv/<id>`` is the detail call for a show. The payload has
the same 77 fields as the film one, with these differences:

``episodes_count``
    Number of episodes in **this season** (Douban keeps every season as its own
    subject, see below). Not the length of ``episodes``, which can be shorter
    when Douban only lists part of the run.

``seasons_count`` / ``seasons``
    **Neither field exists.** All 77 keys were listed on three shows and there is
    no ``seasons_count``, no ``seasons``: Douban keeps every season as its own
    subject, so a subject holds exactly one season and the count would always be
    one. Nothing here can say how many seasons a show has, and the code must not
    pretend otherwise.

``durations``
    Running time of **one episode**, not of the season, so it maps to the
    episode length on the details page and never to a total.

``episodes``
    Usually absent. When present it is a list of ``{"id", "title", "url",
    "playable", "cover", "episode", "release_date"}`` and is not needed by the
    importer: the season is created with the episode count from
    ``episodes_count``.

``pubdate``
    First air date of the season, a list of strings like
    ``"2011-04-17(中国大陆)"``. It becomes ``first_air_date``.

Seasons
-------
Douban gives every season its own subject, so "Game of Thrones Season 7" is a
separate id from "Game of Thrones Season 1". This provider therefore maps
**one subject to one show**, which is the shape the app can store without
guessing: the title stays as Douban wrote it (season marker included) and the
row is a plain TV entry. Grouping the subjects of a show back together would
mean parsing "第七季" / "Season 2" / "第二部" out of titles, and a wrong guess
merges two shows that a re-run cannot undo.

Two fields share the name ``rating`` and must not be mixed up:

- ``interest.rating.value`` is the **user's own** 1-5 stars, written as a note
- ``subject.rating.value`` is the **community average**, 0-10, and is the score

Searching shows
---------------
``GET https://movie.douban.com/j/subject_suggest?q=...`` is the endpoint behind
the search box on Douban, and it serves both films and shows from one list.

The catch, measured on 24 entries across four searches on 2026-10-23: the
``type`` field is **not** usable. It says ``movie`` on every entry, shows
included — 亮剑 (30 episodes) and 神探夏洛克 both came back as ``type: movie``,
so filtering on ``type == "tv"`` returns nothing, which is why search looked
broken. What does separate them is ``episode``: the episode count as a string
(``"30"``, ``"3"``) on a show, the empty string on a film. That rule was checked
against the ``subtype`` of the detail payload on 8 entries and agreed 8 of 8.

The endpoint also throttles by answering ``200`` with ``[]`` instead of the
``400`` + ``1309`` used elsewhere. An empty answer therefore cannot be told
apart from "no match", so it is cached for ``EMPTY_SEARCH_CACHE_TIMEOUT``
seconds rather than the usual day.
"""

import logging
import re

import requests
from django.conf import settings
from django.core.cache import cache

from app.models import Item, MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

BASE_URL = "https://m.douban.com/rexxar/api/v2"
TV_PATH = "tv"
SUBJECT_URL = "https://movie.douban.com/subject/{media_id}/"

# The same suggestion endpoint the search box on Douban uses. It answers shows
# and films from one list, which is why the entries have to be told apart here.
SEARCH_URL = "https://movie.douban.com/j/subject_suggest"

# An empty answer is cached briefly instead of for the usual day. Douban does not
# only refuse with a 400; it also answers ``200`` with ``[]`` once the IP has
# asked too often, and that answer looks exactly like "nothing matched". Caching
# it for a day would keep a throttled search empty until tomorrow.
EMPTY_SEARCH_CACHE_TIMEOUT = 120

# The film provider is imported inside the functions that need it, not here.
# ``app.providers.services`` imports this module and ``douban_movie`` in the same
# statement, so a module level ``from app.providers import douban_movie`` lands on
# a half built module and reads its constants before they exist. Reaching for it
# at call time keeps the two providers independent and still shares one throttle.

SERIES_SUBTYPE = "tv"

# Yamtrack scores out of ten, Douban's community average already does
SCORE_MAX = 10

# How many cast members the details page has room for, same cap as the films
CAST_DISPLAY_LIMIT = 30

# A show with no episode count still has to be openable as a tracked entry, so
# it falls back to one episode instead of a ``max_progress`` of zero. Zero
# would make "completed at 0 of 0" look untracked on the list page.
FALLBACK_EPISODES = 1

RE_MINUTES = re.compile(r"(\d+)\s*分钟")
RE_HOURS = re.compile(r"(\d+)\s*小时")
RE_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
RE_YEAR = re.compile(r"^(\d{4})")


def movies():
    """Return the film provider, imported at call time to avoid a cycle."""
    from app.providers import douban_movie  # noqa: PLC0415

    return douban_movie


def is_rate_limited(response):
    """Return whether a response is Douban's IP rate limit."""
    return movies().is_rate_limited(response)


def handle_error(error):
    """Raise a provider error the same way the film provider does."""
    movies().handle_error(error)


def is_blocked(error):
    """Return whether a provider error means "stop asking for a while"."""
    return movies().is_blocked(error)


def is_rate_limited_error(error):
    """Return whether a provider error is Douban's IP rate limit."""
    return movies().is_rate_limited_error(error)


def get(url, **kwargs):
    """Fetch a Douban URL through the shared throttle and the same proxy.

    ``douban_movie.get`` already pauses for ``REQUEST_DELAY``, reads
    ``DOUBAN_PROXY`` on every call and logs a block, so the two imports queue
    behind each other instead of each holding half of Douban's budget.
    """
    return movies().get(url, **kwargs)


def fixed_poster(url):
    """Return a poster URL that renders, or ``IMG_NONE``.

    Douban's ``img9`` shard answers an HTML page where every other shard serves
    the JPEG, so the link is moved to ``img3``. The URL then goes through the
    local image relay, because a browser asking Douban for a cover without a
    Douban referer is refused.
    """
    return movies().fixed_poster(url)


def large_poster(url):
    """Return the big variant of a search thumbnail, or ``IMG_NONE``."""
    return movies().large_poster(url)


def parse_runtime(durations):
    """Return the length of one episode in whole minutes, or None.

    ``durations`` on a show is the length of a single episode (``["45分钟"]``),
    never the length of the whole season: the same field on a film means the
    film's running time, and on a show it means one episode.
    """
    return movies().parse_runtime(durations)


def readable_duration(minutes):
    """Return a running time in minutes as ``45m``."""
    return movies().readable_duration(minutes)


def parse_release_date(value):
    """Return ``YYYY-MM-DD`` out of a Douban date string, or None."""
    return movies().parse_release_date(value)


def release_date(subject):
    """Return the first air date of a season as ``YYYY-MM-DD``, or None."""
    return movies().release_date(subject)


def get_rating(subject):
    """Return ``(score, count)`` of the community average, 0-10.

    This is ``subject.rating``, the average of everybody who rated the show,
    never the caller's own stars.
    """
    return movies().get_rating(subject)


def get_names(people):
    """Return the names of a list of ``{"name": ...}`` entries."""
    return movies().get_names(people)


def get_cast(actors):
    """Return the cast list shaped the way the details page renders it."""
    return movies().get_cast(actors)


def get_genres(subject):
    """Return the genres of a show."""
    return movies().get_genres(subject)


def clean_text(text):
    """Strip the markup and the extra whitespace Douban wraps text in."""
    return movies().clean_text(text)


def get_synopsis(subject):
    """Return the synopsis of a show."""
    return movies().get_synopsis(subject)


def get_country(subject):
    """Return the countries of a show as one readable line."""
    return movies().get_country(subject)


def get_languages(subject):
    """Return the languages of a show."""
    return movies().get_languages(subject)


def episodes_count(subject):
    """Return how many episodes this season has, at least one.

    ``episodes_count`` is the number Douban shows under the poster. It is
    missing on an unreleased show, and occasionally zero, and a show with no
    episode count cannot be opened as a tracked entry: the progress bar would
    divide by zero and the entry would look like it was never watched. One is
    the honest floor, since Douban does not list a show with no episode at all.
    """
    try:
        count = int(subject.get("episodes_count") or 0)
    except (TypeError, ValueError):
        count = 0

    return count if count > 0 else FALLBACK_EPISODES


def process_tv(response):
    """Turn one show payload into the metadata ``tmdb.tv`` also returns.

    The keys match that function, because the details page, the list page and
    the tracking code all read the same shape. What a show cannot know without
    a second call is left empty rather than guessed:

    - ``related.seasons`` is a single entry describing this one subject, so the
      season dropdown on the season page has exactly the season that exists
    - ``episodes`` is not part of the show payload; the season page asks the
      season for it
    """
    subject = response
    media_id = str(subject.get("id") or "")
    score, score_count = get_rating(subject)
    actors = subject.get("actors") or []
    runtime = parse_runtime(subject.get("durations"))
    count = episodes_count(subject)
    date = release_date(subject)
    title = (subject.get("title") or "").strip() or media_id

    return {
        "media_id": media_id,
        "source": Sources.DOUBAN.value,
        "source_url": SUBJECT_URL.format(media_id=media_id),
        "media_type": MediaTypes.TV.value,
        "title": title,
        "max_progress": count,
        "image": fixed_poster(subject.get("cover_url")),
        "synopsis": get_synopsis(subject),
        "genres": get_genres(subject),
        "score": score,
        "score_count": score_count,
        "release_date": date,
        # One episode in minutes. The details page prints it as 单集时长.
        "episode_runtime": runtime,
        "details": {
            "format": "TV",
            "first_air_date": date,
            "episodes": count,
            "runtime": readable_duration(runtime),
            "country": get_country(subject),
            "languages": get_languages(subject),
            "status": "Released" if subject.get("is_released") else None,
        },
        # Douban has no per-person photo on this endpoint, and no character
        # name at all, so the cards fall back to the name alone
        "cast": get_cast(actors)[:CAST_DISPLAY_LIMIT],
        "total_cast_count": len(actors),
        "directors": [{"name": name} for name in get_names(subject.get("directors"))],
        # A single season, the one this subject is. The season page reads
        # ``season/<n>`` out of ``tv_with_seasons``, which is built from here.
        "related": {
            "seasons": [
                {
                    "source": Sources.DOUBAN.value,
                    "media_type": MediaTypes.SEASON.value,
                    "media_id": media_id,
                    "title": title,
                    "season_number": DEFAULT_SEASON,
                    "season_title": title,
                    "first_air_date": date,
                    "image": fixed_poster(subject.get("cover_url")),
                    "max_progress": count,
                },
            ],
        },
        "external_links": {"Douban": SUBJECT_URL.format(media_id=media_id)},
    }


# Douban keeps every season as its own subject, so the season this subject holds
# is always the first one. Written as a name rather than a bare 1 so the reason
# survives the next reader.
DEFAULT_SEASON = 1


def process_season(response):
    """Return the season metadata of the one season a Douban subject holds.

    The season page asks ``tv_with_seasons`` for this, and feeds it to
    ``tmdb.process_episodes``. Douban sends no episode list for a show, so the
    episodes are generated from ``episodes_count``: the count is real, and the
    episode numbers are what the season is made of. No title, still and air
    date can be invented for them, so those stay null/empty and the season page
    renders the list without them.
    """
    subject = response
    media_id = str(subject.get("id") or "")
    count = episodes_count(subject)
    date = release_date(subject)
    title = (subject.get("title") or "").strip() or media_id
    runtime = parse_runtime(subject.get("durations"))

    return {
        "source": Sources.DOUBAN.value,
        "media_type": MediaTypes.SEASON.value,
        "media_id": media_id,
        "title": title,
        "season_title": title,
        "max_progress": count,
        "image": fixed_poster(subject.get("cover_url")),
        "season_number": DEFAULT_SEASON,
        "synopsis": get_synopsis(subject),
        "score": get_rating(subject)[0],
        "score_count": get_rating(subject)[1],
        "details": {
            "first_air_date": date,
            "last_air_date": None,
            "episodes": count,
            "runtime": readable_duration(runtime),
        },
        "episodes": build_episodes(count, date),
    }


def build_episodes(count, date):
    """Return the episode list of a season, generated from the episode count.

    Douban does not list the episodes of a show on this endpoint, so each entry
    carries only what is known: which episode it is, and the season's first air
    date, which is used as every episode's date rather than left empty. That
    keeps the "released" check on the season page working — without a date the
    episode is treated as not aired yet, and a finished season would be shown as
    still in progress.
    """
    episodes = []
    for number in range(1, count + 1):
        episodes.append(
            {
                "episode_number": number,
                "name": "",
                "overview": "",
                "air_date": date,
                "still_path": None,
                "runtime": None,
                "vote_average": None,
                "vote_count": 0,
            },
        )
    return episodes


def request_subject(media_id):
    """Return the detail payload of a show, or raise a provider error."""
    try:
        response = get(f"{BASE_URL}/{TV_PATH}/{media_id}")
        response.raise_for_status()
    except requests.RequestException as error:
        handle_error(error)

    payload = _json_or_none(response)
    if payload is None:
        services.raise_not_found_error(Sources.DOUBAN.value, media_id, "tv")

    # A film id answers 200 here too, it just carries ``subtype: movie``. Opening
    # a film as a show would put a film's metadata on a show row.
    if payload.get("subtype") and payload.get("subtype") != SERIES_SUBTYPE:
        services.raise_not_found_error(Sources.DOUBAN.value, media_id, "tv")

    return payload


def _json_or_none(response):
    """Return the JSON body of a response, or None when it is not JSON."""
    try:
        return response.json()
    except ValueError:
        return None


def cache_get(key):
    """Return a cached value, or None when the cache cannot be reached.

    Redis is a cache, not storage: when it is down (a restart of the site, a
    stopped service) reading it raises ``ConnectionError``. Letting that out would
    turn one show into an import failure for a reason that has nothing to do with
    the show, so an unreachable cache is treated as an empty one.
    """
    try:
        return cache.get(key)
    except Exception as error:  # noqa: BLE001
        logger.warning("Douban cache read failed for %s: %s", key, error)
        return None


def cache_set(key, value, timeout=None):
    """Store a value in the cache, ignoring a cache that cannot be reached.

    ``timeout`` is the number of seconds to keep it; ``None`` uses the project
    wide default, which is one day.
    """
    try:
        cache.set(key, value, timeout)
    except Exception as error:  # noqa: BLE001
        # A lost cache entry only costs one more call to Douban later on
        logger.warning("Douban cache write failed for %s: %s", key, error)


def tv(media_id):
    """Return the metadata of a Douban show.

    Everything the details page shows is already stored by the importer, so a
    Douban that will not answer right now must not turn an imported show into a
    500: the stored copy is served instead.
    """
    cache_key = f"{Sources.DOUBAN.value}_{MediaTypes.TV.value}_{media_id}"
    data = cache_get(cache_key)
    if data is not None:
        return data

    try:
        data = process_tv(request_subject(media_id))
    except services.ProviderAPIError as error:
        fallback = stored_tv(media_id)
        if fallback is None:
            raise
        logger.warning(
            "Douban unavailable for show %s (%s), serving the stored copy",
            media_id,
            error,
        )
        return fallback

    cache_set(cache_key, data)
    return data


def tv_with_seasons(media_id, season_numbers):
    """Return the show plus the metadata of the season that was asked for.

    The shape is ``{...show, "season/1": {...}}``, the same one ``tmdb`` returns,
    because ``app.views.season_details`` and the season model both read
    ``tv_with_seasons_metadata[f"season/{n}"]``. A Douban subject is one season,
    so only season one exists and any other number gets nothing back: the show
    is returned alone and the caller's lookup raises a ``KeyError`` rather than
    being shown a season that is not there.
    """
    show = tv(media_id)

    if not season_numbers or DEFAULT_SEASON not in season_numbers:
        return show

    cache_key = season_cache_key(media_id, DEFAULT_SEASON)
    season = cache_get(cache_key)
    if season is None:
        try:
            season = process_season(request_subject(media_id))
        except services.ProviderAPIError as error:
            season = stored_season(media_id)
            if season is None:
                raise
            logger.warning(
                "Douban unavailable for show %s season (%s), serving the stored copy",
                media_id,
                error,
            )
        else:
            cache_set(cache_key, season)

    return show | {f"season/{DEFAULT_SEASON}": season}


def season(media_id, season_number):
    """Return the metadata of the season of a show."""
    data = tv_with_seasons(media_id, [season_number])
    return data[f"season/{season_number}"]


def season_cache_key(media_id, season_number):
    """Return the key a season is cached under."""
    return (
        f"{Sources.DOUBAN.value}_{MediaTypes.SEASON.value}_{media_id}"
        f"_{season_number}"
    )


def stored_tv(media_id):
    """Return the metadata of a show from the item the importer stored."""
    item = Item.objects.filter(
        media_id=str(media_id),
        source=Sources.DOUBAN.value,
        media_type=MediaTypes.TV.value,
    ).first()

    if item is None:
        return None

    count = stored_episodes(media_id)
    cast = item.cast or []
    title = item.title
    date = item.release_date.isoformat() if item.release_date else None

    return {
        "media_id": item.media_id,
        "source": Sources.DOUBAN.value,
        "source_url": SUBJECT_URL.format(media_id=item.media_id),
        "media_type": MediaTypes.TV.value,
        "title": title,
        "max_progress": count,
        "image": item.image,
        "synopsis": item.synopsis or "No synopsis available.",
        "genres": item.genres or [],
        "score": None,
        "score_count": None,
        "release_date": date,
        "details": {
            "format": "TV",
            "first_air_date": date,
            "episodes": count,
        },
        "cast": cast[:CAST_DISPLAY_LIMIT],
        "total_cast_count": len(cast),
        "directors": item.directors or [],
        "related": {
            "seasons": [
                {
                    "source": Sources.DOUBAN.value,
                    "media_type": MediaTypes.SEASON.value,
                    "media_id": item.media_id,
                    "title": title,
                    "season_number": DEFAULT_SEASON,
                    "season_title": title,
                    "first_air_date": date,
                    "image": item.image,
                    "max_progress": count,
                },
            ],
        },
        "external_links": {"Douban": SUBJECT_URL.format(media_id=item.media_id)},
    }


def stored_season(media_id):
    """Return the season metadata from what the importer stored, or None."""
    season_item = (
        Item.objects.filter(
            media_id=str(media_id),
            source=Sources.DOUBAN.value,
            media_type=MediaTypes.SEASON.value,
            season_number=DEFAULT_SEASON,
        )
        .select_related()
        .first()
    )

    if season_item is None:
        return None

    stored = stored_tv(media_id)
    if stored is None:
        return None

    count = stored["max_progress"]
    return {
        "source": Sources.DOUBAN.value,
        "media_type": MediaTypes.SEASON.value,
        "media_id": media_id,
        "title": season_item.title,
        "season_title": season_item.title,
        "max_progress": count,
        "image": season_item.image,
        "season_number": DEFAULT_SEASON,
        "synopsis": season_item.synopsis or "No synopsis available.",
        "score": None,
        "score_count": None,
        "details": {
            "first_air_date": stored["release_date"],
            "last_air_date": None,
            "episodes": count,
        },
        "episodes": build_episodes(count, stored["release_date"]),
    }


def stored_episodes(media_id):
    """Return the episode count stored for a show, at least one.

    The item does not carry the count, the season item's ``total_pages`` is for
    books, and the season row's own progress needs the episodes. The count is
    therefore read back from the number of episode items that were stored for
    the season, which the importer creates from ``episodes_count``.
    """
    count = Item.objects.filter(
        media_id=str(media_id),
        source=Sources.DOUBAN.value,
        media_type=MediaTypes.EPISODE.value,
        season_number=DEFAULT_SEASON,
    ).count()

    return count if count > 0 else FALLBACK_EPISODES


def is_show_entry(entry):
    """Return whether one suggestion entry is a show rather than a film.

    The ``type`` field cannot answer this: Douban labels **every** entry
    ``movie``, shows included, which was measured on 24 entries across four
    searches (see the module docstring). The field that does separate them is
    ``episode``: it holds the episode count as a string (``"30"`` on 亮剑,
    ``"3"`` on 神探夏洛克) and is the empty string on every film checked, so a
    non-empty value is what marks a show.
    """
    return bool(str(entry.get("episode") or "").strip()) and bool(entry.get("id"))


def search(query, page):
    """Search Douban shows and return a page of results.

    The shape matches ``douban_movie.search``: the same suggestion endpoint, the
    same keys per result, and the same helpers. Only the type filter differs, in
    that films are dropped here and shows are kept.
    """
    from app import helpers  # noqa: PLC0415

    cache_key = f"search_{Sources.DOUBAN.value}_{MediaTypes.TV.value}_{query}_{page}"
    data = cache_get(cache_key)
    if data is not None:
        return data

    try:
        response = get(SEARCH_URL, params={"q": query})
        response.raise_for_status()
    except requests.RequestException as error:
        # A blocked or throttled Douban must not turn the search page into a 500:
        # the page renders an empty result set, which is what a user with no hit
        # sees anyway, and nothing is cached so the next search asks again. The
        # 400 + 1309 limit arrives as an HTTPError here, so it lands in this
        # branch as well rather than reaching the error page.
        logger.warning("Douban show search failed for %r: %s", query, error)
        per_page = settings.PER_PAGE
        return helpers.format_search_response(page, per_page, 0, [])

    try:
        entries = response.json()
    except ValueError:
        entries = []

    if not isinstance(entries, list):
        entries = []

    shows = [entry for entry in entries if is_show_entry(entry)]

    results = [
        {
            "media_id": str(entry["id"]),
            "source": Sources.DOUBAN.value,
            "media_type": MediaTypes.TV.value,
            "title": entry.get("title") or "",
            "image": movies().large_poster(entry.get("img")),
            "release_year": _year(entry.get("year")),
        }
        for entry in shows
    ]

    per_page = settings.PER_PAGE
    start = (page - 1) * per_page
    data = helpers.format_search_response(
        page,
        per_page,
        len(results),
        results[start : start + per_page],
    )

    cache_set(cache_key, data, EMPTY_SEARCH_CACHE_TIMEOUT if not results else None)
    return data


def _year(raw):
    """Return a year as an int, or None."""
    match = RE_YEAR.match(str(raw or "").strip())
    return int(match.group(1)) if match else None
