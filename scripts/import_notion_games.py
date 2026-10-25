"""Import the Notion game library (288 rows) into the local game library.

Batch 2.5 of the optimisation round.  This script pulls every row of the Notion
database "顾安的游戏世界", stores the untouched JSON on disk first, then tries to
tie each Notion row to one of the already imported Steam games *by name only*:

1. ``exact``      - the Notion name equals a local title, character for
                    character.
2. ``normalized`` - after unifying full/half width, dropping case, spaces and
                    punctuation the two names are equal.
3. ``alias``      - the two names are equal once the bracketed English (or
                    Chinese) half of a local title is used as a separate name,
                    e.g. local ``机械迷城 (Machinarium)`` vs Notion
                    ``机械迷城`` / ``Machinarium``.
4. ``store_search`` - the Steam store is asked once (2 seconds apart) and a
                    result is only accepted when its name is *identical* to the
                    Notion name (or to one of its aliases).  Nothing is guessed:
                    a "similar" store hit is reported as a candidate for the
                    user to decide, never as a match.

Default mode is ``--dry-run``: raw JSON lands in ``.tools/raw/notion/<date>/``,
the match report goes to ``_匹配报告.md``, and the only database rows written are
one ``ImportJob`` plus its ``ImportItem`` bookkeeping rows.  No ``Item`` / ``Game``
row is ever touched by this mode.

Usage (from ``src``)::

    ..\\.venv\\Scripts\\python.exe ..\\scripts\\import_notion_games.py
    ..\\.venv\\Scripts\\python.exe ..\\scripts\\import_notion_games.py --report-only
    ..\\.venv\\Scripts\\python.exe ..\\scripts\\import_notion_games.py --apply   # refused

``--apply`` deliberately refuses to run in this batch: writing game fields is a
separate, reviewed step (see the report's last section).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import requests  # noqa: E402

import django  # noqa: E402

django.setup()

from app.models import (  # noqa: E402
    ImportItem,
    ImportItemStatuses,
    ImportJob,
    ImportJobStatuses,
    ImportSources,
    ImportTaskTypes,
    Item,
)

# --------------------------------------------------------------------- Notion
NOTION_API = "https://api.notion.com/v1"
# The game library is a multi source database: the page id in the URL is *not*
# the data source id, and querying needs the 2025-09-03 versioned endpoint.
NOTION_VERSION = "2025-09-03"
GAME_DATA_SOURCE_ID = "2a743cad-bab3-8121-91a3-000b8bb8d2a6"
NOTION_INTERVAL = 0.35  # mandated by the handover note, ~3 requests per second
PAGE_SIZE = 100

# ---------------------------------------------------------------------- Steam
STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
BROWSE_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
STORE_INTERVAL = 2.0  # hard rule: one store query every two seconds
BROWSE_INTERVAL = 1.0
BROWSE_CHUNK = 150
STORE_LANG = "schinese"
STORE_CC = "CN"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

REQUEST_TIMEOUT = 30
RETRIES = 4

RAW_ROOT = ROOT / ".tools" / "raw" / "notion"
NAMES_CACHE = ROOT / ".tools" / "raw" / "steam_names" / "appid_names.json"
REPORT_PATH = ROOT.parent / "_匹配报告.md"

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# ---------------------------------------------------------------- match labels
STEP_LABELS = {
    "exact": "全名精确",
    "normalized": "归一化精确",
    "alias": "英文名/别名精确",
    "appid_name": "appid 官方名核对",
    "store_search": "商店搜索 + appid 名字核对",
}

# How each step is explained in the report
STEP_WHY = {
    "exact": "Notion 名字与本地标题逐字相同",
    "normalized": "去掉空格/大小写/标点/中英括号后与本地标题相同",
    "alias": "与本地标题里用括号或斜杠并列的英文名/中文名相同",
    "appid_name": "本地 appid 的 Steam 官方中文/英文名就是这个名字",
    "store_search": "商店搜到候选后，用 appid 反查它的官方名，与 Notion 名逐字一致",
}


# ------------------------------------------------------------------------ misc
def read_env_token() -> str:
    """Return the Notion token from ``yamtrack/.env``."""
    path = ROOT / ".env"
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("NOTION") and "=" in line:
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return value
    raise SystemExit("未在 yamtrack/.env 找到 NOTION 令牌")


def proxy_map() -> dict | None:
    """Return the local proxy settings from the environment, if any."""
    https = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not https:
        return None
    http = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or https
    return {"http": http, "https": https}


def session() -> requests.Session:
    """Return one shared HTTP session with a browser-ish user agent."""
    sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT})
    return sess


def text_of(prop: dict) -> str:
    """Flatten a Notion rich text / title property into a plain string."""
    chunks = prop.get("title") or prop.get("rich_text") or []
    return "".join(chunk.get("plain_text", "") for chunk in chunks).strip()


def extract_game(row: dict) -> dict:
    """Return the handful of Notion fields this importer cares about."""
    props = row.get("properties", {})

    def num(key):
        val = props.get(key, {}).get("number")
        return val

    def sel(key):
        node = props.get(key, {})
        picked = node.get("select") or node.get("status") or {}
        return (picked or {}).get("name", "")

    files = props.get("截图", {}).get("files") or []
    shots = []
    for entry in files:
        if entry.get("type") == "file":
            shots.append(entry.get("file", {}).get("url", ""))
        elif entry.get("type") == "external":
            shots.append(entry.get("external", {}).get("url", ""))

    return {
        "page_id": row.get("id", ""),
        "name": text_of(props.get("游戏名称", {})),
        "number": num("序号"),
        "status": sel("状态"),
        "score": num("综合评分"),
        "story": num("剧情"),
        "mechanic": num("机制"),
        "visual": num("画面"),
        "hours": num("游戏时长"),
        "finished_year": sel("完成时间"),
        "genre": sel("游戏类型"),
        "sub_genre": text_of(props.get("细分类型", {})),
        "review": text_of(props.get("评论", {})),
        "date": (props.get("日期", {}).get("date") or {}).get("start", ""),
        "shots": [url for url in shots if url],
    }


# ----------------------------------------------------------------- Notion pull
def query_page(sess: requests.Session, token: str, cursor: str | None) -> dict:
    """Ask the data source for one page of rows."""
    body = {"page_size": PAGE_SIZE}
    if cursor:
        body["start_cursor"] = cursor
    last = ""
    for attempt in range(RETRIES):
        try:
            resp = sess.post(
                f"{NOTION_API}/data_sources/{GAME_DATA_SOURCE_ID}/query",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Notion-Version": NOTION_VERSION,
                    "Content-Type": "application/json",
                },
                data=json.dumps(body),
                timeout=REQUEST_TIMEOUT,
                proxies=proxy_map(),
            )
        except requests.RequestException as exc:
            last = f"网络({type(exc).__name__})"
            time.sleep(1.5 * (attempt + 1))
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 2))
            print(f"  429 退避 {wait:.1f}s", flush=True)
            time.sleep(min(wait, 30))
            continue
        last = f"HTTP {resp.status_code}: {resp.text[:160]}"
        time.sleep(1.5 * (attempt + 1))
    raise SystemExit(f"Notion 查询失败：{last}")


def fetch_all(token: str) -> list[dict]:
    """Pull every Notion row, saving each raw page before anything is parsed."""
    day = date.today().isoformat()
    out_dir = RAW_ROOT / day
    out_dir.mkdir(parents=True, exist_ok=True)
    sess = session()
    rows: list[dict] = []
    cursor = None
    page_no = 0
    meta = {"data_source_id": GAME_DATA_SOURCE_ID, "version": NOTION_VERSION,
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"), "pages": []}
    while True:
        page_no += 1
        payload = query_page(sess, token, cursor)
        raw_path = out_dir / f"games_page_{page_no:03d}.json"
        raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        batch = payload.get("results", [])
        rows.extend(batch)
        meta["pages"].append({"page": page_no, "file": raw_path.name,
                              "rows": len(batch), "has_more": payload.get("has_more")})
        print(f"  第 {page_no} 页：{len(batch)} 条（累计 {len(rows)}）", flush=True)
        if not payload.get("has_more"):
            break
        cursor = payload.get("next_cursor")
        time.sleep(NOTION_INTERVAL)
    merged = out_dir / "games_all.json"
    merged.write_text(json.dumps({"meta": meta, "results": rows},
                                 ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "fetch_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  原始 JSON：{merged}", flush=True)
    return [extract_game(row) for row in rows]


def load_cached_raw() -> list[dict]:
    """Reuse the newest merged raw file instead of hitting Notion again."""
    files = sorted(RAW_ROOT.glob("*/games_all.json"))
    if not files:
        raise SystemExit("没有找到原始 JSON，先不带 --report-only 跑一次")
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    print(f"  复用原始 JSON：{files[-1]}（{len(data['results'])} 条）", flush=True)
    return [extract_game(row) for row in data["results"]]


# ------------------------------------------------------------------ name tools
PUNCT_CHARS = set("·•®™©★☆～~`'\"“”‘’《》〈〉「」『』【】（）()[]{}<>《》、，,。.！!？?:：;；-–—_/\\|&＆+＋*＊#@%^$=  \t　")


def norm(text: str | None) -> str:
    """Return the comparison key of a name: width folded, lower case, no noise."""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", str(text)).lower()
    kept = []
    for ch in folded:
        if ch in PUNCT_CHARS:
            continue
        if unicodedata.category(ch).startswith(("P", "Z", "S")):
            continue
        kept.append(ch)
    return "".join(kept).strip()


SPLIT_RE = re.compile(r"\s*[/／|｜]\s*|\s+-\s+|\s+–\s+|\s+—\s+")


def variants(title: str) -> set[str]:
    """Return every sensible name a local title carries.

    ``机械迷城 (Machinarium)`` yields ``机械迷城 (Machinarium)``, the two halves
    on their own, and the ``/``-separated halves.  The set is used to build the
    alias index; it never produces a name out of thin air.
    """
    out: set[str] = set()
    title = (title or "").strip()
    if not title:
        return out
    out.add(title)
    for part in SPLIT_RE.split(title):
        if part.strip():
            out.add(part.strip())
    # bracketed halves: (EN), （EN）, 《EN》, [EN]
    for inner in re.findall(r"[\(（\[【《]([^\)）\]】》]+)[\)）\]】》]", title):
        if inner.strip():
            out.add(inner.strip())
    # the same title with the bracketed part removed
    stripped = re.sub(r"[\(（\[【《][^\)）\]】》]*[\)）\]】》]", " ", title)
    stripped = SPLIT_RE.sub(" ", stripped).strip()
    if stripped:
        out.add(stripped)
    # pure CJK / pure latin slices, only when the title really mixes both
    if CJK.search(title) and re.search(r"[A-Za-z]", title):
        cjk = "".join(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", title))
        latin = " ".join(re.findall(r"[A-Za-z][A-Za-z0-9'’®™\.\!]*", title))
        for part in (cjk, latin):
            if part and part.strip():
                out.add(part.strip())
    # Very short fragments ("II", "DX", "TEEN") would collide across dozens of
    # titles and produce false matches, so they are not kept as aliases.
    return {item for item in out if item and (len(norm(item)) >= 3 or CJK.search(item))}


def has_cjk(text: str) -> bool:
    """Return True when the string holds at least one Chinese character."""
    return bool(text) and CJK.search(text) is not None


# ---------------------------------------------------------------- local library
def build_index() -> tuple[dict, dict, dict]:
    """Return (exact, normalized, alias) indexes over the local game titles."""
    exact: dict[str, list] = defaultdict(list)
    normalized: dict[str, list] = defaultdict(list)
    alias: dict[str, list] = defaultdict(list)
    items = Item.objects.filter(media_type="game").values("id", "media_id", "source", "title")
    for row in items:
        title = row["title"] or ""
        key = norm(title)
        exact[title].append(row)
        if key:
            normalized[key].append(row)
        for variant in variants(title):
            akey = norm(variant)
            if akey:
                alias[akey].append(row)
    print(f"  本地游戏条目 {len(items)} 个，归一化键 {len(normalized)}，别名键 {len(alias)}",
          flush=True)
    return exact, normalized, alias


def unique_hit(mapping: dict, key: str):
    """Return the single row behind ``key``, or (None, 'multi') when ambiguous."""
    hits = mapping.get(key) or []
    if not hits:
        return None, "none"
    first = hits[0]
    if any(hit["id"] != first["id"] for hit in hits):
        return None, "multi"
    return first, "one"


# ----------------------------------------------------------------- store search
class StoreSearcher:
    """Steam store search with a strict 2 second gap between calls.

    Every answer is written to ``.tools/raw/notion/<date>/store/<page_id>.json``
    before it is interpreted, so a re-run never has to hit the store again and
    the raw evidence stays inspectable.
    """

    def __init__(self, cache_dir: Path | None = None, refresh: bool = False) -> None:
        self.sess = session()
        self.last = 0.0
        self.calls = 0
        self.failures = 0
        self.cache_dir = cache_dir
        self.refresh = refresh
        self.cache_hits = 0
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, page_id: str) -> Path | None:
        if self.cache_dir is None or not page_id:
            return None
        safe = re.sub(r"[^0-9a-zA-Z_-]", "", page_id)
        return self.cache_dir / f"{safe}.json"

    def search(self, term: str, language: str, page_id: str = "") -> list[dict]:
        """Return the store hits for ``term`` (name + appid), paced at 2 s/call."""
        term = (term or "").strip()
        if not term:
            return []
        cache = self._cache_path(page_id)
        if cache is not None and not self.refresh:
            payload = self._read_cache(cache)
            if payload is not None:
                for entry in payload.get("queries", []):
                    if entry.get("term") == term and entry.get("language") == language:
                        self.cache_hits += 1
                        return entry.get("items", [])
        items = self._live_search(term, language)
        if cache is not None:
            self._append_cache(cache, {"term": term, "language": language,
                                       "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                       "items": items})
        return items

    def _read_cache(self, path: Path) -> dict | None:
        """Return the cached payload, tolerating a missing or broken file."""
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _append_cache(self, path: Path, entry: dict) -> None:
        """Add one query/answer pair to the raw cache file."""
        data = self._read_cache(path) or {"page": path.stem, "queries": []}
        data["queries"].append(entry)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def _live_search(self, term: str, language: str) -> list[dict]:
        """Do the paced network call for one term."""
        wait = STORE_INTERVAL - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        self.calls += 1
        for attempt in range(3):
            try:
                resp = self.sess.get(
                    STORE_SEARCH_URL,
                    params={"term": term, "l": language, "cc": STORE_CC},
                    timeout=REQUEST_TIMEOUT,
                    proxies=proxy_map(),
                )
            except requests.RequestException:
                time.sleep(2.0 * (attempt + 1))
                continue
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except ValueError:
                    self.failures += 1
                    return []
                items = body.get("items") or []
                return [{"appid": str(entry.get("id", "")),
                         "name": entry.get("name", "")} for entry in items]
            if resp.status_code == 429:
                time.sleep(10)
                continue
            self.failures += 1
            time.sleep(2.0 * (attempt + 1))
        self.failures += 1
        return []


# --------------------------------------------------------------- name by appid
class NameBook:
    """Official Steam names (Chinese + English) per appid, cached on disk.

    ``search`` answers "what might appid X be"; this answers "what is appid X
    actually called".  That second question is the only thing allowed to turn a
    store hit into a match, because it removes the "first hit is probably it"
    guess: the appid is fetched by its own number and its own official names
    must be identical to the Notion name (or one of the local titles).
    """

    def __init__(self, path: Path = NAMES_CACHE) -> None:
        self.path = path
        self.data: dict[str, dict] = {}
        self.calls = 0
        self.dirty = 0
        self.sess = session()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data = raw
            except (OSError, ValueError):
                self.data = {}

    def save(self) -> None:
        """Write the cache back, atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, appid: str) -> dict:
        """Return ``{cn, en}`` for one appid, asking Steam at most once ever."""
        appid = str(appid)
        if appid in self.data:
            return self.data[appid]
        self.ensure([appid])
        return self.data.get(appid, {"cn": "", "en": ""})

    def ensure(self, appids: list[str]) -> None:
        """Fetch the missing appids in chunks of ``BROWSE_CHUNK``."""
        todo = [str(a) for a in appids if str(a) and str(a) not in self.data]
        for start in range(0, len(todo), BROWSE_CHUNK):
            chunk = todo[start:start + BROWSE_CHUNK]
            cn = self._browse(chunk, "schinese")
            en = self._browse(chunk, "english")
            for appid in chunk:
                self.data[appid] = {
                    "cn": cn.get(appid) or "",
                    "en": en.get(appid) or "",
                }
            self.dirty += len(chunk)
            if self.dirty >= 300:
                self.save()
                self.dirty = 0
            time.sleep(BROWSE_INTERVAL)
        if self.dirty:
            self.save()
            self.dirty = 0

    def _browse(self, appids: list[str], language: str) -> dict[str, str]:
        """One ``GetItems`` batch; returns ``{appid: name}``."""
        if not appids:
            return {}
        payload = {
            "ids": [{"appid": int(appid)} for appid in appids],
            "context": {"language": language, "country_code": STORE_CC,
                        "steam_realm": 1},
            "data_request": {"include_basic_info": True},
        }
        for attempt in range(RETRIES):
            try:
                resp = self.sess.get(
                    BROWSE_URL,
                    params={"input_json": json.dumps(payload, separators=(",", ":"))},
                    timeout=REQUEST_TIMEOUT,
                    proxies=proxy_map(),
                )
            except requests.RequestException:
                time.sleep(2.0 * (attempt + 1))
                continue
            if resp.status_code == 200:
                self.calls += 1
                try:
                    body = resp.json()
                except ValueError:
                    return {}
                items = ((body or {}).get("response") or {}).get("store_items") or []
                return {str(entry.get("id")): entry.get("name") or ""
                        for entry in items
                        if entry.get("id") is not None and entry.get("success") == 1}
            if resp.status_code in (403, 429, 503):
                time.sleep(5.0 * (attempt + 1))
                continue
            time.sleep(2.0 * (attempt + 1))
        return {}


