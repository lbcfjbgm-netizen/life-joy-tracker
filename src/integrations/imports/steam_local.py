"""Read the Steam client's own records from this machine's disk.

The Web API only knows two sets of games: the ones bought with the account
(``GetOwnedGames``) and the ones shared by a family member
(``GetSharedLibraryApps``). A third set exists, and it is invisible to both:
games launched on this machine that belong to neither list. They only show up
in the files the client keeps for itself.

Two files matter:

* ``userdata/<account id>/config/localconfig.vdf`` holds one block per app the
  client ever started, with the total playtime in minutes and the timestamp of
  the last launch.
* ``steamapps/appmanifest_<appid>.acf`` is the install manifest of a game
  installed in that library, and the only place where its original name can be
  read without asking the Store.

Both are Valve's KeyValues ("VDF") format: a nested tree of quoted strings and
braces. No dependency is added for it, as the format is read once per import;
a small scanner below parses it exactly, escapes included.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# The key the client writes the games hidden on the profile into. It carries
# the account id as a suffix, so the key is matched by prefix and never
# hardcoded: the id differs on every machine.
PRIVATE_APPS_PREFIX = "PrivateApps_"

# The default install locations, most common first. A library that lives on
# another drive is found through ``libraryfolders.vdf`` instead, see
# ``steamapps_dirs``.
STEAM_INSTALL_DIRS = (
    Path(r"C:\Program Files (x86)\Steam"),
    Path(r"C:\Program Files\Steam"),
)

# The two keys of an ``apps`` block that carry the play history.
PLAYTIME_KEY = "Playtime"
LAST_PLAYED_KEY = "LastPlayed"

# Valve escapes only a handful of characters, everything else is written as is.
VDF_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}

MANIFEST_GLOB = "appmanifest_*.acf"


def _scan(text):
    """Return the tokens of a KeyValues document.

    A token is either one brace, or a string. A quoted string may escape a
    quote or a backslash; the platform tags some Valve files carry
    (``[$WIN32]``) are skipped as a whole, as are ``//`` comments.
    """
    tokens = []
    index = 0
    length = len(text)

    while index < length:
        char = text[index]

        if char in " \t\r\n\0":
            index += 1
        elif char == "/" and text.startswith("//", index):
            newline = text.find("\n", index)
            index = length if newline < 0 else newline + 1
        elif char in "{}":
            tokens.append(char)
            index += 1
        elif char == "[":
            # Platform condition, not a value
            closing = text.find("]", index)
            index = length if closing < 0 else closing + 1
        elif char == '"':
            index += 1
            chunk = []
            while index < length:
                char = text[index]
                if char == "\\" and index + 1 < length:
                    following = text[index + 1]
                    chunk.append(VDF_ESCAPES.get(following, following))
                    index += 2
                    continue
                if char == '"':
                    index += 1
                    break
                chunk.append(char)
                index += 1
            tokens.append("".join(chunk))
        else:
            # A bare token, which a few Valve files use for plain numbers
            start = index
            while index < length and text[index] not in ' \t\r\n"{}':
                index += 1
            tokens.append(text[start:index])

    return tokens


def _parse(tokens):
    """Return the nested dictionary of a tokenized KeyValues document."""
    root = {}
    stack = [root]
    index = 0
    count = len(tokens)

    while index < count:
        token = tokens[index]

        if token == "}":
            if len(stack) > 1:
                stack.pop()
            index += 1
            continue

        if token == "{":
            # A block opened without a key of its own, nothing to attach it to
            index += 1
            continue

        key = token
        index += 1

        if index < count and tokens[index] == "{":
            node = {}
            stack[-1][key] = node
            stack.append(node)
            index += 1
            continue

        if index < count and tokens[index] != "}":
            stack[-1][key] = tokens[index]
            index += 1

    return root


def _read_vdf(path):
    """Return the parsed tree of a KeyValues file, empty when it can't be read.

    The files are written as UTF-8, but a replaced character must never cost
    the whole import, hence the lenient decoding.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return _parse(_scan(text))


def _as_int(value):
    """Return the integer a VDF value holds, None when it holds something else."""
    if isinstance(value, dict):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_appid(value):
    """Return a usable App ID, None for anything that isn't a positive number."""
    appid = _as_int(value)
    if appid is None or appid <= 0:
        return None
    return appid


def _iter_dicts(node):
    """Yield every dictionary of a parsed document, depth first."""
    if not isinstance(node, dict):
        return
    yield node
    for value in node.values():
        yield from _iter_dicts(value)


