"""Read the Steam client's local screenshot cache and serve it to the site.

The Steam client keeps every screenshot the user takes on this machine, under
``<steam>/userdata/<account id>/760/remote/<appid>/screenshots/``: the original
``<YYYYMMDDHHMMSS>_<n>.jpg`` next to a much smaller ``thumbnails/`` copy of the
same name. Nothing about that list is exposed through the Web API, and the
files never move once written, so the cheapest way to show them on the item
page is to read the directory directly instead of importing them into the
database.

Two consequences shape this module:

* A new capture must show up on the next page load, so the directory scan is
  cached only until one of the two directories changes (see
  :data:`_LISTING_CACHE`). A cache that outlived the directory would defeat the
  whole point of reading the client cache.
* The two directories are filled independently: Steam writes a thumbnail for
  each capture, but a user can delete either side, and this machine really does
  have 374 thumbnails against 292 originals. A capture is only listed when its
  original exists; a missing thumbnail just falls back to the original image.
* The folder is not exclusively Steam's. A user who drags their own image in --
  ``下载 (1).jpg``, a UUID-named PNG -- still expects to see it, so a name is
  only rejected when it could escape the directory, never merely because it
  does not look like the ones Steam writes.

A second, drop-in gallery exists for games that did not come from Steam (a
hand-entered Switch title, say): those have no appid at all, so their images
live under the project's own ``.tools/notion_shots/<key>/`` and are looked up by
the item's ``media_id`` instead. See :data:`_LOCAL_GALLERY_ROOT`.

Everything here is read only: no file is written and nothing is imported into
the database.
"""

import logging
import re
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.urls import reverse

from app.models import MediaTypes, Sources

logger = logging.getLogger(__name__)

# Steam keeps each account's client data under
# ``<steam>/userdata/<account id>/760/remote``, and the account id belongs to
# whoever is logged in -- it is not something this module can know. Instead of
# hard-coding one, the usual install locations are scanned for the first
# account that actually has that folder (see :func:`detect_root`).
_STEAM_DIRS = (
    r"C:\Program Files (x86)\Steam",
    r"C:\Program Files\Steam",
)

# The one detected root, cached for the life of the process. Only a root that
# was really found is remembered: a machine where Steam shows up later (or a
# fresh account) is still picked up on the next call.
_DETECTED_ROOT = None

# Suffixes the Steam client writes. Anything else in the directory (a stray
# ``desktop.ini``, a user's own ``.mp4``) is not a screenshot and is skipped.
_SCREENSHOT_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})

# 本地图库给非 Steam 来源游戏用,额外接受 .webp。这些图常常是从网页或
# 笔记工具里另存下来的,格式不受 Steam 客户端约束。
_LOCAL_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})

_CONTENT_TYPE_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

# Everything the two galleries together may serve. The Steam scan still filters
# on its own narrower set, so a stray ``.webp`` dropped into a Steam folder
# keeps behaving exactly as it did before.
_ALL_SUFFIXES = _SCREENSHOT_SUFFIXES | _LOCAL_SUFFIXES

# 本地图库根目录就是项目根下的 .tools/notion_shots。手工录入或从笔记导入的
# 游戏截图按条目 media_id 分目录放在这里,形如 <根>/<key>/<图片文件>,
# 详情页读它来给非 Steam 游戏显示截图。路径从本文件反推,不写死盘符,
# 换机器或换用户名都不受影响。
_LOCAL_GALLERY_ROOT = (
    Path(__file__).resolve().parent.parent.parent / ".tools" / "notion_shots"
)

# 本地图库 key 的最大长度。够放下 uuid4 的 36 位,也短到能限制拼出来的路径。
_MAX_KEY_LENGTH = 64


# ``<YYYYMMDDHHMMSS>_<n>.<ext>``: the name is the moment of capture followed by
# the index within that second, so it doubles as the sort key.
_FILENAME_PATTERN = re.compile(r"^(\d{14})_\d+\.(?:jpg|jpeg|png)$", re.IGNORECASE)

# An older Steam build prefixed the appid, giving
# ``<appid>_screenshots_<YYYYMMDDHHMMSS>_<n>.<ext>``. Same moment, one more
# group to skip past.
_LEGACY_FILENAME_PATTERN = re.compile(
    r"^\d+_screenshots_(\d{14})_\d+\.(?:jpg|jpeg|png)$",
    re.IGNORECASE,
)

