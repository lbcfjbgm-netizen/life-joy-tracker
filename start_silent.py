"""启动 Steam 记录站 —— 完全静默，不弹任何黑框。

用 pythonw.exe 运行（无控制台），依次拉起 Redis、Celery worker、Celery beat、Django。
重复运行安全：已经在跑的不会重复启动。

停止：双击 stop-win.vbs
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
VENV_PYW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
SRC = ROOT / "src"
LOGS = ROOT / ".tools" / "logs"
REDIS_EXE = ROOT / ".tools" / "redis" / "redis-server.exe"
REDIS_DATA = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "yamtrack" / "redis-data"

SITE_PORT = 8001
REDIS_PORT = 6379

# Windows 进程创建标志。
#
# 0x00000008 DETACHED_PROCESS      不继承控制台
# 0x00000200 CREATE_NEW_PROCESS_GROUP
# 0x08000000 CREATE_NO_WINDOW      不弹黑框
# 0x01000000 CREATE_BREAKAWAY_FROM_JOB
#
# 最后那个是关键。之前没有它，注释里「脱离父进程独立存活」是不成立的：
# DETACHED_PROCESS 只脱离控制台，不脱离 Windows Job 对象。而本机启动脚本常常
# 是别的程序（比如带 Job 的桌面工具）拉起来的，那个 Job 一关，内核会把整组
# 进程一起杀掉，进程连异常都来不及写，日志里就是干干净净地消失。
# 加上这个标志，子进程才会真正脱离父进程所在的 Job。
CREATE_FLAGS = 0x00000008 | 0x00000200 | 0x08000000 | 0x01000000

# 父进程所在 Job 若不允许 breakaway，CreateProcess 会报 ERROR_ACCESS_DENIED
# (5)。这种情况退回不带 breakaway 的老标志，至少还能起来。
FALLBACK_FLAGS = 0x00000008 | 0x00000200 | 0x08000000

# 单个日志文件的上限，超过就改名归档，避免无限增长
LOG_MAX_BYTES = 8 * 1024 * 1024


def port_open(port: int) -> bool:
    """本地端口有没有人在听。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def pid_alive(pid: int) -> bool:
    """这个 PID 的进程还活着吗（Windows 上用 tasklist 查）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            creationflags=0x08000000,
        ).stdout
        return str(pid) in out
    except Exception:
        return False


def rotate_log(path: Path) -> None:
    """日志太大就改名归档，绝不删除历史，免得丢掉崩溃现场。"""
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path.rename(path.with_name(f"{path.stem}-{stamp}{path.suffix}"))
    except OSError:
        pass


def spawn(exe: Path, args: list[str], cwd: Path, log_name: str) -> int:
    """启动一个无窗口、脱离父进程 Job 的进程，返回 PID。

    日志用追加模式（``"a"``），不是覆盖模式：以前每次启动都用 ``"w"`` 清空，
    上一次崩溃的现场就被这一次启动抹掉了，导致「日志干净」既不能证明没崩，
    也让排查无从下手。
    """
    LOGS.mkdir(parents=True, exist_ok=True)
    out_path = LOGS / f"{log_name}.out"
    err_path = LOGS / f"{log_name}.err"
    rotate_log(out_path)
    rotate_log(err_path)

    out = open(out_path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115
    err = open(err_path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115

    env = os.environ.copy()
    env.pop("DB_HOST", None)  # 必须不设，才会走 SQLite
    env["PYTHONIOENCODING"] = "utf-8"
    # 这里刻意不再 setdefault("DOUBAN_PROXY", ...)。
    # 原来那行等于强制给所有豆瓣请求塞代理池 http://127.0.0.1:7899，而代理池
    # 不是常驻进程，它一停整批豆瓣导入就全灭（10-24、10-25 连续两天）。
    # 现在改成「默认直连」：想用代理就在 .env 或系统环境里自己配 DOUBAN_PROXY，
    # 不配就是直连，谁拿去用都不用先装代理池。
    # 另外 providers/douban_movie.py 现在会自己探测代理通不通、不通就退回直连。

    common = {
        "cwd": str(cwd),
        "stdout": out,
        "stderr": err,
        "stdin": subprocess.DEVNULL,
        "env": env,
        "close_fds": True,
    }

    try:
        proc = subprocess.Popen(
            [str(exe), *args],
            creationflags=CREATE_FLAGS,
            **common,
        )
    except OSError:
        # 父进程的 Job 不允许 breakaway，退回旧标志再试一次
        proc = subprocess.Popen(
            [str(exe), *args],
            creationflags=FALLBACK_FLAGS,
            **common,
        )
    return proc.pid


def wait_port(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port):
            return True
        time.sleep(0.4)
    return False


def ensure_marked(name: str, exe: Path, args: list[str]) -> str:
    """按 PID 标记文件管理非端口型进程，避免重复启动。"""
    marker = LOGS / f"{name}.pid"
    if marker.exists():
        try:
            old = int(marker.read_text(encoding="utf-8").strip())
        except Exception:
            old = 0
        if old and pid_alive(old):
            return f"{name}: already running"
        marker.unlink(missing_ok=True)

    pid = spawn(exe, args, SRC, name)
    marker.write_text(str(pid), encoding="utf-8")
    return f"{name}: started"


def main() -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    REDIS_DATA.mkdir(parents=True, exist_ok=True)
    # A deliberate start clears the "user asked me to stay down" flag that
    # stop_silent.py raises, so the watchdog resumes guarding afterwards.
    (ROOT / ".tools" / "stopped.flag").unlink(missing_ok=True)
    lines: list[str] = []

    # --- Redis ---
    if port_open(REDIS_PORT):
        lines.append("redis: already running")
    else:
        spawn(
            REDIS_EXE,
            [
                "--port", str(REDIS_PORT),
                "--bind", "127.0.0.1",
                "--dir", str(REDIS_DATA),
                "--save", "",
                "--appendonly", "no",
            ],
            ROOT,
            "redis",
        )
        ok = wait_port(REDIS_PORT, 20)
        lines.append(f"redis: {'started' if ok else 'FAILED'}")

    # --- Django ---
    if port_open(SITE_PORT):
        lines.append("site: already running")
    else:
        spawn(
            VENV_PYW,
            ["manage.py", "runserver", f"127.0.0.1:{SITE_PORT}", "--noreload"],
            SRC,
            "site",
        )
        ok = wait_port(SITE_PORT, 30)
        lines.append(f"site: {'started' if ok else 'FAILED'}")

    # --- Celery worker ---
    lines.append(
        ensure_marked(
            "celery-worker",
            VENV_PYW,
            [
                "-m", "celery", "--app", "config", "worker",
                "--loglevel", "INFO", "--pool", "solo",
                "--without-mingle", "--without-gossip",
            ],
        )
    )

    # --- Celery beat ---
    lines.append(
        ensure_marked(
            "celery-beat",
            VENV_PYW,
            ["-m", "celery", "--app", "config", "beat", "--loglevel", "INFO"],
        )
    )

    (LOGS / "last-start.log").write_text(
        time.strftime("%Y-%m-%d %H:%M:%S\n") + "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