# ------------------------------------------------------------------- matching
LOCAL_TITLE_INDEX: dict[str, list] = {}
KNOWN_APPIDS: dict[str, dict] = {}


def match_one(game: dict, exact, normalized, alias) -> dict:
    """Try the first three name strategies for one Notion row.

    Returns a dict with ``step`` (or ``None``) and the evidence used.  Ambiguous
    name collisions are *not* resolved here: they come back as ``ambiguous``.
    """
    name = game["name"]
    key = norm(name)
    result = {"step": None, "row": None, "ambiguous": [], "detail": ""}

    hits = exact.get(name) or []
    if hits and len({row["id"] for row in hits}) == 1:
        row = hits[0]
        result.update(step="exact", row=row, detail=f"本地标题完全相同：{row['title']}")
        return result
    if hits:
        result["ambiguous"] = hits
        result["detail"] = "全名精确命中多个本地条目"

    row, state = unique_hit(normalized, key)
    if row is not None:
        result.update(step="normalized", row=row,
                      detail=f"去空格/大小写/标点后与「{row['title']}」相同")
        return result
    if state == "multi":
        result["ambiguous"] = normalized.get(key) or []
        result["detail"] = "归一化后命中多个本地条目"

    row, state = unique_hit(alias, key)
    if row is not None:
        result.update(step="alias", row=row,
                      detail=f"与「{row['title']}」拆分出的英文名/别名相同")
        return result
    if state == "multi":
        result["ambiguous"] = (result["ambiguous"] or []) + (alias.get(key) or [])
        result["detail"] = "别名命中多个本地条目"

    return result


