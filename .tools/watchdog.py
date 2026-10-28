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

Why Celery is checked by pid file, not by a port (2026-10-26)
-------------------------------------------------------------
The three HTTP probes only prove that Django answers. On 2026-10-26 between
07:27 and 10:51 the Celery worker died fifteen times in a row -- every crash
was ``Unrecoverable error: OperationalError('unable to open database file')``
raised while storing a task result, which made the worker stop with
EX_FAILURE. The site stayed perfectly healthy the whole time, so the nightly
Steam import would silently have been skipped for days.

Neither celery process listens on a port (they talk to Redis), so the only
cheap and reliable signal is the pid file: ``celery-worker.pid`` and
``celery-beat.pid`` next to this script's logs. A pid alone can be recycled by
Windows, so the pid is also matched against the real command line (it must
contain ``celery`` plus the word ``worker`` or ``beat``). Fetching the command
line costs one PowerShell call for both pids; if that call fails the check
degrades to "process exists" and says so in the log instead of inventing a
verdict.

Reviving Celery must not touch Django: start_silent.py looks at port 8001 and
at the same two pid files first and only spawns whatever is missing, it never
kills anything. So the very same "SteamTracker Start" task is the right tool --
a running Django is left alone and only the dead worker/beat is started.

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
import os
import re
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

# --- Celery 探活（2026-10-26 新增）---
# worker / beat 不监听端口，只能靠 pid 文件 + 命令行双重确认。
CELERY_ROLES = (("worker", LOGS / "celery-worker.pid"), ("beat", LOGS / "celery-beat.pid"))
CELERY_FAILS = TOOLS / "celery_watchdog_fails"  # 连续几次探活失败才动手
CELERY_LAST_START = TOOLS / "celery_watchdog_last_start"  # 上次动手的时间戳
CELERY_FAIL_THRESHOLD = 2  # 连续 2 次失败才救（网页是 3 次）
CELERY_COOLDOWN = 15 * 60  # 动手后 15 分钟内不再动手，免得疯狂刷计划任务


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


def read_int(path: Path) -> int:
    """读一个「整数小文件」；不存在或内容坏了都当 0。"""
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip() or "0"))
    except (OSError, ValueError):
        return 0


def write_int(path: Path, value: int) -> None:
    """写整数小文件，归零时直接删掉，目录里保持干净。"""
    try:
        if int(value) <= 0:
            path.unlink(missing_ok=True)
            return
        TOOLS.mkdir(parents=True, exist_ok=True)
        path.write_text(str(int(value)), encoding="utf-8")
    except OSError as exc:
        log(f"WARN cannot write {path.name}: {exc}")


def pid_exists(pid: int) -> bool:
    """这个 PID 的进程还在不在。

    只用 OpenProcess + GetExitCodeProcess 查句柄，绝不发信号。
    Windows 上 ``os.kill(pid, 0)`` 不是「探活」，它走 TerminateProcess，
    真的会把进程杀掉 —— 所以这里一行都不能用它。
    """
    if pid <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001 - 非 Windows 上就当作查不到
        return False
    process_query_limited_information = 0x1000
    still_active = 259
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False


def scan_celery_procs() -> dict[int, str] | None:
    """一次 PowerShell 调用拿到所有 python 进程的 {pid: 命令行}。

    返回 ``None`` 表示「这次没查成」，跟「确实没有 celery 进程」要区分开，
    否则查询失败会被误判成进程死掉，进而白重启一次。
    """
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%'\" |"
        " ForEach-Object { if ($_.CommandLine) { \"$($_.ProcessId)`t$($_.CommandLine)\" } }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=HIDE,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"WARN celery probe: cannot read command lines: {exc}")
        return None
    if result.returncode != 0:
        err = (result.stderr or "").strip()[:150]
        log(f"WARN celery probe: command line query exited {result.returncode}: {err}")
        return None
    procs: dict[int, str] = {}
    for line in (result.stdout or "").splitlines():
        pid_text, _, cmdline = line.partition("\t")
        pid_text = pid_text.strip()
        cmdline = cmdline.strip()
        if pid_text.isdigit() and cmdline:
            procs[int(pid_text)] = cmdline
    return procs or None


