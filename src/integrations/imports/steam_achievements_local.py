"""Read the achievement counters the Steam client keeps on this machine.

``ISteamUserStats/GetPlayerAchievements`` answers ``HTTP 403`` for a game whose
profile is not public, and the library then shows "未同步" forever even though
the progress exists. The client itself does not depend on that endpoint: it
caches what it last showed in the library page, under
``userdata/<account id>/config/librarycache/<appid>.json``.

The file is a JSON array of ``[key, {"version": ..., "data": ...}]`` pairs, and
the pair named ``achievements`` holds the two integers this module is after:
``nTotal`` (how many achievements the game has) and ``nAchieved`` (how many of
them the account unlocked). Both were checked against the client UI on the
machine this was written for and matched exactly (``3812600`` 25/50,
``2967950`` 18/60).

Two limits keep the data honest:

* the cache is only useful for games that have no counters in the database
  yet. Its ``nTotal`` is the count the client last saw, which can be smaller
  than Steam's real one (measured: ``2218750`` 500 here against 666 official,
  ``1829470`` 12 against 14), so it must never overwrite a fetched total.
* a file is missing for every game that was never opened on this machine, and
  a game without achievements still gets ``nTotal = 0``. Both are simply "no
  data", never an error.

Everything here is read only: no file is written and nothing is imported into
the database.
"""

import json
import logging
import os
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

# The key of the pair that carries the counters. Every other pair of the file
# (``achievementmap``, ``usernews``, ``badge``, ...) is of no use here.
ACHIEVEMENTS_KEY = "achievements"

# The field names inside that pair.
TOTAL_FIELD = "nTotal"
ACHIEVED_FIELD = "nAchieved"

# A pair is ``[key, value]``, so an entry shorter than this is malformed.
PAIR_LENGTH = 2

# The two conventional install locations, most common first. A library on
# another drive is reached through ``libraryfolders.vdf`` instead, see
# ``_steam_roots``: the cache always lives under a Steam install, but that
# install itself can sit anywhere.
STEAM_INSTALL_DIRS = (
    Path(r"C:\Program Files (x86)\Steam"),
    Path(r"C:\Program Files\Steam"),
)

# Parsed counters, keyed by appid, so that one import asking for the same game
# twice reads the disk once. Only real counters are remembered: a game that has
# no file today gets one the first time the client launches it, and a worker
# process lives for days.
_CACHE = {}

# A library holds thousands of games and the cache lives for the whole process,
# so it is bounded; the order of a dict makes the oldest entry the one to drop.
_CACHE_MAX = 8192

# The one library directory that was resolved, cached for the life of the
# process. Only a directory that really holds files is remembered, so a Steam
# client installed later is still picked up on the next call.
_ROOT = None

# Longer than any real App ID and short enough to bound the path built below;
# being digits only also keeps ``..`` and absolute paths out of it.
_MAX_APPID_DIGITS = 12


def _steam_roots():
    """Return every Steam install root that exists, the live install first.

    The order matters, see ``librarycache_dir``: the client refreshes the cache
    of the install it is actually running from, so an explicit directory, then
    the install the registry names, then the two conventional folders, and only
    then the libraries listed in ``libraryfolders.vdf``.

    ``libraryfolders.vdf`` is the only record of the Steam roots on other
    drives. Its parser already lives in ``steam_local`` for the playtime
    import, so it is reused rather than written a second time; that module is
    imported here because a machine where it can't be read simply has fewer
    roots to try.
    """
    roots = []
    for override in (
        os.environ.get("STEAM_LOCAL_DIR", ""),
        getattr(settings, "STEAM_LOCAL_DIR", "") or "",
    ):
        if override:
            # An explicit answer wins over whatever is detected below
            _append_root(roots, Path(override))

    _append_root(roots, _registered_install())

    for install in STEAM_INSTALL_DIRS:
        _append_root(roots, install)

    for library in _library_roots():
        _append_root(roots, library)

    return roots