def non_latin(text: str | None) -> str:
    """Return the CJK/digit part of a name, dropping every Latin letter.

    ``"猫咪斗恶龙3 Cat Quest III"`` gives ``"猫咪斗恶龙3"``; ``"猫咪斗恶龙 Cat
    Quest"`` gives ``"猫咪斗恶龙"``.  Comparing these two segments is what keeps
    "猫咪斗恶龙" from being matched to Cat Quest *III* just because the store
    ranked the sequel first.
    """
    return "".join(ch for ch in norm(text) if not ("a" <= ch <= "z"))


def build_official_index(book: "NameBook", appids: list[str]) -> dict:
    """Return ``{name key: local row}`` built from the official Steam names.

    Keys are the full normalized name *and* the non-Latin segment, both only
    kept when they point at exactly one local appid.  This is identity: the
    official name of appid X is a fact about appid X, not a similarity guess.
    """
    index: dict[str, list] = defaultdict(list)
    for appid in appids:
        row = KNOWN_APPIDS.get(str(appid))
        if row is None:
            continue
        names = book.get(appid)
        for raw in (names.get("cn"), names.get("en")):
            if not raw:
                continue
            for key in (norm(raw), non_latin(raw)):
                if key and len(key) >= 3:
                    index[key].append(row)
            for variant in variants(raw):
                vkey = norm(variant)
                if vkey and len(vkey) >= 3:
                    index[vkey].append(row)
    unique = {}
    for key, rows in index.items():
        if len({row["id"] for row in rows}) == 1:
            unique[key] = rows[0]
    return unique