def steam_userdata_root() -> Path | None:
    """Return the ``userdata`` folder of the Steam install, None when absent.

    The Steam account is never hardcoded: the folder below this one is named
    after an account id that differs on every machine, so callers walk into it
    themselves (see ``localconfig_path``).
    """
    for install in STEAM_INSTALL_DIRS:
        userdata = install / "userdata"
        if userdata.is_dir():
            return userdata
    return None


def localconfig_path() -> Path | None:
    """Return the ``localconfig.vdf`` that holds the play history of this user.

    Several accounts can have logged in on one machine, and each keeps a file
    of its own. Picking the first one alphabetically is wrong: on the machine
    this was written for, that folder belonged to a friend who played a dozen
    games, while the operator's own folder held thousands.

    The account is therefore chosen by evidence, the account folder of the
    running Steam user first (the login that last wrote ``ActiveUser`` in the
    registry), then the file with the most played games. Neither needs the
    Steam ID, which differs on every machine.
    """
    userdata = steam_userdata_root()
    if userdata is None:
        return None

    candidates = []
    try:
        accounts = sorted(userdata.iterdir(), key=lambda entry: entry.name)
    except OSError:
        logger.warning("Could not list %s", userdata, exc_info=True)
        return None

    for account in accounts:
        # Only account folders are numbered; cache and shared folders are not
        if not account.is_dir() or not account.name.isdigit():
            continue
        candidate = account / "config" / "localconfig.vdf"
        if candidate.is_file():
            candidates.append(candidate)

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    active = _active_user_account()
    if active:
        for candidate in candidates:
            if candidate.parent.parent.name == active:
                logger.info("Using the Steam account logged in on this machine")
                return candidate

    # Nothing to go by but the content: the account that actually played games
    # is the one whose file holds the most entries with a playtime
    best = candidates[0]
    best_count = -1
    for candidate in candidates:
        try:
            count = len(_playtimes_of(candidate))
        except OSError:
            logger.warning("Could not read %s", candidate, exc_info=True)
            continue
        if count > best_count:
            best, best_count = candidate, count

    logger.info(
        "Using %s, the Steam account with the most played games (%d)",
        best,
        best_count,
    )
    return best


