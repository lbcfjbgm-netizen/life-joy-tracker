"""Steam 令牌 · 自动刷新（后台静默，不弹窗）

【谁在跑它】
    Windows 计划任务「SteamTracker TokenRefresh」，每天 03:50，
    用 .venv\\Scripts\\pythonw.exe 跑（pythonw = 没有黑窗口）。

【它干什么】
    1) 用专用配置目录跑一次 headless Chrome，打开令牌页面
    2) 从页面里抠出 webapi_token
    3) 和 .env 里现有的比：
       拿到了且不一样 → 写进 .env，记「已更新」
       拿到了但一样   → 记「无需更新」
       拿不到/空数据/Chrome 挂了 → 绝不覆盖，记清原因
    4) 追加写日志 .tools\\logs\\token_refresh.log

【前置条件】
    必须先跑过一次 steam-login.py 登录 Steam（专用目录 .tools\\steam-profile）。
    没登录时本脚本会安静地失败（退出码 1），不会破坏 .env 里已有的令牌。

【退出码】
    0 = 正常（含「无需更新」）
    1 = 失败（没读到令牌 / Chrome 出问题）
    例外：加 --task（计划任务模式）时，「还没完成首次登录」只记一笔跳过、返回 0，
          免得任务中心天天亮红；真出错（Chrome 崩、写 .env 失败）照样返回 1。

【参数】
    --force    即使和现有令牌一样也重新写一遍
    --task     计划任务模式：未登录首次状态不算失败（计划任务里用这个）
    --restart  更新成功后调用 schtasks 触发「SteamTracker Start」重启站点
               （默认不重启，因为站点是带 --noreload 跑的，改 .env 要重启才生效）
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # yamtrack 根
TOOLS = ROOT / ".tools"
PROFILE = TOOLS / "steam-profile"                      # 专用配置目录（由 steam-login.py 创建）
LOG = TOOLS / "logs" / "token_refresh.log"
ENV = ROOT / ".env"
KEY = "STEAM_ACCESS_TOKEN"

CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
TOKEN_URL = "https://store.steampowered.com/pointssummary/ajaxgetasyncconfig"

CHROME_TIMEOUT = 90
START_TASK = "SteamTracker Start"


# ---------------------------------------------------------------- 日志 / 环境

def log(line: str) -> None:
    """追加一行带时间戳的日志；顺便打到 stdout（手工跑时能看到）。"""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"[{stamp}] {line}"
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass
    try:
        print(text, flush=True)
    except Exception:
        pass


def fix_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def read_env_token() -> str:
    if not ENV.exists():
        return ""
    try:
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{KEY}="):
                return line[len(KEY) + 1:].strip()
    except Exception as exc:
        log(f"读 .env 失败：{exc}")
    return ""


def write_env(token: str) -> bool:
    """只替换 STEAM_ACCESS_TOKEN 这一行，其他行一字不动。"""
    try:
        lines: list[str] = []
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
        return True
    except Exception as exc:
        log(f"写 .env 失败：{exc}")
        return False


# ---------------------------------------------------------------- 抓令牌

def extract_token(raw: str) -> str:
    match = re.search(r'"webapi_token"\s*:\s*"([^"]{20,})"', raw)
    if match:
        return match.group(1).strip()
    match = re.search(r"&quot;webapi_token&quot;\s*:\s*&quot;([^&]{20,})&quot;", raw)
    if match:
        return match.group(1).strip()
    return ""


def token_from_json(raw: str) -> str:
    try:
        obj = json.loads(raw.strip())
    except Exception:
        return ""
    data = obj.get("data") if isinstance(obj, dict) else None
    if isinstance(data, dict):
        token = data.get("webapi_token")
        if isinstance(token, str) and len(token) > 20:
            return token.strip()
    return ""


def fetch_token() -> tuple[str, str]:
    """返回 (令牌, 失败原因)。成功时原因为空串。"""
    if not CHROME.exists():
        return "", f"找不到 Chrome：{CHROME}"
    if not PROFILE.exists():
        return "", ("专用配置目录不存在 —— 还没跑过 steam-login.py，"
                    "请先跑一次登录助手")

    args = [
        str(CHROME),
        "--headless=new",
        "--dump-dom",
        f"--user-data-dir={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        TOKEN_URL,
    ]
    try:
        proc = subprocess.run(
            args,
            cwd=str(TOOLS),
            capture_output=True,
            timeout=CHROME_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return "", f"Chrome 超过 {CHROME_TIMEOUT} 秒没返回"
    except Exception as exc:
        return "", f"Chrome 起不来：{exc}"

    raw = (proc.stdout or b"").decode("utf-8", errors="replace")
    if not raw.strip():
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        if err:
            return "", "Chrome 没有输出页面内容（" + err.splitlines()[0][:120] + "）"
        return "", "Chrome 没有输出页面内容（退出码 %s）" % proc.returncode

    token = token_from_json(raw) or extract_token(raw)
    if token:
        return token, ""

    if re.search(r'"data"\s*:\s*\[\s*\]', raw):
        return "", "页面返回空数据 —— 专用目录里没有有效登录态，请重跑 steam-login.py"
    if '"webapi_token"' not in raw:
        return "", "页面里找不到 webapi_token —— 多半是登录态失效了"
    return "", "读到了字段但解析不出令牌"


# ---------------------------------------------------------------- 可选重启

def restart_site() -> None:
    """通过计划任务重启站点（不走 Popen，避免被宿主 Job 连坐）。"""
    try:
        subprocess.run(
            ["schtasks", "/run", "/tn", START_TASK],
            cwd=str(ROOT),
            capture_output=True,
            timeout=60,
        )
        log("已触发计划任务「%s」重启站点" % START_TASK)
    except Exception as exc:
        log("触发重启失败（不影响令牌已写入）：%s" % exc)


# ---------------------------------------------------------------- 主流程

def main() -> int:
    fix_console()

    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--force", action="store_true",
                        help="和现有令牌相同也重新写一遍")
    parser.add_argument("--task", action="store_true",
                        help="计划任务模式：未完成首次登录只记跳过、不算失败")
    parser.add_argument("--restart", action="store_true",
                        help="更新成功后触发计划任务重启站点（默认不重启）")
    args = parser.parse_args()

    old = read_env_token()

    token, reason = fetch_token()

    if not token:
        # 失败：一个字都不改
        first_login_missing = (
            "没有有效登录态" in reason or "登录态失效" in reason
        )
        if args.task and first_login_missing:
            log("跳过：%s。等首次登录后即可自动刷新；原有令牌未改动（%s 字符）。"
                % (reason, len(old)))
            return 0
        log("失败：%s。原有令牌未改动（%s 字符）。" % (reason, len(old)))
        return 1

    if token == old and not args.force:
        log("无需更新：令牌仍是同一个（%s 字符），.env 未改动。" % len(token))
        return 0

    if token == old and args.force:
        log("令牌相同，但 --force 要求重写。")

    if not write_env(token):
        log("失败：.env 写不进去，原有令牌可能仍是 %s 字符。" % len(old))
        return 1

    action = "已更新" if token != old else "已强制重写"
    log("%s：令牌 %s → %s 字符，已写入 .env。" % (action, len(old), len(token)))
    # 导入器每次都从 .env 现读令牌（见 integrations/imports/steam.py 的
    # read_access_token），所以不需要重启站点就能用上新令牌。
    log("新令牌已生效：下次导入会直接从 .env 读取，无需重启站点。")

    if args.restart:
        restart_site()

    return 0


if __name__ == "__main__":
    sys.exit(main())