def _append_root(roots, path):
    """Append a Steam root that exists and is not listed yet, ignore the rest."""
    if path is None or not path.is_dir():
        return

    # The same folder is spelled with a different case by the registry and by
    # the conventional paths below, so the comparison is case insensitive
    known = {os.path.normcase(str(root)) for root in roots}
    if os.path.normcase(str(path)) not in known:
        roots.append(path)


def _registered_install():
    r"""Return the Steam folder the client is installed in, None when unknown.

    ``SteamPath`` under ``HKCU\Software\Valve\Steam`` is what a running client
    writes, and it is the address of the install whose cache is being updated.
    The two machine wide keys are only a fallback for a freshly installed
    client that was never started for this Windows account.
    """
    if os.name != "nt":
        return None

    try:
        import winreg  # noqa: PLC0415 - Windows only
    except ImportError:
        return None

    for hive, key_name, value_name in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Valve\Steam",
            "InstallPath",
        ),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(hive, key_name) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue
        if value:
            # The client stores the path with forward slashes
            return Path(str(value).replace("/", "\\"))

    return None


def _library_roots():
    """Return the Steam roots listed by the ``libraryfolders.vdf`` of this machine."""
    try:
        from integrations.imports import steam_local  # noqa: PLC0415 - optional
    except ImportError:
        return []

    try:
        return [folder.parent for folder in steam_local.steamapps_dirs()]
    except Exception:
        # Reading the libraries is a bonus: a broken VDF only means the
        # conventional locations are tried
        logger.warning("Could not read the Steam library folders", exc_info=True)
        return []


def librarycache_dir():
    r"""Return the achievement cache of the Steam client, None when absent.

    The folder is named after the account id of whoever last logged in on this
    machine, which differs on every machine and is therefore never hardcoded.
    Two signals pick it out of the folders that exist: the account Steam logged
    in with last (``ActiveUser`` in the registry, on the install the registry
    names), and otherwise the folder that was written most recently.

    Recency rather than the number of files, because a machine can keep the
    userdata folder of an old install around. That copy still holds thousands
    of games this install never opened, but its counters were frozen the day
    the old client last ran: on the machine this was written for, ``D:\\steam``
    held 3123 files frozen in August 2025 while the live install held 1630
    written the same day, and the stale one reported the wrong totals for two
    of the games that were checked by hand (``3812600``, ``2967950``).

    Returns:
        Path | None: ``userdata/<account id>/config/librarycache``, or None
        when Steam is not installed here or no account ever cached a game
    """
    global _ROOT  # noqa: PLW0603
    if _ROOT is not None:
        return _ROOT

    active = _active_account()
    best = None
    best_key = None

    for root in _steam_roots():
        try:
            accounts = sorted(
                entry for entry in (root / "userdata").iterdir() if entry.is_dir()
            )
        except OSError:
            # No Steam here, or the folder can't be read: try the next root
            continue

        for account in accounts:
            cache = account / "config" / "librarycache"
            if not cache.is_dir():
                continue

            count, newest = _cache_stats(cache)
            if count <= 0:
                continue

            if active is not None and account.name == active:
                _ROOT = cache
                logger.info("Using %s, the Steam account logged in here", cache)
                return _ROOT

            key = (newest, count)
            if best_key is None or key > best_key:
                best, best_key = cache, key

    if best is not None:
        _ROOT = best
        logger.info(
            "Using %s, the Steam account whose cache was written last "
            "(%d cached games)",
            best,
            best_key[1],
        )
    return _ROOT


def _cache_stats(folder):
    """Return the file count and the newest mtime of a cache folder.

    Returns:
        tuple: ``(count, newest)``, ``(0, 0.0)`` when the folder is unreadable
    """
    count = 0
    newest = 0.0
    try:
        for entry in folder.iterdir():
            if entry.suffix != ".json":
                continue
            count += 1
            try:
                newest = max(newest, entry.stat().st_mtime)
            except OSError:
                # A file that vanished between iterdir and stat is not a
                # reason to give up on the folder
                continue
    except OSError:
        return 0, 0.0
    return count, newest