CELERY_MARK = re.compile(r"-m\s+celery\b", re.IGNORECASE)


def celery_role_of(cmdline: str) -> str:
    """命令行属于哪个 celery 角色：worker / beat / 空字符串（不是 celery）。"""
    if not CELERY_MARK.search(cmdline):
        return ""
    if re.search(r"\bbeat\b", cmdline):
        return "beat"
    if re.search(r"\bworker\b", cmdline):
        return "worker"
    return ""


def probe_celery() -> tuple[list[tuple[str, bool, str]], bool]:
    """按 pid 文件 + 真实命令行确认 worker / beat 还活着。

    返回 ([(角色, 是否活着, 一句话说明)], 命令行是否查成功)。
    pid 会被系统复用，所以光看「进程还在」不算数，命令行里必须真的是
    某个 celery worker/beat；反过来，pid 文件写死了但进程是真活着（换过壳、
    pid 文件过期），也算活着 —— 宁可漏报也不能误起重开一份。
    """
    procs = scan_celery_procs()
    live_by_role: dict[str, list[int]] = {"worker": [], "beat": []}
    if procs is not None:
        for pid, cmdline in procs.items():
            role = celery_role_of(cmdline)
            if role:
                live_by_role[role].append(pid)

    results: list[tuple[str, bool, str]] = []
    for role, pid_file in CELERY_ROLES:
        pid = read_int(pid_file)
        if not pid:
            results.append((role, False, f"{role} DEAD no pid file"))
            continue
        if procs is None:
            # 命令行查不到，退回最保守的「进程在不在」，并在说明里写清楚
            if pid_exists(pid):
                results.append((role, True, f"{role} alive pid {pid} (cmdline unverified)"))
            else:
                results.append((role, False, f"{role} DEAD pid {pid} gone"))
            continue
        recorded_alive = pid in procs
        if recorded_alive and celery_role_of(procs[pid]) == role:
            results.append((role, True, f"{role} alive pid {pid}"))
            continue
        others = live_by_role.get(role, [])
        if others:
            # pid 文件过期但确实有活的本尊：算活着，别再造一份
            results.append(
                (role, True, f"{role} alive pid {others[0]} (pid file {pid} stale)"),
            )
        elif recorded_alive:
            results.append((role, False, f"{role} DEAD pid {pid} reused by another process"))
        else:
            results.append((role, False, f"{role} DEAD pid {pid} gone"))
    return results, procs is not None


def celery_summary(results: list[tuple[str, bool, str]]) -> str:
    """拼成一行里的尾巴：``2/2 alive`` 或者 ``0/2 alive (worker DEAD pid 123 gone)``。"""
    alive = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    if alive == total:
        return f"{alive}/{total} alive"
    dead = [text for _, ok, text in results if not ok]
    return f"{alive}/{total} alive ({'; '.join(dead)})"


def wait_for_celery(waited: float = 25.0) -> tuple[bool, float]:
    """救完之后等一会儿再探一次，让日志能写明「救回来了没有」。"""
    started = time.perf_counter()
    while time.perf_counter() - started < waited:
        results, _ = probe_celery()
        if all(ok for _, ok, _ in results):
            return True, time.perf_counter() - started
        time.sleep(4)
    return False, time.perf_counter() - started


def drop_dead_pid_files(results: list[tuple[str, bool, str]], reliable: bool) -> None:
    """把确认已死的角色的 pid 文件删掉。

    为什么非删不可：start_silent.py 的 ``ensure_marked`` 只看「pid 文件里这个
    PID 的进程还在不在」，不看它是不是 celery。PID 被系统复用的那天，它就会
    以为 worker 还活着而跳过启动，看门狗再怎么喊也救不回来。
    只删这两个 pid 文件，别的一律不碰。

    ``reliable=False``（命令行没查成）时一律不删：那种情况下「判死」本身就
    不可信，删错了会让 start_silent.py 多起一份 celery。
    """
    if not reliable:
        return
    for role, alive, _ in results:
        if alive:
            continue
        pid_file = dict(CELERY_ROLES)[role]
        try:
            if pid_file.exists():
                pid_file.unlink()
                log(f"removed stale {pid_file.name} so start_silent.py will respawn {role}")
        except OSError as exc:
            log(f"WARN cannot remove {pid_file.name}: {exc}")


