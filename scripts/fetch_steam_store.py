"""Fetch Steam **store** metadata per appid and keep the raw JSON on disk.

Why this script exists
----------------------
``GetOwnedGames`` only knows the English title, and SteamDB is off limits
(Cloudflare + ToS).  The official store endpoint is the sanctioned source:

    https://store.steampowered.com/api/appdetails?appids=<appid>&l=schinese

It answers with the localized name, the description, the genres, the header
image and the release date.  This script stores the untouched response first
(iron rule: raw before parsing), then parses it, and only ever writes to the
database when ``--apply`` is passed.

Layout of the raw files::

    .tools/raw/steam_store/<YYYY-MM-DD>/<appid>.json            # schinese
    .tools/raw/steam_store/<YYYY-MM-DD>/<appid>.english.json     # english

Rules
-----
* one request at a time, 1.5-2 s apart, never concurrent
* HTTP 429/403 -> stop everything for 5 minutes, then resume;
  three such pauses in a row -> give up and report
* ``{"<appid>": {"success": false}}`` -> record as ``failed``, keep going
* an existing ``<appid>.json`` is skipped unless ``--force``
* ``--dry-run`` is the default; it never touches the database

Usage (from ``yamtrack/``)::

    .venv\\Scripts\\python.exe scripts\\fetch_steam_store.py --appids 1012880,400 --limit 10
    .venv\\Scripts\\python.exe scripts\\fetch_steam_store.py --appids ids.txt --lang both
    .venv\\Scripts\\python.exe scripts\\fetch_steam_store.py --from-db --limit 50
    .venv\\Scripts\\python.exe scripts\\fetch_steam_store.py --from-db --apply   # writes DB
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# Windows 控制台默认 GBK，游戏名里有 ™ 之类的符号会直接炸；统一走 UTF-8
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 老版本/被重定向时忽略
        pass

STORE_URL = "https://store.steampowered.com/api/appdetails"
RAW_ROOT = ROOT / ".tools" / "raw" / "steam_store"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DEFAULT_PROXY = "http://127.0.0.1:7897"
SLEEP_MIN = 1.5
SLEEP_MAX = 2.0
COOLDOWN = 300  # seconds to wait after a 429/403
MAX_COOLDOWNS = 3  # consecutive cooldowns before giving up
REQUEST_TIMEOUT = 30
SOFT_RETRIES = 3  # 5xx / timeouts / connection resets

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #
def has_cjk(text: str | None) -> bool:
    """True when the string holds at least one Chinese character."""
    return bool(text) and CJK.search(text) is not None


def clean_text(raw: str | None, limit: int = 0) -> str:
    """Turn the HTML blurb Steam returns into plain text."""
    if not raw:
        return ""
    text = re.sub(r"<(br|/p|/h\d|/li)\s*/?>", "\n", raw, flags=re.I)
    text = TAG.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    text = WS.sub(" ", text) if limit else text
    return text[:limit] if limit else text


def parse_data(appid: str, data: dict | None) -> dict:
    """Flatten the interesting store fields into a plain dict."""
    if not data:
        return {
            "appid": appid,
            "ok": False,
            "reason": "success=false (下架/地区不可见/接口没有这个 id)",
        }
    genres = [g.get("description") for g in data.get("genres") or [] if g.get("description")]
    categories = [
        c.get("description") for c in data.get("categories") or [] if c.get("description")
    ]
    release = data.get("release_date") or {}
    detail = clean_text(data.get("detailed_description"), limit=600)
    short = clean_text(data.get("short_description"), limit=400)
    name = (data.get("name") or "").strip()
    return {
        "appid": appid,
        "ok": True,
        "type": data.get("type"),
        "name": name,
        "name_has_cjk": has_cjk(name),
        "detailed_description": detail,
        "short_description": short,
        "genres": genres,
        "categories": categories,
        "header_image": data.get("header_image"),
        "capsule_image": data.get("capsule_image"),
        "release_date": release.get("date"),
        "coming_soon": release.get("coming_soon"),
        "is_free": data.get("is_free"),
        "developers": data.get("developers") or [],
        "publishers": data.get("publishers") or [],
        "platforms": data.get("platforms") or {},
        "price": (data.get("price_overview") or {}).get("final_formatted"),
        "screenshots": len(data.get("screenshots") or []),
        "achievements": ((data.get("achievements") or {}).get("total")),
    }


# --------------------------------------------------------------------------- #
# appid sources
# --------------------------------------------------------------------------- #
def read_appids(value: str) -> list[str]:
    """Accept ``1,2,3``, a file with one id per line, or a mix."""
    path = Path(value)
    if path.is_file():
        lines = [
            ln.strip()
            for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
        ]
        tokens = [t for ln in lines if ln and not ln.startswith("#") for t in re.split(r"[,\s]+", ln)]
    else:
        tokens = re.split(r"[,\s]+", value.strip())
    ids: list[str] = []
    for tok in tokens:
        tok = tok.strip()
        if tok.isdigit() and tok not in ids:
            ids.append(tok)
    return ids


def appids_from_db() -> list[str]:
    """Every Steam game already in the library, oldest appid first."""
    sys.path.insert(0, str(SRC))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from app.models import Item  # noqa: PLC0415

    qs = Item.objects.filter(source="steam").only("id", "media_id", "title")
    rows = [(str(i.media_id), i.title) for i in qs if str(i.media_id).isdigit()]
    rows.sort(key=lambda r: int(r[0]))
    return [r[0] for r in rows]


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #
class Fetcher:
    """Single-threaded, rate limited store client with a shared breaker."""

    def __init__(self, proxy: str | None, lang: str) -> None:
        import requests  # noqa: PLC0415

        self.requests = requests
        self.lang = lang
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        )
        self.session.proxies.update({"http": proxy, "https": proxy} if proxy else {})
        self.last_request = 0.0
        self.cooldowns = 0
        self.given_up = False
        self.stats = {"requests": 0, "429": 0, "403": 0, "soft_errors": 0, "cooldowns": 0}

    def _pace(self) -> None:
        wait = random.uniform(SLEEP_MIN, SLEEP_MAX) - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)

    def fetch(self, appid: str, lang: str) -> tuple[int, bytes | None]:
        """Return ``(status, body)``; body is None when the request died."""
        if self.given_up:
            return 0, None
        url = STORE_URL
        params = {"appids": appid, "l": lang, "cc": "cn"}
        for attempt in range(SOFT_RETRIES):
            self._pace()
            self.last_request = time.monotonic()
            self.stats["requests"] += 1
            try:
                resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - transport level, retry
                self.stats["soft_errors"] += 1
                print(f"  ! {appid} 网络错误：{exc!r}（第 {attempt + 1} 次）", flush=True)
                time.sleep(10 * (attempt + 1))
                continue

            code = resp.status_code
            if code in (429, 403):
                self.stats[str(code)] += 1
                self.cooldowns += 1
                self.stats["cooldowns"] += 1
                if self.cooldowns >= MAX_COOLDOWNS:
                    self.given_up = True
                    print(
                        f"  !! {code} 连续 {self.cooldowns} 次，停手不查了（已拿到的都落盘了）",
                        flush=True,
                    )
                    return code, None
                print(f"  ! {code} 被限速，暂停 {COOLDOWN // 60} 分钟再继续", flush=True)
                time.sleep(COOLDOWN)
                continue
            if code >= 500:
                self.stats["soft_errors"] += 1
                print(f"  ! {appid} HTTP {code}，{10 * (attempt + 1)}s 后重试", flush=True)
                time.sleep(10 * (attempt + 1))
                continue
            self.cooldowns = 0
            return code, resp.content
        return 0, None


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def raw_path(day: str, appid: str, lang: str, primary: str) -> Path:
    folder = RAW_ROOT / day
    if lang == primary:
        return folder / f"{appid}.json"
    return folder / f"{appid}.{lang}.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Steam 官方商店 appdetails 抓取（默认只落 JSON，不写库）")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--appids", help="逗号分隔的 appid，或一个每行一个 id 的文件")
    src.add_argument("--from-db", action="store_true", help="从库里取全部 Steam 游戏的 appid")
    ap.add_argument("--limit", type=int, default=0, help="只查前 N 个")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", default=False,
        help="只查、只落 JSON，不写库（不加 --apply 时就是这个行为）",
    )
    mode.add_argument("--apply", action="store_true", help="把结果写进数据库（本任务不用）")
    ap.add_argument("--lang", choices=("schinese", "english", "both"), default="schinese")
    ap.add_argument("--force", action="store_true", help="已有 JSON 也重新抓")
    ap.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or DEFAULT_PROXY)
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--day", default="", help="原始 JSON 的日期子目录，默认今天")
    args = ap.parse_args(argv)

    apply = bool(args.apply)
    if apply:
        print("!! --apply 会把结果写进数据库；本任务请勿使用", flush=True)

    appids = appids_from_db() if args.from_db else read_appids(args.appids)
    if args.limit:
        appids = appids[: args.limit]
    if not appids:
        print("没有 appid 可查，退出", flush=True)
        return 2

    day = args.day or dt.date.today().isoformat()
    (RAW_ROOT / day).mkdir(parents=True, exist_ok=True)
    langs = ["schinese", "english"] if args.lang == "both" else [args.lang]
    primary = "schinese"

    proxy = None if args.no_proxy else args.proxy
    fetcher = Fetcher(proxy, primary)
    print(
        f"[STAGE] 抓取 — {len(appids)} 个 appid / 语言 {','.join(langs)} / "
        f"限速 {SLEEP_MIN}-{SLEEP_MAX}s / 代理 {proxy or '直连'} / 模式 "
        f"{'APPLY(写库)' if apply else 'DRY-RUN(不写库)'}",
        flush=True,
    )
    print(f"原始 JSON → {RAW_ROOT / day}", flush=True)

    results: list[dict] = []
    stopped = ""
    for idx, appid in enumerate(appids, 1):
        head = f"[{idx}/{len(appids)}] {appid}"
        per_lang: dict[str, dict] = {}

        for lang in langs:
            target = raw_path(day, appid, lang, primary)
            if target.exists() and not args.force:
                cached = json.loads(target.read_text(encoding="utf-8"))
                entry = (cached or {}).get(str(appid)) or {}
                parsed = parse_data(appid, entry.get("data") if entry.get("success") else None)
                parsed["lang"] = lang
                parsed["file"] = str(target)
                parsed["cached"] = True
                per_lang[lang] = parsed
                results.append(parsed)
                continue
            code, body = fetcher.fetch(appid, lang)
            if fetcher.given_up:
                stopped = f"连续 {MAX_COOLDOWNS} 次 429/403，停手"
                break
            if body is None:
                results.append(
                    {"appid": appid, "lang": lang, "ok": False, "reason": f"HTTP {code} 请求失败"}
                )
                continue
            # iron rule: the raw bytes hit the disk before anything is parsed
            target.write_bytes(body)
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                results.append(
                    {"appid": appid, "lang": lang, "ok": False, "reason": "JSON 解析失败（原始文件已保留）"}
                )
                continue
            entry = (payload or {}).get(str(appid)) or {}
            parsed = parse_data(appid, entry.get("data") if entry.get("success") else None)
            parsed["lang"] = lang
            parsed["file"] = str(target)
            per_lang[lang] = parsed
            results.append(parsed)

        if stopped:
            break

        if per_lang:
            main_r = per_lang.get(primary) or next(iter(per_lang.values()))
            name_cn = per_lang.get("schinese", {}).get("name")
            name_en = per_lang.get("english", {}).get("name")
            line = f"{head} "
            if not main_r.get("ok"):
                line += f"FAIL {main_r.get('reason')}"
            else:
                line += (
                    f"中文名={name_cn or '(无)'} | 英文名={name_en or '-'} | "
                    f"类型={'/'.join(main_r.get('genres') or []) or '-'} | "
                    f"发行={main_r.get('release_date') or '-'} | "
                    f"简介={(main_r.get('short_description') or main_r.get('detailed_description') or '')[:40]}"
                )
            print(line, flush=True)

    # persist the parsed digest next to the raw files
    out = RAW_ROOT / day / "_results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = [r for r in results if r.get("ok")]
    bad = [r for r in results if not r.get("ok")]

    # one verdict per appid, not per language: failed / has_cn_name / no_cn_name
    by_appid: dict[str, dict] = {}
    for row in results:
        by_appid.setdefault(str(row.get("appid")), {})[row.get("lang", "schinese")] = row
    failed_ids, cn_ids, no_cn_ids, diff_ids = [], [], [], []
    for appid, langs_of in by_appid.items():
        zh = langs_of.get("schinese") or langs_of.get("english")
        en = langs_of.get("english") or {}
        if not zh or not zh.get("ok"):
            failed_ids.append(appid)
            continue
        if zh.get("name_has_cjk"):
            cn_ids.append(appid)
        else:
            no_cn_ids.append(appid)
        if en.get("ok") and (en.get("name") or "").strip() != (zh.get("name") or "").strip():
            diff_ids.append(appid)

    print(
        f"\n[SUMMARY] 请求 {fetcher.stats['requests']} 次 / 成功 {len(ok)} / 失败 {len(bad)} / "
        f"429 {fetcher.stats['429']} / 403 {fetcher.stats['403']}",
        flush=True,
    )
    print(
        f"[按 appid] 有中文名 {len(cn_ids)} 个 / 没中文名（返回还是英文） {len(no_cn_ids)} 个 / "
        f"解析失败 {len(failed_ids)} 个",
        flush=True,
    )
    print(f"  有中文名：{', '.join(cn_ids) or '-'}", flush=True)
    print(f"  没中文名：{', '.join(no_cn_ids) or '-'}", flush=True)
    if failed_ids:
        print(f"  解析失败：{', '.join(failed_ids)}", flush=True)
    if diff_ids:
        print(f"  中英名不同：{', '.join(diff_ids)}", flush=True)
    if bad:
        print("失败清单：" + ", ".join(f"{r['appid']}({r.get('reason', '?')[:20]})" for r in bad[:20]), flush=True)
    if stopped:
        print(f"[STOPPED] {stopped}", flush=True)
    print(f"解析摘要 → {out}", flush=True)

    if apply:
        written = apply_to_db(ok)
        print(f"[APPLY] 更新 {written} 条", flush=True)
    return 0


def apply_to_db(parsed: list[dict]) -> int:
    """Write the parsed values back into the library (only for ``--apply``)."""
    sys.path.insert(0, str(SRC))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from app.models import Item  # noqa: PLC0415

    changed = 0
    for row in parsed:
        item = Item.objects.filter(source="steam", media_id=str(row["appid"])).first()
        if item is None:
            continue
        fields = []
        name = row.get("name") or ""
        if name and has_cjk(name) and not has_cjk(item.title):
            item.title = name
            fields.append("title")
        desc = row.get("short_description") or row.get("detailed_description") or ""
        if desc and not (item.synopsis or "").strip():
            item.synopsis = desc
            fields.append("synopsis")
        if row.get("header_image") and not (item.image or "").strip():
            item.image = row["header_image"]
            fields.append("image")
        if fields:
            item.save(update_fields=fields)
            changed += 1
    return changed


if __name__ == "__main__":
    sys.exit(main())