def _active_account():
    r"""Return the account id Steam last logged in with, None when unknown.

    ``userdata`` is keyed by the low 32 bits of the Steam ID, and that is what
    ``HKCU\Software\Valve\Steam\ActiveUser`` holds. Reading the registry is a
    nicety only: callers fall back on the number of cached games without it.
    """
    if os.name != "nt":
        return None

    try:
        import winreg  # noqa: PLC0415 - Windows only
    except ImportError:
        return None

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Valve\Steam",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "ActiveUser")
    except OSError:
        return None

    try:
        return str(int(value) & 0xFFFFFFFF)
    except (TypeError, ValueError):
        return None


def _as_appid(appid):
    """Return an App ID usable as a file name, None for anything else."""
    text = str(appid if appid is not None else "").strip()
    if not text.isdigit() or len(text) > _MAX_APPID_DIGITS:
        return None
    return text


def _as_count(value):
    """Return a non negative integer, None when the value is not one."""
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _counts_from_document(document):
    """Return ``(total, unlocked)`` of a parsed cache file, None when unusable."""
    if not isinstance(document, list):
        return None

    for entry in document:
        # Every entry is a ``[key, {...}]`` pair, but the file is Valve's own
        # output: a malformed one must never raise out of an import
        if not isinstance(entry, (list, tuple)) or len(entry) < PAIR_LENGTH:
            continue
        if entry[0] != ACHIEVEMENTS_KEY:
            continue

        payload = entry[1]
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None

        total = _as_count(data.get(TOTAL_FIELD))
        unlocked = _as_count(data.get(ACHIEVED_FIELD))
        if total is None or unlocked is None or total <= 0:
            return None

        # A file written mid update could report more unlocked than the game
        # has in total, which would render as a percentage above 100
        return total, min(unlocked, total)

    return None


def read_achievements(appid):
    """Return the achievement counters cached for a game, None when absent.

    Args:
        appid (str | int): the Steam App ID of the game

    Returns:
        tuple | None: ``(total, unlocked)``, or None when the game was never
        opened here, the file cannot be parsed, or it holds no achievement
    """
    key = _as_appid(appid)
    if key is None:
        return None

    if key in _CACHE:
        return _CACHE[key]

    result = _read_uncached(key)
    if result is None:
        return None

    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)), None)
    _CACHE[key] = result
    return result


def _read_uncached(appid):
    """Return the counters of one App ID, read from the disk this time."""
    folder = librarycache_dir()
    if folder is None:
        return None

    path = folder / f"{appid}.json"
    if not path.is_file():
        return None

    try:
        # The files are UTF-8, but a replaced character must never cost the
        # whole sync, hence the lenient decoding
        document = json.loads(path.read_bytes().decode("utf-8", "replace"))
    except (OSError, ValueError):
        # A file being rewritten by the client is a normal race, not an error
        logger.debug("Could not read the achievement cache of appid %s", appid)
        return None

    return _counts_from_document(document)


def counts_for_appids(appids):
    """Return the cached counters of several games at once.

    Args:
        appids (iterable): App IDs, as strings or integers

    Returns:
        dict: ``{appid: (total, unlocked)}``, only the games that have a usable
        cache file, keyed by the App ID as a string
    """
    counts = {}
    for appid in appids:
        key = _as_appid(appid)
        if key is None:
            continue
        result = read_achievements(key)
        if result is not None:
            counts[key] = result
    return counts


def clear_cache():
    """Forget every parsed cache file, so the next call reads the disk again."""
    global _ROOT  # noqa: PLW0603
    _CACHE.clear()
    _ROOT = None