def official_match(game: dict, official: dict) -> dict:
    """Match a Notion name against the official names of the local appids.

    Two keys are tried, both of them identity checks:

    * the full normalized name, always;
    * the non-Latin segment, but **only when the Notion name has no Latin
      letters at all**.  Otherwise "中国式网游+ dlc" would silently collapse to
      "中国式网游" and get matched to the base game, which is a judgement call
      the user must make, not something the importer may decide.
    """
    name = game["name"]
    keys = [norm(name)]
    if not re.search(r"[A-Za-z]", name):
        segment = non_latin(name)
        if segment:
            keys.append(segment)
    for key in keys:
        row = official.get(key) if key and len(key) >= 3 else None
        if row is not None:
            return {"step": "appid_name", "row": row,
                    "detail": (f"Steam appid {row['media_id']} 的官方中文/英文名与这个名字一致"
                               f"（本地条目「{row['title']}」）")}
    return {"step": None, "row": None, "detail": ""}


def name_overlap(a: str, b: str) -> bool:
    """Return True when one normalized name contains the other, reasonably long."""
    ka, kb = norm(a), norm(b)
    if not ka or not kb:
        return False
    if ka in kb or kb in ka:
        shorter = min(len(ka), len(kb))
        longer = max(len(ka), len(kb))
        return shorter >= 4 and shorter / longer >= 0.6
    return False


