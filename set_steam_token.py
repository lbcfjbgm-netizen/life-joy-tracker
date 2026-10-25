"""把 Steam 家庭共享令牌写进 .env。

双击同目录的 set-steam-token.bat 就会调用这个脚本：
1. 打开 Steam 的配置页面
2. 等用户把 webapi_token 粘进来
3. 写进 .env，重启站点
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
URL = "https://store.steampowered.com/pointssummary/ajaxgetasyncconfig"
KEY = "STEAM_ACCESS_TOKEN"


def ask_token() -> str:
    """让用户把令牌粘进来，顺手把多余的东西剥掉。"""
    print("=" * 60)
    print(" 获取 Steam 家庭共享令牌")
    print("=" * 60)
    print()
    print("1) 浏览器会打开一个页面（可能显示一堆 JSON）")
    print("2) 在里面找这样一段： \"webapi_token\": \"xxxxx\"")
    print("3) 把 xxxxx 那串复制下来（很长的十六进制字符）")
    print("4) 粘到下面，回车")
    print()
    print("如果浏览器里没登录 Steam，先登录 https://store.steampowered.com/")
    print()
    webbrowser.open(URL)

    raw = input("把令牌粘到这里然后回车：").strip()
    if not raw:
        return ""

    # 用户可能把整段 JSON 粘进来，也可能连 key 一起粘
    match = re.search(r'"webapi_token"\s*:\s*"([0-9A-Fa-f]+)"', raw)
    if match:
        return match.group(1)
    match = re.search(r"([0-9A-Fa-f]{16,})", raw)
    if match:
        return match.group(1)
    return raw


def write_env(token: str) -> None:
    """把令牌写进 .env，已经有这行就替换，没有就追加。"""
    lines = []
    if ENV.exists():
        lines = ENV.read_text(encoding="utf-8").splitlines()

    replaced = False
    for index, line in enumerate(lines):
        if line.startswith(f"{KEY}="):
            lines[index] = f"{KEY}={token}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{KEY}={token}")

    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def restart_site() -> None:
    """停一下再起，让新的 .env 生效。"""
    stop = ROOT / "stop_silent.py"
    start = ROOT / "start_silent.py"
    if not VENV_PY.exists():
        print("找不到 venv 的 python，请手动双击 stop-win.vbs 再双击 start-win.vbs")
        return
    print("正在重启站点…")
    subprocess.run([str(VENV_PY), str(stop)], cwd=str(ROOT), timeout=120)
    subprocess.run([str(VENV_PY), str(start)], cwd=str(ROOT), timeout=240)
    print("已重启，等几秒后打开 http://127.0.0.1:8001")


def main() -> int:
    token = ask_token()
    if not token:
        print("没输入令牌，什么都没改。")
        return 1

    write_env(token)
    print(f"已写入 {ENV}（{KEY} 长度 {len(token)}）")
    restart_site()
    print()
    print("完成。下一次导入 Steam 时会自动带上家庭共享库。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
