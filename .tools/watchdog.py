"""Keep the Steam Tracker site alive.

Why it exists
-------------
The Django process has died silently at least once (port 8001 gone, every log
file clean), and because everything runs under pythonw there is no window and
no error to notice.

Why it probes HTTP instead of the port (2026-10-22)
---------------------------------------------------
A TCP probe only proves something is listening. Django can happily keep the
socket open while every view raises: on 2026-10-22 ``/journal?start-date=all``
and ``/statistics?start-date=all`` returned HTTP 500 for hours and the old
port-only watchdog never noticed. So each run now sends real GET requests and
only HTTP 200 with a non-empty body counts as healthy.

Why three failures before a restart
-----------------------------------
A single hiccup (slow page, transient 500) must not restart Django in the
middle of a Celery import. The counter lives in ``.tools\\watchdog_fails`` so
it survives between runs; one healthy check resets it to 0.

Why it goes through the scheduled task
-------------------------------------
Starting the stack directly from here inherits the caller's Windows Job
object. Anything running with a "kill everything when I close" job (the
desktop AI tool this project gets edited from is one) would then tear the site
down with it the moment it exits. That is exactly what happened twice on
2026-10-21: clean logs, no traceback, process simply gone. ``schtasks /run``
hands the work to the task scheduler, which sits outside any such job.
There is deliberately NO direct-Popen fallback any more.

Safety rules
------------
* ``.tools\\stopped.flag`` wins. stop_silent.py writes it, start_silent.py
  deletes it, so a deliberate shutdown is never fought with.
* start_silent.py is idempotent, so a false alarm cannot start a second copy.
* Every run writes exactly one heartbeat line to ``.tools\\logs\\watchdog.log``,
  so "the watchdog ran" and "when did it go bad" are both provable afterwards.
  The log is rotated at 8 MB, same rule start_silent.py uses.

Run by the "SteamTracker Watchdog" scheduled task every five minutes:

    .venv\\Scripts\\python.exe .tools\\watchdog.py

Safe to run by hand. For a side-effect-free test use

    .venv\\Scripts\\python.exe .tools\\watchdog.py --no-restart --base-url http://127.0.0.1:18099
"""

from __future__ import annotations

import argparse
import http.client
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / ".tools"
LOGS = TOOLS / "logs"
FLAG = TOOLS / "stopped.flag"
FAILS = TOOLS / "watchdog_fails"
STACK_TASK = "SteamTracker Start"

DEFAULT_BASE_URL = "http://127.0.0.1:8001"
PROBE_PATHS = ("/", "/journal", "/statistics")
PROBE_TIMEOUT = 10  # 秒，单次请求
FAIL_THRESHOLD = 3  # 连续几次不健康才重启
RECOVER_WAIT = 90  # 请求启动后最多等多久看它回来

LOG_MAX_BYTES = 8 * 1024 * 1024  # 和 start_silent.py 一样的归档阈值
HIDE = 0x08000000  # CREATE_NO_WINDOW，别弹黑框


def log(message: str) -> None:
    """Append one line to the watchdog log, rotating it at 8 MB first."""
    LOGS.mkdir(parents=True, exist_ok=True)
    path = LOGS / "watchdog.log"
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path.rename(path.with_name(f"{path.stem}-{stamp}{path.suffix}"))
    except OSError:
        pass  # 归档失败不能挡住写日志
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def probe(host: str, port: int, path: str, secure: bool = False) -> tuple[bool, str]:
    """发一次真 HTTP GET，只有 200 且有正文才算这一项健康。"""
    started = time.perf_counter()
    try:
        conn_class = http.client.HTTPSConnection if secure else http.client.HTTPConnection
        conn = conn_class(host, port, timeout=PROBE_TIMEOUT)
        try:
            conn.request(
                "GET",
                path,
                headers={"User-Agent": "SteamTracker-Watchdog", "Connection": "close"},
            )
            response = conn.getresponse()
            status = response.status
            body = response.read()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - 探活就是要吞掉一切异常只记账
        spent = time.perf_counter() - started
        return False, f"{path} ERR {type(exc).__name__}:{str(exc)[:60]} {spent:.2f}s"
    spent = time.perf_counter() - started
    ok = status == 200 and len(body) > 0
    return ok, f"{path} {status} {len(body)}B {spent:.2f}s"


