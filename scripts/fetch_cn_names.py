"""Backfill the Chinese titles of the imported Steam games.

``GetOwnedGames`` has no language switch, so every imported game was stored
with its English title.  The public store API *does* know the localized name;
this script asks it and rewrites ``app_item.title`` only when the answer is a
real Chinese name.

Two ways to ask Steam are implemented:

``batch`` (default)
    ``IStoreBrowseService/GetItems``: up to 200 appids in one GET, ~0.5s per
    call.  The whole library is done in about half a minute.
``single`` (``--single``)
    ``store.steampowered.com/api/appdetails``: one appid per request.  This is
    the documented endpoint, but it throttles hard (HTTP 429 with a ``null``
    body) after a few dozen calls, hence the pacing / backoff / thread pool.

Both return the same names; ``batch`` is only a different transport.

Rules
-----
* the appid is not sold any more (``success`` != 1)   -> keep the title.
* the returned name has no Chinese character           -> keep the title.
* the returned name is identical in ``english``        -> keep the title
  (the game simply has no Chinese localization).
* the returned name equals the current one             -> nothing to do.
* a title that already contains Chinese is never touched, it is skipped
  before any request is made.

Usage (from the repository root)::

    .venv/Scripts/python.exe scripts/fetch_cn_names.py --limit 20 --dry-run
    .venv/Scripts/python.exe scripts/fetch_cn_names.py              # full run
    .venv/Scripts/python.exe scripts/fetch_cn_names.py --status     # progress only
    .venv/Scripts/python.exe scripts/fetch_cn_names.py --single     # per appid

The set of already processed appids lives in ``.tools/cn_names_progress.json``
so an interrupted run resumes where it stopped; network failures are retried
on the next run (``--retry-failed``).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import signal
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import requests  # noqa: E402

import django  # noqa: E402

django.setup()

from app.models import Item  # noqa: E402

STORE_URL = "https://store.steampowered.com/api/appdetails"
BROWSE_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
PROGRESS_PATH = ROOT / ".tools" / "cn_names_progress.json"
BATCH_SIZE = 100
CHUNK_SIZE = 150
REQUEST_TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# CJK unified ideographs: the only thing that makes a name "Chinese enough"
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

STATUS_LABEL = {
    "updated": "更新",
    "no_cn": "无中文名",
    "same": "同名",
    "dead": "已下架",
    "failed": "失败",
}

_local = threading.local()


def session() -> requests.Session:
    """Return the requests session of the current worker thread."""
    sess = getattr(_local, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update(
            {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
        )
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _local.session = sess
    return sess


def has_cjk(text: str | None) -> bool:
    """Return True when the string holds at least one Chinese character."""
    return bool(text) and CJK.search(text) is not None


class Pacer:
    """Global request pacing plus a shared circuit breaker.

    Every worker asks for a slot; slots are spaced by ``interval`` so the whole
    run stays at a few requests per second instead of ``workers`` times that.
    A 429 makes every worker wait, not just the one that got it.
    """

    def __init__(self, interval: float, jitter: float) -> None:
        self.interval = interval
        self.jitter = jitter
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.pause_until = 0.0

    def wait(self) -> None:
        """Block until this worker is allowed to fire its request."""
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_at, self.pause_until)
            self.next_at = start + self.interval + random.uniform(0, self.jitter)
            delay = start - now
        if delay > 0:
            time.sleep(delay)

    def pause(self, seconds: float) -> None:
        """Make every worker hold off for ``seconds``."""
        with self.lock:
            self.pause_until = max(self.pause_until, time.monotonic() + seconds)


class State:
    """Shared counters and the stop flag of the run."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.failure_streak = 0
        self.pauses = 0
        self.stop = False


def decide(current: str, cn_name: str | None, en_name: str | None) -> tuple[str, str]:
    """Return the status and the (possibly unchanged) title of one appid."""
    if not cn_name:
        return "dead", current
    new = cn_name.strip()
    if not has_cjk(new):
        return "no_cn", current
    if en_name and new == en_name.strip():
        # Same string in English: the store has no Chinese localization at all
        return "no_cn", current
    if new == (current or "").strip():
        return "same", current
    return "updated", new


