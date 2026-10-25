"""停止 Steam 记录站的所有进程，静默执行。"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / ".tools" / "logs"
HIDE = 0x08000000


def kill_pid(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        creationflags=HIDE,
    )


def port_pids(port: int) -> list[int]:
    """谁在监听这个端口。"""
    pids: set[int] = set()
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, creationflags=HIDE
        ).stdout
    except Exception:
        return []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and f":{port}" in parts[1] and parts[3] == "LISTENING":
            try:
                pids.add(int(parts[4]))
            except ValueError:
                pass
    return list(pids)


def kill_marked(name: str) -> bool:
    marker = LOGS / f"{name}.pid"
    if not marker.exists():
        return False
    try:
        pid = int(marker.read_text(encoding="utf-8").strip())
    except Exception:
        marker.unlink(missing_ok=True)
        return False
    kill_pid(pid)
    marker.unlink(missing_ok=True)
    return True


def kill_by_command() -> int:
    """按命令行扫一遍，兜底清理没有标记文件的残留。"""
    killed = 0
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -match 'celery|runserver|redis-server' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        creationflags=HIDE,
    )
    return killed


def main() -> int:
    # Tell the watchdog this shutdown was intentional. start_silent.py clears
    # the flag again, so a later double click still brings the site back.
    flag = ROOT / ".tools" / "stopped.flag"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("stopped\n", encoding="utf-8")

    # 先按标记文件杀，精确
    for name in ("celery-worker", "celery-beat"):
        kill_marked(name)

    # 再按端口杀
    for port in (8001, 6379):
        for pid in port_pids(port):
            kill_pid(pid)

    # 兜底
    kill_by_command()

    (LOGS / "last-stop.log").write_text(
        __import__("time").strftime("%Y-%m-%d %H:%M:%S stopped\n"), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
