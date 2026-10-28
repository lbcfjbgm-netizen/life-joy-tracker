r"""Chinese names for the Steam items, from a hand written table first.

The library imported from Steam carries the name the Web API returns, which is
the one Valve ships to every language, so most cards are titled in plain Latin
letters. The search screen looks at ``title``, ``title_zh`` and ``title_en``
(``app/views.py::_search_local_items``), which means a game is only findable by
its Chinese name once that name is stored in ``title_zh``.

Two sources feed that column, in this order:

1. **The manual table**, ``.tools/raw/cn_names.json``. It is written by hand and
   therefore always right: a game the operator named once keeps that name, and
   no network answer ever overrides it.
2. **The Steam store**, through ``appdetails`` in Simplified Chinese. It only
   answers with a Chinese name for the games whose publisher filled a localized
   title in (about one in sixty of this library), so it is a bonus, never the
   plan. Asking the store costs one request per game, which is why only the
   command line tool runs it; the daily import sticks to the manual table.

Only ``Item.title_zh`` is ever written here. ``title`` is what the library was
imported with and stays untouched, and a ``title_zh`` that already holds a
different name is left alone too: that value came from the store or from the
operator, and neither is worth losing.

The writes go through ``Item.objects.filter(...).update(...)`` and never through
``save()``: the model refetches the provider metadata on save, which cannot work
outside a request (``app.providers`` is left without its ``services`` module).
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import requests
from django.conf import settings

from app.models import Item

logger = logging.getLogger(__name__)

# The manual table. The file is meant to be edited by hand, so a missing or a
# broken one is never an error: the run simply has no manual name to apply.
MANUAL_NAMES_PATH = (
    Path(settings.BASE_DIR).parent / ".tools" / "raw" / "cn_names.json"
)

STORE_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
STORE_LANGUAGE = "schinese"
STORE_COUNTRY = "CN"

# The store is asked about one game at a time, so the delay between two requests
# is what keeps a full run from looking like an attack. A full library is about
# 2130 candidates, i.e. eleven minutes of asking.
REQUEST_TIMEOUT = 10
THROTTLE_SECONDS = 0.3

# The store is not always reachable from here: the direct route works on some
# days and the operator's proxy is only up when they started it. When that
# happens every request waits for its timeout, and a library of a few thousand
# games would take hours before the run ends. The run gives up on the network
# instead after this many games in a row got no answer, which costs a few
# minutes at worst and never blocks the names of the manual table.
MAX_CONSECUTIVE_FAILURES = 5

# The blocks a Chinese title is written in: the Han ideographs and their
# compatibility forms. The kana blocks are left out on purpose -- a Japanese
# name is no better than the Latin one the library already shows, and what the
# search box is fed is Chinese.
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# Answers already collected during this process, so a daily task that calls the
# store twice pays for the games once. A game the store has no Chinese name for
# is remembered as ``None`` and is not asked about again either.
_AUTO_CACHE: dict[str, str | None] = {}

# A digits only key in the manual table is a Steam App ID; anything else is the
# exact English title of the game.
_NUMERIC_KEY = re.compile(r"^\d+$")

# Set when a run gave up on the store because it kept timing out, so a caller
# can tell "no game has a Chinese name" apart from "the store was unreachable".
LAST_RUN_GAVE_UP = False


def has_cjk(text: str | None) -> bool:
    """Return whether a text holds Chinese characters."""
    return bool(CJK.search(text or ""))


def load_manual_names() -> dict[str, str]:
    """Read the hand written name table.

    The table maps a key to a Chinese name, where the key is either the Steam
    App ID (digits only) or the exact English title of the game. Both shapes are
    returned as they were written: ``apply_names`` decides which column to match
    against.

    A missing file is created as an empty skeleton, so the next hand edit has
    somewhere to go. A broken file only costs the manual names of this run.

    Returns:
        dict: ``{key: Chinese name}``, empty when there is nothing to read
    """
    try:
        raw = MANUAL_NAMES_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        _write_skeleton()
        return {}
    except OSError:
        logger.warning(
            "Could not read the Chinese name table at %s",
            MANUAL_NAMES_PATH,
            exc_info=True,
        )
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "The Chinese name table at %s is not valid JSON, ignoring it",
            MANUAL_NAMES_PATH,
            exc_info=True,
        )
        return {}

    if not isinstance(data, dict):
        logger.warning(
            "The Chinese name table at %s is not an object, ignoring it",
            MANUAL_NAMES_PATH,
        )
        return {}

    # Both shapes are accepted: the full document with a "names" block, and a
    # bare mapping of key to name, so the file stays comfortable to hand edit
    names = data.get("names", data)
    if not isinstance(names, dict):
        logger.warning(
            "The 'names' entry of %s is not an object, ignoring it",
            MANUAL_NAMES_PATH,
        )
        return {}

    clean = {}
    for key, value in names.items():
        name = str(value or "").strip()
        # A key without a value, or a name without Chinese, is a leftover from
        # an edit; storing it would only fill title_zh with what title says
        if not name or not has_cjk(name):
            continue
        clean[str(key).strip()] = name

    logger.debug(
        "Read %d Chinese names from %s (%d entries skipped)",
        len(clean),
        MANUAL_NAMES_PATH,
        len(names) - len(clean),
    )
    return clean


def auto_names(appids) -> dict[str, str]:
    """Ask the Steam store for the Chinese name of the given games.

    The store only carries a localized name for the games its publisher filled
    one in, which is rare in this library, so an answer without Chinese is
    dropped instead of stored: it is the same Latin name the title already has.

    The whole thing is best effort. A game the store cannot answer for is
    skipped silently, the cached answer of this process is reused when there is
    one, and the requests are spaced out so a daily task can call this without
    the run turning slow.

    Args:
        appids (Iterable): Steam App IDs to ask about

    Returns:
        dict: ``{appid: Chinese name}``, only for the games that have one
    """
    names: dict[str, str] = {}
    pending = []

    for appid in (str(value).strip() for value in appids or ()):
        if not appid:
            continue
        if appid in _AUTO_CACHE:
            cached = _AUTO_CACHE[appid]
            if cached:
                names[appid] = cached
            continue
        pending.append(appid)

    if pending:
        names.update(_fetch_uncached(pending))
    return names


def candidate_appids(limit: int | None = None) -> list[str]:
    """Return the Steam games whose title is not in Chinese yet.

    Those are the only games a Chinese name can help: the card already shows a
    Chinese title otherwise, and the search box already finds it.

    Args:
        limit (int, optional): keep at most this many App IDs, all of them when
            it is ``None`` or not positive

    Returns:
        list: Steam App IDs, in library order
    """
    rows = (
        Item.objects.filter(source="steam", media_type="game")
        .order_by("id")
        .values_list("media_id", "title")
        .iterator()
    )

    candidates: list[str] = []
    for media_id, title in rows:
        if has_cjk(title):
            continue
        appid = str(media_id).strip()
        if not appid:
            continue
        candidates.append(appid)
        if limit and len(candidates) >= limit:
            break

    logger.debug(
        "Found %d Steam games without a Chinese title%s",
        len(candidates),
        f" (stopped at {limit})" if limit else "",
    )
    return candidates


def apply_names(names, *, dry_run: bool = False) -> dict:
    """Store the given Chinese names in ``Item.title_zh``.

    Three rules, in the order they matter:

    * a digits only key is the Steam App ID and is matched against
      ``media_id``; any other key is the English title and is matched against
      ``title`` exactly
    * a row whose ``title_zh`` already holds this name counts as already right,
      and one that holds a *different* name is left alone: a name the store or
      the operator put there outranks this run
    * only ``title_zh`` is written, ``title`` is never touched

    The rows are updated in place, one query per key, so the models of the
    library are never loaded and their ``save`` override never runs.

    Args:
        names (dict): ``{appid or English title: Chinese name}``
        dry_run (bool): count the rows that would be written without writing

    Returns:
        dict: the counts ``updated``, ``skipped``, ``missing`` and ``unchanged``
    """
    counts = {"updated": 0, "skipped": 0, "missing": 0, "unchanged": 0}
    touched: list[str] = []

    for key, value in (names or {}).items():
        name = str(value or "").strip()
        if not name:
            continue

        rows = _match_rows(key)
        if not rows:
            counts["missing"] += 1
            logger.debug("No Steam game in the library for the key %r", key)
            continue

        for item_id, stored in rows:
            current = (stored or "").strip()
            if current == name:
                counts["unchanged"] += 1
                continue
            if current:
                counts["skipped"] += 1
                logger.debug(
                    "Left the Chinese name of item %s alone, it already holds "
                    "%r",
                    item_id,
                    current,
                )
                continue

            if dry_run:
                counts["updated"] += 1
            else:
                counts["updated"] += Item.objects.filter(id=item_id).update(
                    title_zh=name,
                )
            touched.append(f"{key} -> {name}")

    logger.info(
        "Stored %d Chinese names, left %d alone, %d keys matched no game and "
        "%d were already right%s",
        counts["updated"],
        counts["skipped"],
        counts["missing"],
        counts["unchanged"],
        f": {', '.join(touched[:10])}" if touched else "",
    )
    return counts


def resolve_manual_names(appids=None) -> dict[str, str]:
    """Return the manual names keyed by App ID, so callers can count them.

    A title key is resolved through the library: the digits come from the item
    that carries that exact title. Two games can share a title in theory, so a
    title key can resolve to more than one App ID.

    Args:
        appids (Iterable, optional): keep only these App IDs, all of them when
            it is ``None``

    Returns:
        dict: ``{appid: Chinese name}``
    """
    wanted = None if appids is None else {str(appid) for appid in appids}
    resolved: dict[str, str] = {}

    for key, name in load_manual_names().items():
        if _NUMERIC_KEY.match(key):
            if wanted is None or key in wanted:
                resolved[key] = name
            continue

        if wanted is None:
            rows = Item.objects.filter(
                source="steam",
                media_type="game",
                title=key,
            ).values_list("media_id", flat=True)
        else:
            rows = Item.objects.filter(
                source="steam",
                media_type="game",
                title=key,
                media_id__in=wanted,
            ).values_list("media_id", flat=True)

        for appid in rows:
            resolved[str(appid)] = name

    return resolved



def apply_manual_names() -> int:
    """Store the Chinese names of the manual table, returning how many landed.

    A thin wrapper over ``load_manual_names`` and ``apply_names`` for the daily
    import: it reads no network, and the table is the only source it applies, so
    the daily run never slows down on the store.

    Returns:
        int: number of rows whose ``title_zh`` was written
    """
    names = load_manual_names()
    if not names:
        return 0
    return apply_names(names)["updated"]


def _fetch_uncached(appids):
    """Ask the store about the games this process has not seen yet.

    Every game is asked about once, with a pause between two requests, and the
    run gives up after ``MAX_CONSECUTIVE_FAILURES`` unreachable games in a row:
    when the store is down nothing is gained by waiting for the timeout of every
    remaining game.

    Args:
        appids (list): Steam App IDs without a cached answer

    Returns:
        dict: ``{appid: Chinese name}``, only for the games that have one
    """
    found: dict[str, str] = {}
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (yamtrack title_zh)"})
    failures = 0
    gave_up = False

    try:
        for appid in appids:
            reachable, name = _fetch_store_name(session, appid)

            if not reachable:
                failures += 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    gave_up = True
                    logger.warning(
                        "The Steam store could not be reached %d times in a row, "
                        "giving up on the remaining %d games",
                        failures,
                        len(appids) - len(_AUTO_CACHE),
                    )
                    break
                continue

            failures = 0
            # A store name without Chinese is the very Latin name the game is
            # already titled with, so it is cached as "asked, nothing to store"
            _AUTO_CACHE[appid] = name if name and has_cjk(name) else None
            if _AUTO_CACHE[appid]:
                found[appid] = _AUTO_CACHE[appid]
            time.sleep(THROTTLE_SECONDS)
    finally:
        session.close()

    global LAST_RUN_GAVE_UP  # noqa: PLW0603
    LAST_RUN_GAVE_UP = gave_up

    logger.info(
        "The Steam store answered with a Chinese name for %d of %d games%s",
        len(found),
        len(appids),
        " (gave up, the store was unreachable)" if gave_up else "",
    )
    return found


def _fetch_store_name(session, appid):
    """Return the Simplified Chinese store name of one game.

    Args:
        session: the requests session, reused to keep the connection warm
        appid (str): the Steam App ID of the game

    Returns:
        tuple: whether the store answered at all, and the name it gave or None
        when the game has no entry or no localized title
    """
    try:
        response = session.get(
            STORE_APPDETAILS_URL,
            params={
                "appids": appid,
                "l": STORE_LANGUAGE,
                "cc": STORE_COUNTRY,
                "filters": "basic",
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        # Best effort: one unreachable game only costs that game's name
        logger.debug("Could not read the store entry of %s", appid, exc_info=True)
        return False, None

    entry = payload.get(str(appid)) if isinstance(payload, dict) else None
    if not isinstance(entry, dict) or not entry.get("success"):
        return True, None

    data = entry.get("data")
    if not isinstance(data, dict):
        return True, None

    name = data.get("name")
    if not isinstance(name, str):
        return True, None
    return True, name.strip() or None


def _match_rows(key):
    """Return the ``(id, title_zh)`` rows a manual table key points at.

    Args:
        key (str): a Steam App ID or an exact English title

    Returns:
        list: tuples of the item id and its current ``title_zh``
    """
    key = str(key).strip()
    if not key:
        return []

    if _NUMERIC_KEY.match(key):
        rows = Item.objects.filter(
            source="steam",
            media_type="game",
            media_id=key,
        )
    else:
        rows = Item.objects.filter(
            source="steam",
            media_type="game",
            title=key,
        )

    return list(rows.values_list("id", "title_zh"))


def _write_skeleton():
    """Create the empty manual table so the next hand edit has a place to go."""
    skeleton = {
        "note": (
            "手工维护的中文名对照表。key 是 Steam appid（字符串），value 是中文名。"  # noqa: RUF001
            "纯数字 key 按 appid 匹配；其他 key 按英文原名（title）精确匹配。"  # noqa: RUF001
            "改完直接保存，下次跑 .tools\\fill_cn_names.py 或每日同步就生效。"  # noqa: RUF001
        ),
        "version": 1,
        "names": {},
    }
    try:
        MANUAL_NAMES_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANUAL_NAMES_PATH.write_text(
            json.dumps(skeleton, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info("Created an empty Chinese name table at %s", MANUAL_NAMES_PATH)
    except OSError:
        logger.warning(
            "Could not create the Chinese name table at %s",
            MANUAL_NAMES_PATH,
            exc_info=True,
        )