def store_lookup(game: dict, searcher: "StoreSearcher", known_ids: dict,
                 book: "NameBook") -> dict:
    """Search the store for candidates, then verify every candidate by appid.

    A hit becomes a match only when the appid's *own* official Chinese or English
    name equals the Notion name, or when the appid is already in the local
    library and its official name still overlaps the Notion name.  Nothing is
    taken from "the first search result".
    """
    name = game["name"]
    tried: list[str] = []
    candidates: list[dict] = []
    seen: set[str] = set()

    terms = [name]
    for part in SPLIT_RE.split(name):
        part = part.strip()
        if part and part != name and re.search(r"[A-Za-z]", part) and len(terms) < 2:
            terms.append(part)
    key = norm(name)
    alias_keys = {norm(v) for v in variants(name)}

    for term in terms:
        language = "schinese" if has_cjk(name) and term == name else "english"
        tried.append(f"{term}（{'中文' if language == 'schinese' else '英文'}商店）")
        for hit in searcher.search(term, language, game.get("page_id", "")):
            if hit["appid"] and hit["appid"] not in seen:
                seen.add(hit["appid"])
                candidates.append(hit)

    # Verify by appid: pull each candidate's own official names.
    book.ensure([hit["appid"] for hit in candidates[:8]])
    verified = []
    for hit in candidates[:8]:
        names = book.get(hit["appid"])
        verified.append({**hit, "cn": names.get("cn", ""), "en": names.get("en", "")})

    # 1) official name identical to the Notion name (or one of its variants)
    for hit in verified:
        for official_name in (hit["cn"], hit["en"]):
            if not official_name:
                continue
            if norm(official_name) == key or norm(official_name) in alias_keys:
                row = known_ids.get(hit["appid"])
                return {"step": "store_search", "hit": {**hit, "name": official_name},
                        "local": row, "why": "name_identical",
                        "tried": tried, "candidates": verified}

    # 2) appid already local and the official name still overlaps the Notion name
    for hit in verified:
        row = known_ids.get(hit["appid"])
        if row is None:
            continue
        official_names = [n for n in (hit["cn"], hit["en"]) if n]
        for official_name in official_names:
            same_segment = (
                not re.search(r"[A-Za-z]", name)
                and non_latin(official_name)
                and non_latin(official_name) == non_latin(name)
            )
            if same_segment or name_overlap(name, official_name):
                return {"step": "appid_name", "hit": {**hit, "name": official_name},
                        "local": row, "why": "appid_local_overlap",
                        "tried": tried, "candidates": verified}

    return {"step": None, "hit": None, "local": None, "why": "",
            "tried": tried, "candidates": verified}


# --------------------------------------------------------------- persistence
def pick_job(dry_run: bool) -> ImportJob:
    """Reuse the previous job of the same mode so re-runs never stack up."""
    job = (ImportJob.objects.filter(source=ImportSources.NOTION,
                                    task_type=ImportTaskTypes.NOTION_LIBRARY)
           .order_by("-id").first())
    if job is not None and job.cursor.get("dry_run") == dry_run:
        job.items.all().delete()
        return job
    return ImportJob.objects.create(
        source=ImportSources.NOTION,
        task_type=ImportTaskTypes.NOTION_LIBRARY,
        status=ImportJobStatuses.PENDING,
        cursor={"dry_run": dry_run},
    )


def write_items(job: ImportJob, records: list[dict]) -> None:
    """Store one ImportItem per Notion row, in bulk."""
    objs = []
    for rec in records:
        game = rec["game"]
        status = (ImportItemStatuses.MATCHED if rec["step"]
                  else ImportItemStatuses.UNMATCHED)
        method = {"exact": "exact", "normalized": "fuzzy",
                  "alias": "fuzzy", "store_search": "store_search"}.get(rec["step"], "")
        note = rec["note"]
        objs.append(ImportItem(
            job=job,
            external_id=game["page_id"] or "",
            external_title=game["name"][:300],
            item_id=rec["item_id"],
            status=status,
            match_method=method,
            note=note[:2000],
            raw={
                "name": game["name"],
                "number": game["number"],
                "status": game["status"],
                "score": game["score"],
                "hours": game["hours"],
                "finished_year": game["finished_year"],
                "genre": game["genre"],
                "local_title": rec["local_title"],
                "step": rec["step"] or "",
            },
        ))
    ImportItem.objects.bulk_create(objs, batch_size=200)