def _active_user_account():
    """Return the account id Steam wrote as active last, None when unknown.

    ``HKCU\\Software\\Valve\\Steam\\ActiveUser`` is a 32 bit Steam ID, and the
    folder under ``userdata`` is the low part of it (the account id). Reading
    it is a nicety only: every caller falls back to the playtime counts when
    the registry can't be read, so Linux and a locked down registry are fine.
    """
    if os.name != "nt":
        return None

    try:
        import winreg  # noqa: PLC0415 - Windows only
    except ImportError:
        return None

    try:
        with winreg.OpenKey(  # noqa: SIM117 - one key, one query
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


def _playtimes_of(path):
    """Return the played games of one ``localconfig.vdf``."""
    document = _read_vdf(path)
    section = _playtime_section(document)
    playtimes = {}
    for key, entry in section.items():
        if not isinstance(entry, dict):
            continue
        appid = _as_appid(key)
        if appid is None:
            continue
        minutes = _as_int(entry.get(PLAYTIME_KEY))
        if minutes is None or minutes <= 0:
            continue
        last_played = _as_int(entry.get(LAST_PLAYED_KEY))
        playtimes[appid] = {
            "playtime_minutes": minutes,
            "last_played": last_played if last_played is not None else 0,
        }
    return playtimes


def _playtime_section(document):
    """Return the ``apps`` node that holds the play history.

    The expected place is ``UserLocalConfigStore/Software/Valve/Steam/apps``.
    The document holds a second ``apps`` node with the same shape but no
    playtime at all (the per-game launch options), so a node is only accepted
    when at least one of its entries carries a playtime. A walk over the whole
    document is the fallback for clients that nest the node elsewhere.
    """
    store = document.get("UserLocalConfigStore")
    if isinstance(store, dict):
        software = store.get("Software")
        if isinstance(software, dict):
            valve = software.get("Valve")
            if isinstance(valve, dict):
                steam = valve.get("Steam")
                if isinstance(steam, dict):
                    apps = steam.get("apps")
                    if isinstance(apps, dict) and _holds_playtime(apps):
                        return apps

    for node in _iter_dicts(document):
        apps = node.get("apps")
        if isinstance(apps, dict) and _holds_playtime(apps):
            return apps

    return {}


def _holds_playtime(apps):
    """Return whether any entry of an ``apps`` node records a playtime."""
    return any(
        isinstance(entry, dict) and PLAYTIME_KEY in entry
        for entry in apps.values()
    )


def read_local_playtime() -> dict[int, dict]:
    """Return the play history the Steam client keeps, keyed by App ID.

    Only the apps with a playtime above zero are returned: the client also
    lists every app it merely knows about, and those were never played.

    Returns:
        dict: ``{appid: {"playtime_minutes": int, "last_played": int}}``
    """
    path = localconfig_path()
    if path is None:
        logger.info("No Steam localconfig.vdf found, no local playtime to read")
        return {}

    try:
        playtimes = _playtimes_of(path)
    except OSError:
        logger.warning("Could not read %s", path, exc_info=True)
        return {}

    if not playtimes:
        logger.warning("No play history found in %s", path)
        return {}

    logger.info(
        "Read the playtime of %d games from %s",
        len(playtimes),
        path,
    )
    return playtimes


def _read_library_paths(vdf_path):
    """Return the library folders listed in a ``libraryfolders.vdf``.

    The current format nests one block per library under a numbered key, each
    with a ``path``. Older clients wrote the path straight as the value of
    that key, which is still read here.
    """
    try:
        document = _read_vdf(vdf_path)
    except OSError:
        return []

    node = None
    for key in ("libraryfolders", "LibraryFolders"):
        candidate = document.get(key)
        if isinstance(candidate, dict):
            node = candidate
            break
    if node is None:
        return []

    paths = []
    for value in node.values():
        if isinstance(value, dict):
            library = value.get("path")
        else:
            library = value
        if isinstance(library, str) and library.strip():
            paths.append(Path(library.strip()))
    return paths


def steamapps_dirs() -> list[Path]:
    """Return every ``steamapps`` folder of this machine that actually exists.

    The known install locations are the seeds; every ``libraryfolders.vdf``
    found in them adds the libraries of the other drives, and those libraries
    may hold a file of their own. The walk therefore keeps going until no new
    folder shows up, then duplicates and missing folders are dropped.
    """
    queue = [install / "steamapps" for install in STEAM_INSTALL_DIRS]
    found = []
    seen = set()

    while queue:
        folder = queue.pop(0)
        if folder in seen:
            continue
        seen.add(folder)
        if not folder.is_dir():
            continue

        found.append(folder)
        for library in _read_library_paths(folder / "libraryfolders.vdf"):
            queue.append(library / "steamapps")

    return found


def read_installed_apps() -> dict[int, str]:
    """Return the original name of every game installed, keyed by App ID.

    A game without a manifest has no locally readable name: the Store is the
    only other source, and it is not always reachable, hence the empty result
    instead of an error.
    """
    apps = {}

    for folder in steamapps_dirs():
        try:
            manifests = sorted(folder.glob(MANIFEST_GLOB))
        except OSError:
            logger.warning("Could not list %s", folder, exc_info=True)
            continue

        for manifest in manifests:
            try:
                document = _read_vdf(manifest)
            except OSError:
                logger.warning("Could not read %s", manifest, exc_info=True)
                continue

            state = document.get("AppState")
            if not isinstance(state, dict):
                continue

            appid = _as_appid(state.get("appid"))
            if appid is None:
                continue

            name = state.get("name")
            if isinstance(name, str) and name.strip():
                apps[appid] = name.strip()

    logger.info("Read the name of %d installed Steam games", len(apps))
    return apps


def local_only_games(known_appids, exclude_appids=None) -> list[dict]:
    """Return the games played here that the Steam API never reports.

    Args:
        known_appids (iterable): App IDs the caller already holds, as strings
            or integers; every one of them is left out
        exclude_appids (iterable, optional): App IDs to leave out as well, as
            strings or integers; the private and hidden games of the account
            come from here

    Returns:
        list: dictionaries with ``appid``, ``name`` (may be empty),
        ``playtime_minutes`` and ``last_played``, played the longest first
    """
    known = set()
    for appid in known_appids:
        value = _as_appid(appid)
        if value is not None:
            known.add(value)

    excluded = set()
    for appid in exclude_appids or ():
        value = _as_appid(appid)
        if value is not None:
            excluded.add(value)

    playtimes = read_local_playtime()
    if not playtimes:
        return []

    names = read_installed_apps()

    games = [
        {
            "appid": appid,
            "name": names.get(appid, ""),
            "playtime_minutes": entry["playtime_minutes"],
            "last_played": entry["last_played"],
        }
        for appid, entry in playtimes.items()
        if appid not in known and appid not in excluded
    ]

    # Longest played first, so a capped run still backfills the games that
    # matter most; the App ID only keeps the order stable on a tie.
    games.sort(key=lambda game: (-game["playtime_minutes"], game["appid"]))

    logger.info(
        "Found %d games played locally that Steam does not report "
        "(%d known App IDs, %d private, %d with a playtime, %d with a name)",
        len(games),
        len(known),
        len(excluded),
        len(playtimes),
        sum(1 for game in games if game["name"]),
    )
    return games


def local_playtime_map(exclude_appids=None) -> dict[str, dict]:
    """Return the playtime the Steam client records, keyed by App ID string.

    ``read_local_playtime`` returns the same data keyed by integer, which suits
    a lookup by App ID but not a comparison against the library: every media ID
    of Yamtrack is a string. The keys are converted here, once, so the caller
    can match its own records without rebuilding the mapping itself.

    Args:
        exclude_appids (iterable, optional): App IDs to leave out, as strings or
            integers; the private, excluded and blocked games of the account
            come from here

    Returns:
        dict: ``{appid: {"playtime_minutes": int, "last_played": int}}``, empty
        when the client file can't be read
    """
    skipped = set()
    for appid in exclude_appids or ():
        value = _as_appid(appid)
        if value is not None:
            skipped.add(value)

    playtimes = read_local_playtime()
    if not playtimes:
        return {}

    mapping = {
        str(appid): {
            "playtime_minutes": entry["playtime_minutes"],
            "last_played": entry["last_played"],
        }
        for appid, entry in playtimes.items()
        if appid not in skipped
    }

    logger.info(
        "Mapped the local playtime of %d games (%d left out by the caller)",
        len(mapping),
        len(playtimes) - len(mapping),
    )
    return mapping


def read_private_appids() -> set[int]:
    r"""返回 Steam 里被标记为私密或隐藏的 App ID 集合.

    ``localconfig.vdf`` 里 ``PrivateApps_<账号 id>`` 这个键, 记的就是用户
    在库里勾成「私密」或「隐藏」的游戏. 这些游戏在 Steam 页面上别人看不到,
    但 Web API 照样把它们当成正常游戏返回, 所以只能从本机这份文件里读.

    键名里带着账号 id, 因此只按 ``PrivateApps_`` 前缀找, 绝不写死账号 id:
    每台机器, 每个账号的后缀都不一样.

    值是一个被 VDF 转义过的 JSON 数组字符串 (``\"`` 代表一个引号),
    这里既反转义又直接用正则抽数字, 两种写法都能吃下.

    Returns:
        set: 私密或隐藏游戏的 App ID, 读不到时是空集合
    """
    try:
        path = localconfig_path()
        if path is None:
            logger.info("No Steam localconfig.vdf found, no private games to read")
            return set()

        document = _read_vdf(path)

        appids, keys = set(), []
        for node in _iter_dicts(document):
            for key, value in node.items():
                if isinstance(key, str) and key.startswith(PRIVATE_APPS_PREFIX):
                    # 同一个键可能被写到不止一层, 取并集才不会漏
                    keys.append(key)
                    appids.update(_parse_private_appids(value))

        if not appids:
            logger.info("No private Steam games found in %s", path)
            return set()

        logger.info(
            "Read %d private or hidden games from %s (%s)",
            len(appids),
            path,
            ", ".join(keys),
        )
    except Exception:
        # 读不到私密清单绝不能拖垮整次导入, 最坏的结果只是这层过滤失效
        logger.warning(
            "Could not read the private Steam games, importing without that filter",
            exc_info=True,
        )
        return set()
    else:
        return appids


def _parse_private_appids(value) -> set[int]:
    """Return the App IDs held by the value of a ``PrivateApps_`` key.

    The value is a JSON array in a string, with every quote escaped the way
    KeyValues escapes them. Taking the digits out of it is enough: the array
    holds nothing but numbers and commas, so it survives a malformed tail that
    ``json.loads`` would reject.
    """
    if not isinstance(value, str):
        return set()

    unescaped = value.replace(r"\"", '"')
    return {int(digits) for digits in re.findall(r"\d+", unescaped)}