# A Steam appid is a plain integer; twelve digits is well past the largest one
# and short enough to bound the path this module will ever build.
_APPID_PATTERN = re.compile(r"^\d{1,12}$")

# Characters no single path component may contain. ``/`` and ``\`` are the
# separators, ``:`` addresses a drive or an NTFS stream, and the rest are
# reserved by Windows. Control characters are checked separately below.
_FORBIDDEN_FILENAME_CHARS = frozenset('/\\:*?"<>|')

# Windows refuses names longer than 255 characters; this module is stricter
# because a screenshot name is a file the Steam client wrote, not a document.
_MAX_FILENAME_LENGTH = 160

# The only two things the URL may ask for.
KIND_FULL = "full"
KIND_THUMB = "thumb"
KINDS = frozenset({KIND_FULL, KIND_THUMB})

# Scanned listings, keyed by appid. Each entry stores the two directory
# timestamps the scan was taken at: creating a capture touches
# ``screenshots/``, creating only a thumbnail touches ``thumbnails/``, and
# either one invalidates the entry. That keeps the common case (a visitor
# reloading the same game page) free of directory reads while still letting a
# brand new screenshot appear on the very next request.
_LISTING_CACHE = {}
_LISTING_CACHE_MAX = 256


def detect_root():
    """Return the screenshot root found on this machine, or ``""``.

    Steam names the folder after the account id of the person logged in
    (``userdata/<account id>/760/remote``), so the only portable way to find it
    is to look for it. The two conventional install locations are tried, and
    the first directory that exists wins.

    Nothing here is required: a machine without the Steam client simply gets
    ``""``, which the item page reads as "no screenshots to show".
    """
    global _DETECTED_ROOT  # noqa: PLW0603
    if _DETECTED_ROOT is not None:
        return _DETECTED_ROOT

    for steam_dir in _STEAM_DIRS:
        userdata = Path(steam_dir) / "userdata"
        try:
            accounts = sorted(entry for entry in userdata.iterdir() if entry.is_dir())
        except OSError:
            # No Steam here, or the folder is unreadable: try the next one.
            continue
        for account in accounts:
            remote = account / "760" / "remote"
            try:
                if remote.is_dir():
                    _DETECTED_ROOT = str(remote)
                    return _DETECTED_ROOT
            except OSError:
                continue
    return ""


def screenshot_root():
    """Return the Steam screenshot root as a :class:`Path`.

    The configured setting wins; without one the directory is auto detected.
    The directory is not required to exist: a machine without the Steam client
    simply has no screenshots, and every function here degrades to an empty
    answer.
    """
    root = getattr(settings, "STEAM_SCREENSHOT_ROOT", None) or detect_root()
    return Path(root) if root else Path()


def content_type_for(filename):
    """Return the content type to answer a screenshot with."""
    suffix = Path(str(filename or "")).suffix.lower()
    return _CONTENT_TYPE_BY_SUFFIX.get(suffix, "application/octet-stream")


def clear_cache():
    """Forget every scanned listing, so the next call reads the disk again."""
    _LISTING_CACHE.clear()


def valid_appid(appid):
    """Return True if ``appid`` is a plausible Steam appid (digits only)."""
    return bool(_APPID_PATTERN.match(str(appid or "").strip()))


def valid_gallery_key(key):
    """Return True if ``key`` may name one directory under a screenshot root.

    The key ends up as a single path component in both the URL and the path
    this module builds, and it is no longer always a Steam appid: a hand added
    game has a UUID ``media_id`` instead. So instead of "digits only" the rule
    is "one safe component": 1 to :data:`_MAX_KEY_LENGTH` characters, each of
    them ``[A-Za-z0-9_-]``. That already excludes ``.``/``..``, both path
    separators, ``:`` and NUL, and is loose enough for every real key this site
    produces.
    """
    key = str(key or "").strip()
    if not 1 <= len(key) <= _MAX_KEY_LENGTH:
        return False
    return all(
        char.isascii() and (char.isalnum() or char in {"_", "-"}) for char in key
    )


def _screenshots_dir(appid):
    """Return the ``screenshots`` directory for an appid, or None if invalid."""
    appid = str(appid or "").strip()
    if not valid_appid(appid):
        return None
    return screenshot_root() / appid / "screenshots"