def read_fails() -> int:
    """读连续失败计数；文件坏了/不存在就当 0。"""
    try:
        return max(0, int(FAILS.read_text(encoding="utf-8").strip() or "0"))
    except (OSError, ValueError):
        return 0


def write_fails(value: int) -> None:
    """把连续失败计数写盘，跨多次运行保留；归零时直接删掉文件。"""
    try:
        if int(value) <= 0:
            FAILS.unlink(missing_ok=True)
            return
        TOOLS.mkdir(parents=True, exist_ok=True)
        FAILS.write_text(str(int(value)), encoding="utf-8")
    except OSError as exc:
        log(f"WARN cannot write fail counter {FAILS.name}: {exc}")


def start_via_task() -> bool:
    """Ask the task scheduler to launch the stack, outside any inherited job."""
    try:
        result = subprocess.run(
            ["schtasks", "/run", "/tn", STACK_TASK],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=HIDE,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"ERROR schtasks /run \"{STACK_TASK}\" failed: {exc}")
        return False
    if result.returncode == 0:
        return True
    output = (result.stderr or result.stdout or "").strip()[:200]
    log(f"ERROR schtasks /run \"{STACK_TASK}\" exited {result.returncode}: {output}")
    return False


def wait_for_recovery(host: str, port: int, secure: bool = False) -> tuple[bool, float]:
    """轮询首页，等站点自己起来。返回 (是否恢复, 等了多久)。"""
    started = time.perf_counter()
    while time.perf_counter() - started < RECOVER_WAIT:
        ok, _ = probe(host, port, "/", secure=secure)
        if ok:
            return True, time.perf_counter() - started
        time.sleep(3)
    return False, time.perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Steam Tracker watchdog")
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="只探测不重启，人工验证用（计划任务永远不带这个参数）",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"探活地址，默认 {DEFAULT_BASE_URL}（人工验证用）",
    )
    args = parser.parse_args(argv)

    split = urlsplit(args.base_url)
    host = split.hostname or "127.0.0.1"
    secure = split.scheme == "https"
    port = split.port or (443 if secure else 80)

    fails = read_fails()

    if FLAG.exists():
        # 用户手动停了站点，不许抢着启动，也别把失败记到他头上。
        write_fails(0)
        log(f"SKIP stopped.flag present -> site deliberately stopped, no probe, no start (fails reset 0, was {fails})")
        return 0

    results = [probe(host, port, path, secure=secure) for path in PROBE_PATHS]
    healthy = all(ok for ok, _ in results)
    detail = " | ".join(text for _, text in results)

    if healthy:
        write_fails(0)  # 归零（文件不存在就不写，健康时目录里保持干净）
        log(f"HEALTHY fails=0/{FAIL_THRESHOLD} | {detail}")
        return 0

    fails += 1
    write_fails(fails)
    log(f"UNHEALTHY fails={fails}/{FAIL_THRESHOLD} | {detail}")

    if fails < FAIL_THRESHOLD:
        return 1

    if args.no_restart:
        log(
            f"fails={fails}/{FAIL_THRESHOLD} reached, but --no-restart was given "
            f"-> NOT asking the scheduler to start the stack",
        )
        return 1

    log(f'{FAIL_THRESHOLD} consecutive unhealthy checks -> schtasks /run "{STACK_TASK}"')
    if not start_via_task():
        log("start request failed; will try again after the next 3 unhealthy checks")
        write_fails(0)
        return 1

    # 一次启动请求之后计数清零：就算没救回来，也等再攒 3 次才重试，
    # 免得每 5 分钟反复重启打断 Celery 导入。
    write_fails(0)
    recovered, waited = wait_for_recovery(host, port, secure=secure)
    if recovered:
        log(f"site is back up after {waited:.0f}s (fail counter reset to 0)")
        return 0
    log(
        f"start requested but site still unhealthy after {waited:.0f}s; "
        f"counter reset to 0, next attempt needs {FAIL_THRESHOLD} more failures",
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