def handle_celery(
    fails: int,
    no_restart: bool,
    results: list[tuple[str, bool, str]],
    reliable: bool,
) -> None:
    """celery 死了才动手：连续 2 次探活失败 + 15 分钟节流。

    只走计划任务 ``SteamTracker Start``，绝不自己 Popen —— 本项目踩过坑，
    从 DSH 的终端里起的进程会跟 DSH 的 Job 一起被连坐杀掉。
    start_silent.py 是「缺哪个起哪个」，Django 还在 8001 上跑的时候它只会补
    拉起死掉的 worker / beat，不会踢掉正在服务的站点。
    """
    if no_restart:
        log(
            f"celery unhealthy (fails={fails}/{CELERY_FAIL_THRESHOLD}), but --no-restart "
            f"was given -> NOT asking the scheduler to start celery",
        )
        return
    if fails < CELERY_FAIL_THRESHOLD:
        log(
            f"celery unhealthy (fails={fails}/{CELERY_FAIL_THRESHOLD}) "
            f"-> waiting for another failed check before restarting",
        )
        return
    now = int(time.time())
    last = read_int(CELERY_LAST_START)
    if last and now - last < CELERY_COOLDOWN:
        log(
            f"celery still down but restarted {(now - last) // 60} min ago "
            f"(cooldown {CELERY_COOLDOWN // 60} min) -> NOT restarting again",
        )
        return
    log(f'celery down {fails} checks in a row -> schtasks /run "{STACK_TASK}" (celery only)')
    write_int(CELERY_LAST_START, now)
    write_int(CELERY_FAILS, 0)  # 动过手就归零，下次要再攒 2 次
    # 先删掉死掉的 pid 文件，否则 start_silent.py 可能以为它还活着而跳过启动
    drop_dead_pid_files(results, reliable)
    if not start_via_task():
        log("celery start request failed; will retry after the next 2 failed checks")
        return
    recovered, waited = wait_for_celery()
    if recovered:
        log(f"celery is back up after {waited:.0f}s")
    else:
        log(
            f"celery start requested but still not alive after {waited:.0f}s; "
            f"cooldown {CELERY_COOLDOWN // 60} min applies before the next attempt",
        )


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
    parser.add_argument(
        "--skip-celery",
        action="store_true",
        help="只探网页、跳过 celery 探活（调试时用，计划任务永远不带这个参数）",
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
        write_int(CELERY_FAILS, 0)
        log(f"SKIP stopped.flag present -> site deliberately stopped, no probe, no start (fails reset 0, was {fails})")
        return 0

    # celery 探活和网页探活是两件独立的事：网页活着不代表 worker 活着
    # （worker 假死时页面照常能打开，第二天定时导入就是没人跑）。
    celery_results: list[tuple[str, bool, str]] = []
    celery_ok = True
    celery_reliable = False
    celery_text = "celery skipped"
    if not args.skip_celery:
        celery_results, celery_reliable = probe_celery()
        celery_ok = all(ok for _, ok, _ in celery_results)
        celery_text = f"celery {celery_summary(celery_results)}"

    results = [probe(host, port, path, secure=secure) for path in PROBE_PATHS]
    healthy = all(ok for ok, _ in results)
    detail = " | ".join(text for _, text in results)
    detail = f"{detail} | {celery_text}"

    if not args.skip_celery:
        celery_fails = 0 if celery_ok else read_int(CELERY_FAILS) + 1
        write_int(CELERY_FAILS, celery_fails)
        if not celery_ok:
            handle_celery(celery_fails, args.no_restart, celery_results, celery_reliable)

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