def browse_chunk(appids: list[str], language: str, pacer: Pacer, state: State) -> dict:
    """Ask the store browser for the names of up to ``CHUNK_SIZE`` appids."""
    if not appids:
        return {}
    payload = {
        "ids": [{"appid": int(appid)} for appid in appids],
        "context": {"language": language, "country_code": "CN", "steam_realm": 1},
        "data_request": {"include_basic_info": True},
    }
    last_error = ""
    for attempt in range(4):
        pacer.wait()
        try:
            response = session().get(
                BROWSE_URL,
                params={"input_json": json.dumps(payload, separators=(",", ":"))},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            response = None
            last_error = f"网络({type(exc).__name__})"
        if response is not None:
            if response.status_code == 200:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                items = ((body or {}).get("response") or {}).get("store_items")
                if isinstance(items, list):
                    with state.lock:
                        state.failure_streak = 0
                    return {
                        str(entry.get("id")): (
                            entry.get("success") == 1,
                            entry.get("name") or "",
                        )
                        for entry in items
                        if entry.get("id") is not None
                    }
                last_error = "响应无法解析"
            elif response.status_code in (403, 429, 503, 414):
                last_error = f"限流(HTTP {response.status_code})"
            else:
                last_error = f"HTTP {response.status_code}"

        delay = (5, 10, 20)[min(attempt, 2)]
        if "限流" in last_error:
            pacer.pause(delay)
            with state.lock:
                state.pauses += 1
                pauses = state.pauses
            print(f"  ! 批量 {len(appids)} 个 {last_error}，暂停 {delay}s（第 {pauses} 次）", flush=True)
        with state.lock:
            state.failure_streak += 1
            streak = state.failure_streak
        if streak >= 8:
            with state.lock:
                state.failure_streak = 0
                state.pauses += 1
                too_many = state.pauses >= 6
            if too_many:
                print("  !! Steam 连续限流，停止发新请求，稍后用同一命令续跑", flush=True)
                state.stop = True
                return {}
            pacer.pause(60)
            print("  !! 连续失败，暂停 60s", flush=True)
        time.sleep(min(2.0 * (attempt + 1), 6.0))
    print(f"  !! 批量 {len(appids)} 个请求失败：{last_error}", flush=True)
    return {}


def browse_names(appids: list[str], pacer: Pacer, state: State) -> dict[str, dict]:
    """Return ``{appid: {cn, en}}`` for the appids, chunked and resumed."""
    result: dict[str, dict] = {}
    pending = list(appids)
    while pending and not state.stop:
        chunk = pending[:CHUNK_SIZE]
        pending = pending[CHUNK_SIZE:]
        cn = browse_chunk(chunk, "schinese", pacer, state)
        if not cn:
            break
        en = browse_chunk([a for a in chunk if cn.get(a, (False, ""))[0]], "english", pacer, state)
        for appid in chunk:
            ok, name = cn.get(appid, (False, ""))
            if not ok:
                result[appid] = {"cn": None, "en": None}
                continue
            result[appid] = {"cn": name, "en": (en.get(appid) or (True, ""))[1] or name}
    return result


def fetch_single(appid: str, current: str, pacer: Pacer, state: State) -> dict:
    """Ask the store for one appid (the documented, throttled endpoint)."""
    params = {"appids": appid, "l": "schinese", "cc": "cn", "filters": "basic"}
    last_error = ""
    for attempt in range(4):
        pacer.wait()
        try:
            response = session().get(STORE_URL, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            response = None
            last_error = f"网络({type(exc).__name__})"
        if response is not None:
            if response.status_code == 200:
                try:
                    payload = json.loads(response.content.decode("utf-8", "replace"))
                except ValueError:
                    payload = None
                entry = (payload or {}).get(str(appid)) if isinstance(payload, dict) else None
                if isinstance(entry, dict) and entry.get("success"):
                    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
                    with state.lock:
                        state.failure_streak = 0
                    return {"appid": appid, "cn": data.get("name"), "en": None}
                if isinstance(entry, dict):
                    with state.lock:
                        state.failure_streak = 0
                    return {"appid": appid, "cn": None, "en": None}
                last_error = "响应无法解析"
            elif response.status_code in (403, 429, 503):
                last_error = f"限流(HTTP {response.status_code})"
            else:
                last_error = f"HTTP {response.status_code}"

        delay = (5, 10, 20)[min(attempt, 2)]
        if "限流" in last_error:
            pacer.pause(delay)
            with state.lock:
                state.pauses += 1
                pauses = state.pauses
            print(f"  ! {appid} {last_error}，全局暂停 {delay}s（第 {pauses} 次）", flush=True)
        with state.lock:
            state.failure_streak += 1
            streak = state.failure_streak
        if streak >= 12:
            with state.lock:
                state.failure_streak = 0
                state.pauses += 1
                too_many = state.pauses >= 5
            if too_many:
                print("  !! 连续失败过多，停止发新请求", flush=True)
                state.stop = True
                return {"appid": appid, "error": last_error}
            pacer.pause(60)
            print(f"  !! 连续失败 {streak} 次，暂停 60s", flush=True)
        time.sleep(min(2.0 * (attempt + 1), 6.0))
    return {"appid": appid, "error": last_error}


def load_progress(path: Path) -> dict:
    """Read the resume file, tolerating a missing or broken one."""
    if not path.exists():
        return {"version": 1, "done": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"progress file unreadable, starting fresh: {path}", flush=True)
        return {"version": 1, "done": {}}
    if not isinstance(data.get("done"), dict):
        data["done"] = {}
    return data


def save_progress(path: Path, done: dict[str, str]) -> None:
    """Atomically persist the resume file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "done": done,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def steam_items() -> list[Item]:
    """Return every imported Steam game, oldest appid first."""
    items = list(Item.objects.filter(source="steam").only("id", "media_id", "title"))

    def sort_key(item: Item):
        return (
            (0, int(item.media_id), "")
            if str(item.media_id).isdigit()
            else (1, 0, str(item.media_id))
        )

    return sorted(items, key=sort_key)


def summarize(done: dict[str, str]) -> dict[str, int]:
    """Count the statuses stored in the resume file."""
    stats = {key: 0 for key in STATUS_LABEL}
    for status in done.values():
        stats[status] = stats.get(status, 0) + 1
    return stats


def fmt_eta(seconds: float) -> str:
    """Render a duration the short way."""
    if seconds <= 0 or seconds > 6 * 3600:
        return "?"
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.1f}min"


def show_status(path: Path) -> int:
    """Print the content of the resume file and exit."""
    data = load_progress(path)
    stats = summarize(data["done"])
    print(f"progress file: {path}")
    print(f"processed: {len(data['done'])}")
    for key, label in STATUS_LABEL.items():
        print(f"  {label:<6}{stats.get(key, 0)}")
    print(f"last write: {data.get('updated_at', '-')}")
    return 0


def main() -> int:
    """Run the backfill."""
    parser = argparse.ArgumentParser(description="Backfill Chinese titles of Steam games")
    parser.add_argument("--limit", type=int, default=0, help="only handle N games")
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        help="handle N games drawn at random instead of the first N",
    )
    parser.add_argument("--seed", type=int, default=42, help="seed of the --sample draw")
    parser.add_argument("--dry-run", action="store_true", help="decide only, never write")
    parser.add_argument("--single", action="store_true", help="ask appdetails per appid")
    parser.add_argument("--workers", type=int, default=5, help="parallel requests in --single")
    parser.add_argument("--interval", type=float, default=0.15, help="min seconds between requests")
    parser.add_argument("--jitter", type=float, default=0.15, help="extra random delay per request")
    parser.add_argument("--progress", type=Path, default=PROGRESS_PATH, help="resume file")
    parser.add_argument("--reset-progress", action="store_true", help="forget the resume file")
    parser.add_argument("--status", action="store_true", help="print progress and exit")
    parser.add_argument("--retry-failed", action="store_true", help="re-queue the failed appids")
    parser.add_argument("--quiet", action="store_true", help="no per game lines")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    progress_path: Path = args.progress
    if args.status:
        return show_status(progress_path)
    if args.reset_progress and progress_path.exists():
        progress_path.unlink()
        print(f"progress reset: {progress_path}", flush=True)

    items = steam_items()
    if not items:
        print("no Steam items found, nothing to do")
        return 0

    progress = load_progress(progress_path)
    done: dict[str, str] = dict(progress["done"])
    if args.retry_failed:
        done = {k: v for k, v in done.items() if v != "failed"}
    if done:
        seen = summarize(done)
        print(
            "resume: " + " ".join(f"{STATUS_LABEL[k]}={seen.get(k, 0)}" for k in STATUS_LABEL),
            flush=True,
        )

    already_cn = [item for item in items if has_cjk(item.title)]
    finished = {"updated", "no_cn", "same", "dead"}
    pending = [
        item
        for item in items
        if not has_cjk(item.title) and done.get(str(item.media_id)) not in finished
    ]
    if args.sample:
        random.seed(args.seed)
        pending = random.sample(pending, min(args.sample, len(pending)))
    elif args.limit:
        pending = pending[: args.limit]

    mode = "single" if args.single else "batch"
    print(
        f"steam games={len(items)} with_chinese={len(already_cn)} "
        f"already_processed={len(done)} todo={len(pending)} "
        f"mode={mode} dry_run={args.dry_run}",
        flush=True,
    )
    if not pending:
        print("nothing left to do")
        return 0

    pacer = Pacer(max(0.0, args.interval), max(0.0, args.jitter))
    state = State()
    total = len(pending)
    stats = {"updated": 0, "no_cn": 0, "same": 0, "dead": 0, "failed": 0}
    changed: list[Item] = []
    written = 0
    processed = 0
    started = time.time()
    last_print = 0.0

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    def flush_progress(force: bool = False) -> None:
        if args.dry_run:
            return
        if force or processed % 25 == 0:
            save_progress(progress_path, done)

    def write_db(force: bool = False) -> None:
        nonlocal written
        if args.dry_run or not changed:
            return
        if force or len(changed) >= BATCH_SIZE:
            Item.objects.bulk_update(list(changed), ["title"])
            written += len(changed)
            changed.clear()

    def report_hit(status: str, old: str, new: str) -> None:
        if args.quiet:
            return
        label = STATUS_LABEL.get(status, status)
        if status == "updated":
            print(f"  -> {old[:60]}  =>  {new}", flush=True)
        else:
            print(f"     {old[:60]}  ({label})", flush=True)

    def apply(appid: str, current: str, decision: tuple[str, str], item: Item) -> None:
        nonlocal processed
        status, new_title = decision
        stats[status] = stats.get(status, 0) + 1
        done[appid] = status
        if status == "updated":
            item.title = new_title
            changed.append(item)
            write_db()
        report_hit(status, current, new_title)
        processed += 1

    def progress_line() -> str:
        elapsed = time.time() - started
        rate = processed / elapsed if elapsed > 0 else 0
        eta = (total - processed) / rate if rate > 0 else 0
        pct = 100 * processed / total
        skipped = stats["no_cn"] + stats["same"] + stats["dead"]
        return (
            f"[{processed}/{total}] {pct:.0f}% | 更新 {stats['updated']} | "
            f"跳过 {skipped} | 失败 {stats['failed']} | {rate:.1f}/s | ETA {fmt_eta(eta)}"
        )

    def tick() -> None:
        nonlocal last_print
        flush_progress()
        now = time.time()
        if processed % 20 == 0 or now - last_print > 5:
            last_print = now
            print(progress_line(), flush=True)

    def on_signal(signum, frame):  # noqa: ARG001
        state.stop = True
        print("  ... 收到中断，保存进度后退出", flush=True)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        if mode == "batch":
            remaining = [str(item.media_id) for item in pending]
            by_appid = {str(item.media_id): item for item in pending}
            while remaining and not state.stop:
                block = remaining[:CHUNK_SIZE]
                remaining = remaining[CHUNK_SIZE:]
                names = browse_names(block, pacer, state)
                for appid in block:
                    if state.stop:
                        break
                    item = by_appid[appid]
                    info = names.get(appid)
                    if info is None:
                        # not answered: leave it for the next run
                        stats["failed"] += 1
                        done[appid] = "failed"
                        processed += 1
                        continue
                    apply(appid, item.title, decide(item.title, info["cn"], info["en"]), item)
                tick()
                print(progress_line(), flush=True)
        else:
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                it = iter(pending)
                futures: dict = {}

                def fill() -> None:
                    while len(futures) < max(1, args.workers) * 2 and not state.stop:
                        item = next(it, None)
                        if item is None:
                            return
                        futures[
                            pool.submit(fetch_single, str(item.media_id), item.title, pacer, state)
                        ] = item

                fill()
                while futures:
                    finished, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in finished:
                        item = futures.pop(future)
                        try:
                            result = future.result()
                        except Exception as exc:  # noqa: BLE001 - never lose the run
                            result = {"appid": str(item.media_id), "error": repr(exc)}
                        appid = str(item.media_id)
                        if not result.get("cn") and not result.get("error"):
                            decision = ("dead", item.title)
                        elif result.get("error"):
                            stats["failed"] += 1
                            done[appid] = "failed"
                            processed += 1
                            decision = None
                        else:
                            decision = decide(item.title, result.get("cn"), result.get("en"))
                        if decision is not None:
                            apply(appid, item.title, decision, item)
                        # keep a deleted/vanished title from being reported twice
                    flush_progress()
                    if not state.stop:
                        fill()
                    tick()
    finally:
        write_db(force=True)
        flush_progress(force=True)

    if not args.dry_run and changed:
        Item.objects.bulk_update(list(changed), ["title"])
        written += len(changed)
        changed.clear()

    elapsed = time.time() - started
    final = summarize(done)
    skipped = stats["no_cn"] + stats["same"] + stats["dead"]
    print("", flush=True)
    print(
        f"done in {fmt_eta(elapsed)} | checked {processed}/{total} | "
        f"updated {stats['updated']} | skipped {skipped} | failed {stats['failed']} | "
        f"written_to_db {written if not args.dry_run else 0}",
        flush=True,
    )
    print(
        "overall: " + " ".join(f"{STATUS_LABEL[k]}={final.get(k, 0)}" for k in STATUS_LABEL),
        flush=True,
    )
    if final.get("failed"):
        print(f"note: {final['failed']} appids failed, rerun with --retry-failed", flush=True)
    if args.dry_run:
        print("dry run: nothing was written", flush=True)
    else:
        print(f"progress saved to {progress_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