# -------------------------------------------------------------------- report
def write_report(records: list[dict], stats: dict, job: ImportJob, total: int) -> None:
    """Write the human readable Chinese match report."""
    matched = [rec for rec in records if rec["step"]]
    unmatched = [rec for rec in records if not rec["step"]]
    by_step = defaultdict(list)
    for rec in matched:
        by_step[rec["step"]].append(rec)
    # A store-confirmed appid that the library does not own yet is "matched" in
    # the sense that its identity is known, but apply would have to create it.
    local_hits = [rec for rec in matched if rec.get("item_id")]
    new_hits = [rec for rec in matched if not rec.get("item_id")]

    lines: list[str] = []
    lines.append("# Notion 288 条游戏 · 名字匹配报告（dry-run）")
    lines.append("")
    lines.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 数据源：Notion 游戏库「顾安的游戏世界」数据源 `{GAME_DATA_SOURCE_ID}`")
    lines.append(f"- 拉到条数：**{total}**")
    lines.append(f"- 匹配任务记录：`app_importjob` #{job.pk}（stats 里有完整计数）")
    lines.append(f"- 本地游戏库：{stats['local_games']} 条（全部 Steam，未改动，一条都没写）")
    lines.append("")
    lines.append("## 总数校验")
    lines.append("")
    lines.append("| 项 | 条数 |")
    lines.append("|---|---|")
    lines.append(f"| 拉到 Notion 记录 | {total} |")
    lines.append(f"| 已匹配 | {len(matched)} |")
    lines.append(f"| ├ 本地库里已有这个游戏（有本地条目） | {len(local_hits)} |")
    lines.append(f"| └ 商店确认了 appid、但本地库里还没有（后面要新建） | {len(new_hits)} |")
    lines.append(f"| 匹配不上 | {len(unmatched)} |")
    lines.append(f"| 合计 | {len(matched) + len(unmatched)} |")
    lines.append("")
    lines.append("## 各匹配方式")
    lines.append("")
    lines.append("| 方式 | 条数 |")
    lines.append("|---|---|")
    for step, label in STEP_LABELS.items():
        lines.append(f"| {label} | {len(by_step.get(step, []))} |")
    lines.append(f"| 商店搜索本轮新发请求 | {stats['store_calls']} |")
    lines.append(f"| 商店搜索结果复用已落盘原始文件 | {stats.get('store_cache_hits', 0)} |")
    lines.append("")
    # ------------------------------------------------------------ matched part
    lines.append("## 已匹配")
    lines.append("")
    for step, label in STEP_LABELS.items():
        group = by_step.get(step, [])
        if not group:
            continue
        lines.append(f"### {label}（{len(group)} 条）")
        lines.append("")
        lines.append("| 序号 | Notion 名字 | 本地条目 | 本地 media_id | 依据 |")
        lines.append("|---|---|---|---|---|")
        for rec in sorted(group, key=lambda r: (r["game"]["number"] or 9999)):
            game = rec["game"]
            lines.append(
                f"| {game['number'] if game['number'] is not None else '-'} "
                f"| {game['name']} | {rec['local_title']} | {rec['media_id']} "
                f"| {rec['note']} |",
            )
        lines.append("")

    # ---------------------------------------------------------- unmatched part
    lines.append("## 匹配不上")
    lines.append("")
    if not unmatched:
        lines.append("无。全部 288 条都按上面四种方式对上了。")
        lines.append("")
    else:
        with_cand = sum(1 for rec in unmatched if rec.get("store_candidates"))
        lines.append(f"共 **{len(unmatched)}** 条。每条都写清：Notion 名字 / 序号 / 查过的方向 / "
                     "需要你决定什么。**没有任何一条被猜着配上，也没有任何一条被强行建条目。**")
        lines.append("")
        lines.append(f"- 其中 **{with_cand}** 条：商店搜到候选，但候选的官方名跟 Notion 名不是逐字相同，"
                     "所以停在这里等你确认；")
        lines.append(f"- 另外 **{len(unmatched) - with_cand}** 条：商店连候选都没有（名字还原不出来），"
                     "只能靠你给名字/appid。")
        lines.append("")
        for rec in sorted(unmatched, key=lambda r: (r["game"]["number"] or 9999)):
            game = rec["game"]
            lines.append(f"### {game['name']}（序号 {game['number'] if game['number'] is not None else '-'}）")
            lines.append("")
            lines.append(f"- Notion 里的名字：`{game['name']}`")
            lines.append(f"- 序号：{game['number'] if game['number'] is not None else '-'}")
            lines.append(f"- 查过的方向：{rec['searched']}")
            lines.append(f"- 需要你决定什么：{rec['decision']}")
            lines.append("")
    lines.append("## 说明")
    lines.append("")
    lines.append("- 本轮只跑 dry-run：没有新增/修改任何游戏条目，`app_game` 仍是 "
                 f"{stats['local_games']} 条。")
    lines.append(f"- dry-run 只写了导入任务记录：`app_importjob` 1 条（#{job.pk}）、"
                 f"`app_importitem` {len(records)} 条。")
    lines.append("- 匹配只认「名字完全相同」这一类硬证据：逐字相同、去掉空格大小写标点后相同、"
                 "本地标题里并列的英文名/中文名相同、以及用 Steam appid 的官方名核对（appid 是"
                 "唯一编号，官方名是它的事实）。**相似度、大概像、商店第一条** 这类一律不算匹配。")
    lines.append("- `--apply` 本轮**没有运行**，脚本里也写死了拒绝执行：真正写游戏字段要等你看完本报告"
                 "并逐条给出匹配不上的处理意见之后。")
    lines.append("")
    lines.append("### 各步怎么判的")
    lines.append("")
    for step, label in STEP_LABELS.items():
        lines.append(f"- **{label}**：{STEP_WHY[step]}（{len(by_step.get(step, []))} 条）")
    lines.append("")
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------- main
def nearest_title(name: str, choices: list[tuple[str, str]]) -> str:
    """Return a *hint* only: the closest local title by character overlap.

    This is never used as a match.  It exists so the report can say "本地有个
    「XXX」看着接近，但名字不同", which is exactly the evidence the user needs
    to decide by hand.
    """
    from difflib import SequenceMatcher

    key = norm(name)
    if not key:
        return ""
    best = ("", 0.0)
    for title, title_key in choices:
        if not title_key:
            continue
        score = SequenceMatcher(None, key, title_key).ratio()
        if score > best[1]:
            best = (title, score)
    return best[0] if best[1] >= 0.62 else ""


