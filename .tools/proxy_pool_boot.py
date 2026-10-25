"""Start the Douban proxy pool kernel at logon, with a log a human can read.

The scheduled task runs this through ``pythonw.exe``, so there is no console and
anything the pool prints would be lost. Here it is written to
``.tools/logs/proxy_pool.log`` instead, and an already running pool counts as
success rather than as a failure, so the task does not show a red result every
morning.

Only the kernel (``verge-mihomo.exe``) is started: the pool's own ``up`` command
refuses to touch a port that is already taken unless it is told ``--force``, and
that flag is never passed here. The Clash Verge GUI is never started.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / ".tools" / "logs"
LOG = LOGS / "proxy_pool.log"


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    """Return whether something already listens on the port."""
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.6)
        return sock.connect_ex((host, port)) == 0


def main() -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8", errors="replace") as log:
        sys.stdout = log
        sys.stderr = log

        if port_open(7899) and port_open(19197):
            print("[skip] proxy pool is already listening on 7899/19197")
            return 0

        sys.path.insert(0, str(ROOT / ".tools"))
        import douban_proxy_pool  # noqa: PLC0415

        print("[boot] starting the proxy pool kernel")
        code = douban_proxy_pool.main(["up"])
        print(f"[boot] douban_proxy_pool up exited {code}")
        return code


if __name__ == "__main__":
    sys.exit(main())