def local_gallery_dir(key):
    """Return the local gallery directory for one key, or None if invalid.

    The layout is ``<project>/.tools/notion_shots/<key>/<image file>``: no
    extra ``screenshots`` level and usually no ``thumbnails`` subdirectory,
    because these images are dropped in by hand or by the Notion importer
    rather than written by the Steam client.
    """
    key = str(key or "").strip()
    if not valid_gallery_key(key):
        return None
    return _LOCAL_GALLERY_ROOT / key


def _thumbnails_dir(shots_dir):
    """Return the thumbnails directory that belongs to a screenshots one."""
    return shots_dir / "thumbnails"


def _parse_taken_at(name):
    """Return the capture time encoded in a filename, or None if unreadable."""
    match = _FILENAME_PATTERN.match(name) or _LEGACY_FILENAME_PATTERN.match(name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S")  # noqa: DTZ007
    except ValueError:
        # Fourteen digits that are not a real date (``99999999999999_1.jpg``).
        return None


def is_safe_filename(filename):
    """Return True if ``filename`` may be used as a single path component.

    The Steam client always writes ``<timestamp>_<n>.jpg``, but a user is free
    to drop their own image into the folder -- this machine really has
    ``下载 (1).jpg`` and a UUID-named PNG. Those are still their screenshots, so
    they must render; only names that could escape the directory are refused.

    This is a cheap first pass. The real guarantee is the ``resolve()`` plus
    ``is_relative_to`` check in :func:`resolve_screenshot_path`, which holds
    even for a name that somehow slips through here.
    """
    name = str(filename or "")
    if not name or len(name) > _MAX_FILENAME_LENGTH:
        return False
    if name in {".", ".."}:
        return False
    if Path(name).suffix.lower() not in _ALL_SUFFIXES:
        return False
    if any(char in _FORBIDDEN_FILENAME_CHARS for char in name):
        return False
    # Control characters (including NUL) have no place in a filename and are
    # what a C-string level trick would rely on.
    return all(char >= " " and char != "\x7f" for char in name)


def _mtime_of(entry):
    """Return an entry's modification time as a naive datetime, or None."""
    try:
        return datetime.fromtimestamp(entry.stat().st_mtime)  # noqa: DTZ006
    except OSError:
        return None


def _sort_key(shot):
    """Return the ordering key for one listing entry.

    A capture named the way Steam names it sorts by the moment it was taken.
    Anything else (a user's ``下载 (1).jpg``, or an old prefixed name) sorts by
    when the file landed on disk, so it still lands in a believable place
    instead of clumping at the bottom.
    """
    return (shot["taken_at"] or shot["_mtime"] or datetime.min, shot["filename"])  # noqa: DTZ901


def _screenshot_url(appid, kind, filename):
    """Return the site URL that serves one screenshot."""
    return reverse(
        "steam_screenshot",
        kwargs={"appid": appid, "kind": kind, "filename": filename},
    )


def _thumbnail_names(thumbs_dir):
    """Return the lowercase names of everything inside a thumbnails directory."""
    try:
        if not thumbs_dir.is_dir():
            return frozenset()
        return frozenset(
            entry.name.lower() for entry in thumbs_dir.iterdir() if entry.is_file()
        )
    except OSError as error:
        logger.warning("Steam screenshots could not list %s: %s", thumbs_dir, error)
        return frozenset()


def _scan(appid, shots_dir, thumb_names, suffixes=_SCREENSHOT_SUFFIXES):
    """Build the listing for one gallery. Never raises.

    ``suffixes`` is the set of file types this gallery accepts: the Steam
    client's own three for a Steam folder, the wider local set for the drop-in
    gallery (which also takes ``.webp``). A filename itself is only rejected
    when it could escape the directory -- see :func:`is_safe_filename`.
    """
    shots = []
    try:
        entries = list(shots_dir.iterdir())
    except OSError as error:
        logger.warning("Steam screenshots could not list %s: %s", shots_dir, error)
        return shots

    for entry in entries:
        name = entry.name
        if not is_safe_filename(name):
            continue
        if Path(name).suffix.lower() not in suffixes:
            continue
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue

        taken_at = _parse_taken_at(name)
        has_thumb = name.lower() in thumb_names
        url = _screenshot_url(appid, KIND_FULL, name)
        shots.append(
            {
                "filename": name,
                "url": url,
                # Falling back to the original keeps the template free of
                # "is there a thumbnail" branches: the field is always usable.
                "thumb_url": _screenshot_url(appid, KIND_THUMB, name)
                if has_thumb
                else url,
                "has_thumb": has_thumb,
                "taken_at": taken_at,
                # Reading the pixel size of 292 files is not worth the open()
                # calls; the template sizes them with CSS instead.
                "width": None,
                "height": None,
                # Private: only used to order captures whose name carries no
                # timestamp. Stripped before the entry reaches a caller.
                "_mtime": None if taken_at else _mtime_of(entry),
            },
        )

    # Newest first. A capture whose name holds no date still lands in a
    # believable place via its modification time, so a user's own
    # ``下载 (1).jpg`` does not clump at the bottom of the page.
    shots.sort(key=_sort_key, reverse=True)
    for shot in shots:
        shot.pop("_mtime", None)
    return shots


def _directory_stamp(shots_dir):
    """Return the timestamps a cached listing is only valid for."""
    thumbs_dir = _thumbnails_dir(shots_dir)
    try:
        shots_stamp = shots_dir.stat().st_mtime_ns
    except OSError:
        return None
    try:
        thumbs_stamp = thumbs_dir.stat().st_mtime_ns
    except OSError:
        thumbs_stamp = 0
    return (shots_stamp, thumbs_stamp, str(shots_dir))


def _gallery_specs(key):
    """Return the ``(dir, suffixes)`` galleries to try for one key, in order.

    A Steam appid is tried first so an existing game resolves exactly as it did
    before; the local gallery is the fallback, which is what gives a hand added
    game its screenshots without touching the Steam path. A key that fails the
    Steam pattern gets an empty directory back and is skipped by the caller, so
    a UUID never even looks at the Steam root.
    """
    specs = []
    steam_dir = _screenshots_dir(key)
    if steam_dir is not None:
        specs.append((steam_dir, _SCREENSHOT_SUFFIXES))
    local_dir = local_gallery_dir(key)
    if local_dir is not None:
        specs.append((local_dir, _LOCAL_SUFFIXES))
    return specs


def list_screenshots(key):
    """Return one gallery's screenshots, newest first, or ``[]``.

    ``key`` is a Steam appid for a Steam game, or the item's ``media_id`` (a
    UUID for hand added ones) for the local gallery. The two layouts differ:
    Steam nests ``<root>/<appid>/screenshots/``, while the local gallery is
    ``<root>/<key>/`` with the images directly inside.

    Each entry is a dict with ``filename``, ``url``, ``thumb_url``,
    ``has_thumb``, ``taken_at``, ``width`` and ``height``. Only captures whose
    original file really exists are listed, so a leftover thumbnail (the user
    deleted the original) never shows up as a broken image.

    A missing directory, a malformed key or an unreadable folder all give an
    empty list rather than an error: the item page must still render.
    """
    key = str(key or "").strip()
    for shots_dir, suffixes in _gallery_specs(key):
        shots = _list_one_gallery(key, shots_dir, suffixes)
        if shots:
            return shots
    return []


def _list_one_gallery(key, shots_dir, suffixes):
    """Return the (possibly cached) listing for exactly one gallery directory."""
    stamp = _directory_stamp(shots_dir)
    if stamp is not None:
        cached = _LISTING_CACHE.get(stamp[2])
        if cached is not None and cached[0] == stamp:
            # Copy the list: a caller appending to it must not edit the cache.
            return list(cached[1])

    if not shots_dir.is_dir():
        # Nothing on this machine (a game never played, or no Steam at all).
        return []

    thumbs = _thumbnail_names(_thumbnails_dir(shots_dir))
    shots = _scan(key, shots_dir, thumbs, suffixes)

    if stamp is not None:
        if len(_LISTING_CACHE) >= _LISTING_CACHE_MAX:
            _LISTING_CACHE.pop(next(iter(_LISTING_CACHE)), None)
        # Keyed by the directory itself, not the key: "123" as a Steam appid and
        # "123" as a local gallery are different folders.
        _LISTING_CACHE[stamp[2]] = (stamp, shots)
    return list(shots)


def _request_parts(key, kind, filename):
    """Validate the three URL parts, or return None if any of them is wrong.

    Split out of :func:`resolve_screenshot_path` so the checks the URL has to
    pass read as one list, and the function that walks the disk is only about
    files. Nothing here touches the filesystem.

    ``key`` is either a Steam appid or a local gallery key. The local rule is
    deliberately the looser one and is applied to both, because an appid that
    passes ``[A-Za-z0-9_-]{1,64}`` is a superset of "digits only" and would
    change nothing on the Steam side; where the key is only usable as a real
    appid is checked by :func:`_request_galleries`.
    """
    key = str(key or "").strip()
    if not valid_gallery_key(key):
        return None

    kind = str(kind or "")
    if kind not in KINDS:
        return None

    filename = str(filename or "")
    if not is_safe_filename(filename):
        return None

    return key, kind, filename


def _request_galleries(key):
    """Return ``[(dir, root), ...]`` to search for one validated key."""
    galleries = []
    steam_dir = _screenshots_dir(key)
    if steam_dir is not None:
        galleries.append((steam_dir, screenshot_root()))
    local_dir = local_gallery_dir(key)
    if local_dir is not None:
        galleries.append((local_dir, _LOCAL_GALLERY_ROOT))
    return galleries


def _candidates(shots_dir, kind, filename):
    """Return the paths to try, in order, for one validated request."""
    if kind == KIND_THUMB:
        # A capture whose thumbnail was deleted still has to render, so the
        # original is the fallback rather than a missing image.
        return [_thumbnails_dir(shots_dir) / filename, shots_dir / filename]
    return [shots_dir / filename]


def _inside(path, root):
    """Return ``(allowed, resolved)`` for one candidate path.

    ``allowed`` False means the request itself is bad (the path leaves the
    screenshot root), so the caller refuses it outright. ``allowed`` True with
    ``resolved`` None means only this candidate is unusable (it could not be
    resolved), so the caller may still try the next one. Either way the answer
    is the same to the browser: no file, no bytes.
    """
    try:
        resolved = path.resolve()
    except OSError as error:
        logger.warning("Steam screenshots could not resolve %s: %s", path, error)
        return True, None
    if not resolved.is_relative_to(root):
        # Either a crafted ``..`` that slipped past the pattern, or a symlink
        # planted inside the cache. Both are refused the same way.
        logger.warning("Steam screenshots refused %s: outside the root", path)
        return False, None
    return True, resolved


def resolve_screenshot_path(key, kind, filename):
    """Return the file behind one screenshot URL, or None.

    This is the only place a request's strings turn into a filesystem path, so
    every part is validated before anything is touched: the key must be one
    safe path component (an appid or a local gallery key), ``kind`` must be one
    of :data:`KINDS`, and the filename must be a single safe component ending
    in a screenshot suffix (see :func:`is_safe_filename`). Every resolved path
    is then checked against the root it came from again, so neither a crafted
    ``..`` nor a symlink planted in either gallery can reach a file outside it.
    """
    parts = _request_parts(key, kind, filename)
    if parts is None:
        return None
    key, kind, filename = parts

    for shots_dir, root in _request_galleries(key):
        try:
            resolved_root = Path(root).resolve()
        except OSError as error:
            logger.warning("Steam screenshots root is unusable: %s", error)
            continue
        for candidate in _candidates(shots_dir, kind, filename):
            allowed, resolved = _inside(candidate, resolved_root)
            if not allowed:
                return None
            if resolved is None:
                continue
            try:
                if resolved.is_file():
                    return resolved
            except OSError:
                continue
    return None


def appid_for_item(item):
    """Return the screenshot gallery key for an :class:`app.models.Item`, or None.

    A Steam game is keyed by its appid, exactly as before. Any other game (a
    hand entered Switch title, an import from a notebook) is keyed by its
    ``media_id`` -- normally a UUID -- which names a folder under the local
    gallery; ``str(item.id)`` is the last resort for a game whose ``media_id``
    came out empty. Non-game items still get None: films and books have no
    screenshot gallery, and the item page reads None as "nothing to show".
    """
    if item is None:
        return None
    try:
        source = item.source
        media_type = item.media_type
        media_id = item.media_id
    except (AttributeError, ValueError):
        return None

    if media_type != MediaTypes.GAME.value:
        return None

    key = str(media_id or "").strip()
    if source == Sources.STEAM.value:
        return key if valid_appid(key) else None
    if not valid_gallery_key(key):
        key = str(item.id or "").strip()
    return key if valid_gallery_key(key) else None