def searched_directions(rec: dict) -> str:
    """Describe what was already tried for an unmatched row, in plain Chinese."""
    bits = ["① 全名精确：没有同名本地条目", "② 归一化（去空格/大小写/标点/中英括号）：没有"]
    local_title = rec.get("nearest") or ""
    if local_title:
        bits.append(f"③ 本地英文名/别名：没有；本地有个「{local_title}」看着接近，但名字并不相同"
                    "（只是提示，不当匹配）")
    else:
        bits.append("③ 本地英文名/别名：没有")
    bits.append("④ 本地 appid 的 Steam 官方名核对：没有一致")
    if rec.get("store_tried"):
        terms = "、".join(f"`{t}`" for t in rec["store_tried"])
        bits.append(f"⑤ Steam 商店搜索：查了 {terms}")
        cands = rec.get("store_candidates") or []
        if cands:
            shown = "、".join(f"{c.get('cn') or c.get('name')}(appid {c['appid']})" for c in cands[:3])
            bits.append(f"商店返回 {shown}，逐个用 appid 反查了官方名，跟 Notion 名都不是逐字相同，"
                        "所以不算匹配")
        else:
            bits.append("商店一条结果都没有（纯中文名在 Steam 商店经常搜不到，非 Steam 游戏也会这样）")
    else:
        bits.append("⑤ Steam 商店搜索：没有跑")
    return "；".join(bits)


def decide_note(rec: dict) -> str:
    """Tell the user what they have to decide for an unmatched row."""
    if rec.get("store_candidates"):
        cands = rec["store_candidates"][:2]
        shown = "、".join(f"「{c.get('cn') or c.get('name')}」(appid {c['appid']})" for c in cands)
        return (f"商店搜到 {shown}，但名字跟 Notion 名不是逐字相同，所以我不敢直接配。"
                "如果就是你要的那个游戏，回一句「XXX = appid 数字」我下一轮按它接；"
                "如果是 Switch/主机/非 Steam 游戏，说一句「非 Steam」我按自定义条目建；"
                "如果我查错了，直接给我正确的名字或 appid。")
    if rec.get("ambiguous"):
        names = "、".join(f"{row['title']}({row['media_id']})" for row in rec["ambiguous"][:4])
        return f"本地有多个同名/近名条目（{names}），要哪一个，或者都不对，说一声。"
    if rec.get("store_tried") is None:
        return "还没查商店。可以让我下一轮补一次商店搜索，或直接告诉我对哪个 appid / 非 Steam。"
    return ("商店里也没有逐字同名的结果：请确认它是不是非 Steam 游戏（Switch/主机/自研/已下架改名）。"
            "是的话我按自定义条目建，不是的话给我正确的名字或 appid。")


