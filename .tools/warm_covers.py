"""Pre-download every library cover into the local disk cache.

The relay (``src/app/image_relay.py``) keeps each cover twice: Redis for 30 days
and, durably, on disk at ``.tools/raw/covers/<sha256[:2]>/<sha256>.<ext>``.
A cold cover costs 1.8~3.5 s from the source; a warm one costs 0~2 ms. The
first visitor of a list page therefore pays for every unseen poster.

This script walks ``app_item.image`` ahead of time and fills that disk cache
through the very same ``image_relay.fetch_image`` the view calls, so cache keys
and disk layout match exactly. It never writes to the database.

Usage:

    .venv\\Scripts\\python.exe .tools\\warm_covers.py --dry-run
    .venv\\Scripts\\python.exe .tools\\warm_covers.py --limit 40 --workers 4
    .venv\\Scripts\\python.exe .tools\\warm_covers.py --workers 4
    .venv\\Scripts\\python.exe .tools\\warm_covers.py --media-type movie,tv

Already-cached images are skipped, so re-running resumes where it stopped.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Windows consoles here default to GBK and blow up on the arrows/Chinese text.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

import requests  # noqa: E402
from django.conf import settings  # noqa: E402

from app import image_relay  # noqa: E402
from app.models import Item  # noqa: E402

MEDIA_TYPES = ("movie", "tv", "book", "game")
FAILED_LOG = REPO_ROOT / ".tools" / "logs" / "warm_covers_failed.json"
MAX_WORKERS = 8
DIAG_TIMEOUT = 15


def original_url(image):
    """Return the upstream URL behind a stored ``app_item.image`` value.

    Films, series and books already store the local relay path
    ``/image_relay?url=<urlencoded>``; games store the raw CDN URL.
    """
    if not image:
        return ""
    if image.startswith("/image_relay"):
        values = parse_qs(urlparse(image).query).get("url") or [""]
        return values[0]
    return image


def dir_size_bytes(path):
    """Return the total size of every file under ``path`` (0 when missing)."""
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
            elif entry.is_dir(follow_symlinks=False):
                total += dir_size_bytes(entry.path)
    except OSError:
        pass
    return total


def collect(media_types):
    """Return ``(per_type_stats, pending)`` for the requested media types.

    ``per_type_stats`` mirrors the audit numbers (items, cacheable, on disk,
    missing); ``pending`` holds one ``(url, media_type)`` per distinct URL that
    still needs a download.
    """
    stats = {mt: Counter() for mt in media_types}
    seen = {}
    pending = []
    for media_type, image in (
        Item.objects.filter(media_type__in=media_types)
        .exclude(image=None)
        .exclude(image="")
        .values_list("media_type", "image")
    ):
        stat = stats[media_type]
        stat["items"] += 1
        url = original_url(image)
        if not url or url.startswith("data:") or url == settings.IMG_NONE:
            stat["plain"] += 1
            continue
        if not image_relay.should_cache(url):
            stat["plain"] += 1
            continue
        stat["cacheable"] += 1
        digest = image_relay.image_digest(url)
        if image_relay._read_from_disk(digest) is not None:  # noqa: SLF001
            stat["on_disk"] += 1
            continue
        stat["missing"] += 1
        if url not in seen:
            seen[url] = media_type
            pending.append((url, media_type))
    return stats, pending


def print_stats(stats, hosts):
    print("\n=== 封面落盘率（当前磁盘状态）===")
    print(
        f"{'类型':<6}{'条目':>7}{'走缓存':>8}{'已落盘':>8}{'未落盘':>8}"
        f"{'落盘率':>9}{'不走缓存':>9}"
    )
    for media_type in MEDIA_TYPES:
        stat = stats.get(media_type)
        if stat is None:
            continue
        total = stat["on_disk"] + stat["missing"]
        pct = (stat["on_disk"] / total * 100) if total else 0.0
        print(
            f"{media_type:<6}{stat['items']:>7}{stat['cacheable']:>8}"
            f"{stat['on_disk']:>8}{stat['missing']:>8}{pct:>8.1f}%{stat['plain']:>9}"
        )
    print("\n=== 待下载 URL 按域名 ===")
    for host, count in hosts.most_common():
        proxy_only = " (代理专用)" if host in image_relay.PROXY_ONLY_HOSTS else ""
        print(f"  {host:<42}{count:>6}{proxy_only}")


def classify(url):
    """Probe a failed URL once and return a short human reason."""
    headers = {"User-Agent": image_relay.USER_AGENT}
    referer = image_relay.referer_for(url)
    if referer:
        headers["Referer"] = referer
    proxy = image_relay._relay_proxies()  # noqa: SLF001
    if not proxy and image_relay._proxy_only(url):  # noqa: SLF001
        return "其它", "代理未监听，该域名只走代理"
    try:
        response = requests.get(
            url, headers=headers, timeout=DIAG_TIMEOUT, proxies=proxy or {}
        )
    except requests.Timeout:
        return "超时", "timeout"
    except requests.RequestException as error:
        return "其它", f"{type(error).__name__}: {str(error)[:120]}"
    except ValueError as error:
        return "其它", f"ValueError: {str(error)[:120]}"
    content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
    status = response.status_code
    if status in (404, 410):
        return "404", f"HTTP {status}"
    if status >= 400:
        return "其它", f"HTTP {status} {content_type}"
    if not content_type.startswith("image/"):
        return "非图片", f"HTTP {status} {content_type or '无类型'} {len(response.content)}B"
    if len(response.content) > image_relay.MAX_IMAGE_BYTES:
        return "其它", f"过大 {len(response.content)}B"
    return "其它", f"HTTP {status} {content_type} 但抓取失败"


def fetch_with_retry(url):
    """Fetch one cover, retrying twice; return ``(ok, reason_category, detail)``."""
    delays = (1, 3)
    last = None
    for attempt in range(len(delays) + 1):
        if attempt:
            time.sleep(delays[attempt - 1])
        try:
            content, content_type = image_relay.fetch_image(url)
        except Exception as error:  # noqa: BLE001 - one bad URL must not kill the run
            last = f"{type(error).__name__}: {error}"
            continue
        if content is not None:
            return True, "", ""
        last = last or "fetch_image 返回 None"
        # The disk write happened inside fetch_image, so check it directly too:
        # a Redis hiccup could still raise after the bytes reached the disk.
        if image_relay._read_from_disk(image_relay.image_digest(url)) is not None:  # noqa: SLF001
            return True, "", ""
    category, detail = classify(url)
    return False, category, f"{detail} (最后一次: {last})"


def media_type_of(url, pending_map):
    return pending_map.get(url, "?")


def main():
    parser = argparse.ArgumentParser(description="预热封面磁盘缓存（只读数据库）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不下载")
    parser.add_argument("--limit", type=int, default=0, help="最多下载 N 张（0 = 全量）")
    parser.add_argument(
        "--media-type",
        default=",".join(MEDIA_TYPES),
        help="逗号分隔：movie,tv,book,game（默认全部）",
    )
    parser.add_argument("--workers", type=int, default=4, help="并发数（默认 4，上限 8）")
    args = parser.parse_args()

    wanted = [t.strip() for t in args.media_type.split(",") if t.strip()]
    unknown = [t for t in wanted if t not in MEDIA_TYPES]
    if unknown:
        print(f"[warm] 未知类型: {unknown}，可选 {list(MEDIA_TYPES)}")
        return 2
    workers = max(1, min(MAX_WORKERS, args.workers))

    started = time.time()
    stats, pending = collect(wanted)
    hosts = Counter((urlparse(url).hostname or "?") for url, _ in pending)

    if args.limit and args.limit > 0:
        pending = pending[: args.limit]

    print(
        f"[warm] 类型={wanted} 待下载(去重后)={len(pending)} "
        f"并发={workers} 代理={'有' if image_relay._relay_proxies() else '无(直连)'} "
        f"耗时={time.time() - started:.1f}s"
    )
    if args.dry_run:
        print_stats(stats, hosts)
        total_missing = sum(s["missing"] for s in stats.values())
        print(f"\n[warm] dry-run 结束：条目级未落盘合计 {total_missing} 张，去重后 {len(pending)} 张")
        return 0
    if not pending:
        print("[warm] 没有需要下载的封面")
        return 0

    pending_map = dict((url, mt) for url, mt in pending)
    size_before = dir_size_bytes(image_relay.IMAGE_DIR)

    done = ok = fail = 0
    bytes_map = {}
    failures = []
    lock = threading.Lock()
    run_started = time.time()

    def work(item):
        url, media_type = item
        success, category, detail = fetch_with_retry(url)
        return url, media_type, success, category, detail

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(work, item) for item in pending]
            for future in as_completed(futures):
                url, media_type, success, category, detail = future.result()
                with lock:
                    done += 1
                    if success:
                        ok += 1
                        bytes_map[url] = True
                    else:
                        fail += 1
                        failures.append(
                            {
                                "url": url,
                                "media_type": media_type,
                                "category": category,
                                "detail": detail,
                            }
                        )
                    if done % 20 == 0 or done == len(pending):
                        elapsed = time.time() - run_started
                        rate = (done / elapsed) if elapsed else 0
                        eta = ((len(pending) - done) / rate) if rate else 0
                        print(
                            f"[warm] 已处理 {done}/{len(pending)}，成功 {ok}，失败 {fail}，"
                            f"耗时 {elapsed:.0f} 秒（{rate:.2f} 张/秒，预计还需 {eta:.0f} 秒）",
                            flush=True,
                        )
    except KeyboardInterrupt:
        print("\n[warm] 被中断，已完成的图片都已落盘，重跑会自动跳过", flush=True)

    elapsed = time.time() - run_started
    size_after = dir_size_bytes(image_relay.IMAGE_DIR)
    delta_mb = (size_after - size_before) / 1_048_576
    avg = (elapsed / done) if done else 0

    print("\n=== 汇总 ===")
    print(f"总目标      : {len(pending)}")
    print(f"成功        : {ok}")
    print(f"失败        : {fail}")
    print(f"跳过(起始已落盘): {sum(s['on_disk'] for s in stats.values())} 条目")
    print(f"总耗时      : {elapsed:.0f} 秒")
    print(f"平均每张    : {avg:.2f} 秒（{workers} 并发）")
    print(f"磁盘增量    : {delta_mb:+.1f} MB（现共 {size_after / 1_048_576:.1f} MB）")

    if failures:
        by_category = Counter(f["category"] for f in failures)
        by_media = Counter(f["media_type"] for f in failures)
        print("\n=== 失败分类 ===")
        for category, count in by_category.most_common():
            print(f"  {category:<8}{count:>5}")
        print("  按类型: " + "，".join(f"{k}={v}" for k, v in by_media.most_common()))
        by_host = Counter((urlparse(f["url"]).hostname or "?") for f in failures)
        print("  按域名: " + "，".join(f"{k}={v}" for k, v in by_host.most_common()))
        FAILED_LOG.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if FAILED_LOG.exists():
            try:
                existing = json.loads(FAILED_LOG.read_text(encoding="utf-8")).get(
                    "failures", []
                )
            except (OSError, json.JSONDecodeError):
                existing = []
        merged = {f["url"]: f for f in existing}
        merged.update({f["url"]: f for f in failures})
        FAILED_LOG.write_text(
            json.dumps(
                {
                    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "total": len(merged),
                    "failures": list(merged.values()),
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"  失败清单: {FAILED_LOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