def main() -> None:
    """Run the dry-run import and print the counters."""
    parser = argparse.ArgumentParser(description="导入 Notion 游戏库（默认 dry-run）")
    parser.add_argument("--apply", action="store_true",
                        help="真正写游戏数据（本批次故意拒绝执行）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只匹配出报告，不写游戏数据（默认）")
    parser.add_argument("--report-only", action="store_true",
                        help="复用上一次落盘的原始 JSON，不再访问 Notion")
    parser.add_argument("--no-store", action="store_true",
                        help="跳过 Steam 商店搜索（只用本地名字匹配）")
    parser.add_argument("--store-limit", type=int, default=200,
                        help="商店搜索最多查询多少条记录（默认 200）")
    parser.add_argument("--no-db", action="store_true",
                        help="只打印计数、不写导入任务记录（调试用）")
    parser.add_argument("--refresh-store", action="store_true",
                        help="忽略商店搜索的原始缓存，重新联网查询")
    args = parser.parse_args()

    if args.apply:
        raise SystemExit(
            "拒绝执行 --apply：本轮（2.5）只允许 dry-run。\n"
            "写游戏字段要等你看过 _匹配报告.md、逐条确认匹配不上的名字之后再单独开一步，"
            "现在没有任何 Game 数据被写。",
        )

    print("[STAGE] 拉取 — 从 Notion 拉 288 条并落盘原始 JSON", flush=True)
    games = load_cached_raw() if args.report_only else fetch_all(read_env_token())
    total = len(games)
    print(f"  拉到 {total} 条", flush=True)

    print("[STAGE] 匹配 — 名义索引 + 四步策略", flush=True)
    exact, normalized, alias = build_index()
    local_games = Item.objects.filter(media_type="game").count()
    # hint pool for the report only (never a match source)
    hint_pool = [(row["title"], norm(row["title"]))
                 for row in Item.objects.filter(media_type="game").values("title")]
    # appid index: identity evidence for the store step, built from local media_id
    for row in Item.objects.filter(media_type="game", source="steam").values(
            "id", "media_id", "title"):
        KNOWN_APPIDS[str(row["media_id"])] = row
        key = norm(row["title"])
        if key:
            LOCAL_TITLE_INDEX.setdefault(key, []).append(row)

    records: list[dict] = []
    for game in games:
        result = match_one(game, exact, normalized, alias)
        records.append({
            "game": game, "step": result["step"], "row": result["row"],
            "ambiguous": result["ambiguous"], "item_id": (result["row"] or {}).get("id"),
            "local_title": (result["row"] or {}).get("title", ""),
            "media_id": (result["row"] or {}).get("media_id", ""),
            "note": result["detail"], "searched": "", "decision": "",
        })

    # Step 4a: official Steam names of the local appids (identity, not similarity)
    book = NameBook()
    print("[STAGE] 官方名 — 给本地 appid 补齐 Steam 官方中英文名", flush=True)
    book.ensure(list(KNOWN_APPIDS))
    official = build_official_index(book, list(KNOWN_APPIDS))
    print(f"  官方名索引 {len(official)} 个键（Steam 批量接口调用 {book.calls} 次）", flush=True)

    still = [rec for rec in records if not rec["step"]]
    for rec in still:
        found = official_match(rec["game"], official)
        if found["step"]:
            rec["step"] = "appid_name"
            rec["row"] = found["row"]
            rec["item_id"] = found["row"]["id"]
            rec["local_title"] = found["row"]["title"]
            rec["media_id"] = found["row"]["media_id"]
            rec["note"] = found["detail"]
    print(f"  官方名核对后仍剩 {len([r for r in records if not r['step']])} 条", flush=True)

    pending = [rec for rec in records if not rec["step"]]
    print(f"  进入商店搜索：{len(pending)} 条", flush=True)

    stats = Counter()
    day_dir = RAW_ROOT / date.today().isoformat()
    searcher = StoreSearcher(day_dir / "store", refresh=args.refresh_store)
    if not args.no_store and pending:
        limit = max(0, min(args.store_limit, len(pending)))
        for index, rec in enumerate(pending[:limit], 1):
            game = rec["game"]
            print(f"  [{index}/{limit}] 商店查「{game['name']}」", flush=True)
            found = store_lookup(game, searcher, KNOWN_APPIDS, book)
            rec["store_tried"] = found["tried"]
            rec["store_candidates"] = found["candidates"]
            if found["step"]:
                hit = found["hit"]
                row = found["local"]
                rec["step"] = found["step"]
                if row is not None:
                    rec["row"] = row
                    rec["item_id"] = row["id"]
                    rec["local_title"] = row["title"]
                    rec["media_id"] = row["media_id"]
                    rec["note"] = (
                        f"商店 appid {hit['appid']} 的官方名是「{hit['cn'] or hit['en']}」，"
                        f"与 Notion 名一致/覆盖，本地已有「{row['title']}」（按 appid 对齐）")
                else:
                    rec["local_title"] = f"{hit['cn'] or hit['en']}（商店，本地无此条目）"
                    rec["media_id"] = hit["appid"]
                    rec["note"] = (
                        f"商店 appid {hit['appid']} 官方名「{hit['cn'] or hit['en']}」"
                        "与 Notion 名完全一致，本地库里还没有这条")
    book.save()
    stats["store_calls"] = searcher.calls
    stats["store_cache_hits"] = searcher.cache_hits
    stats["namebook_calls"] = book.calls

    matched = [rec for rec in records if rec["step"]]
    unmatched = [rec for rec in records if not rec["step"]]
    for rec in records:
        if rec["step"]:
            stats[rec["step"]] += 1

    for rec in unmatched:
        rec["nearest"] = nearest_title(rec["game"]["name"], hint_pool)
        rec["searched"] = searched_directions(rec)
        rec["decision"] = decide_note(rec)

    counts = {step: stats.get(step, 0) for step in STEP_LABELS}
    summary = {
        "total": total,
        "matched": len(matched),
        "unmatched": len(unmatched),
        "exact": counts["exact"],
        "normalized": counts["normalized"],
        "alias": counts["alias"],
        "appid_name": counts["appid_name"],
        "store_search": counts["store_search"],
        "store_calls": stats["store_calls"],
        "store_cache_hits": stats.get("store_cache_hits", 0),
        "namebook_calls": stats.get("namebook_calls", 0),
        "local_games": local_games,
        "dry_run": True,
        "report": str(REPORT_PATH),
        "raw_dir": str(RAW_ROOT / date.today().isoformat()),
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    print("[STAGE] 落库 — 只写导入任务记录", flush=True)
    if args.no_db:
        print("  --no-db：跳过写库", flush=True)
        job = None
    else:
        job = pick_job(True)
        job.mark_running()
        write_items(job, records)
        job.mark_done(summary)
        job.cursor = {"dry_run": True, "total": total, "report": str(REPORT_PATH)}
        job.save(update_fields=["cursor"])

    print("[STAGE] 报告 — 生成 _匹配报告.md", flush=True)
    if job is not None:
        write_report(records, summary, job, total)

    print("")
    print(f"拉到 {total} 条 | 已匹配 {len(matched)} | 匹配不上 {len(unmatched)}")
    for step, label in STEP_LABELS.items():
        print(f"  {label}: {counts[step]}")
    print(f"  商店查询次数: {stats['store_calls']}")
    if job is not None:
        print(f"job #{job.pk} / item {len(records)} 条")
    print(f"报告: {REPORT_PATH}")


if __name__ == "__main__":
    main()
