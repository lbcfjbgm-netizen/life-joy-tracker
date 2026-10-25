"""Steam 令牌 · 一次性登录助手（只需跑一次，之后全自动）

【首次使用怎么做】
    双击 yamtrack\\steam-login.bat，或者命令行里跑：
        .venv\\Scripts\\python.exe .tools\\steam-login.py
    弹出的 Chrome 窗口里登录 Steam（Steam 问「记住我」就勾上），
    登录完关掉那个窗口，回到控制台按回车。
    脚本会自己把令牌读出来写进 .env。
    只需登录这一次 —— 登录 cookie 存在专用配置目录里，实测有效期到 2027 年。

【之后怎么维护】
    不用你管。每天 03:50 计划任务「SteamTracker TokenRefresh」会自动跑
    .tools\\refresh-steam-token.py，用同一个专用配置目录后台静默刷新令牌。

【为什么这么做】
    Steam 这个令牌页面没有跨域许可，网页里的脚本读不到；
    它本身也不校验来源，只要带着已登录的 cookie，本地程序就能读。
    而 Chrome 127 之后的 cookie 密钥和 Chrome+本机绑定，外部程序解不开，
    所以不能去偷用户日常 Chrome 的 cookie。
    办法是：另开一个固定的专用配置目录（.tools\\steam-profile\\），
    让用户在里面登录一次，之后就用这个目录反复读令牌 —— 登录态长期有效。

【令牌寿命】
    JWT，约 24 小时。所以每天刷一次，永远是新令牌。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # yamtrack 根
TOOLS = ROOT / ".tools"
PROFILE = TOOLS / "steam-profile"                      # 专用配置目录（固定，别删）
ENV = ROOT / ".env"
KEY = "STEAM_ACCESS_TOKEN"

CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
LOGIN_URL = "https://store.steampowered.com/"
TOKEN_URL = "https://store.steampowered.com/pointssummary/ajaxgetasyncconfig"

ATTEMPTS = 3          # 关窗口后可能有一两秒 profile 还锁着，多试几次
CHROME_TIMEOUT = 90   # 单次 headless 最长等多久


def say(msg: str = "") -> None:
    """打印，但 pythonw 下没有控制台也不要炸。"""
    try:
        print(msg, flush=True)
    except Exception:
        pass


def fix_console() -> None:
    """命令行编码不对时，中文别抛异常。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def chrome_ok() -> bool:
    return CHROME.exists()


def launch_visible() -> subprocess.Popen | None:
    """开一个可见的 Chrome 窗口，专用配置目录，让用户登录。"""
    args = [
        str(CHROME),
        f"--user-data-dir={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        LOGIN_URL,
    ]
    say("[1/3] 正在打开 Chrome 窗口（专用配置目录，不影响你平时的浏览器）…")
    try:
        return subprocess.Popen(args, cwd=str(TOOLS))
    except Exception as exc:
        say(f"  × Chrome 启动失败：{exc}")
        return None


def profile_in_use() -> bool:
    """专用配置目录是不是还开着 Chrome 窗口。

    Chrome 同一个 user-data-dir 同时只允许一个实例；窗口没关就跑 headless，
    会连不上、输出为空。所以先问一句系统：还有没有带 steam-profile 的 chrome。
    """
    cmd = (
        "@(Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -like '*steam-profile*' }).Count"
    )
    for exe in ("powershell", "pwsh"):
        try:
            proc = subprocess.run(
                [exe, "-NoProfile", "-NonInteractive", "-Command", cmd],
                capture_output=True, timeout=25,
            )
            text = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
            if text.isdigit():
                return int(text) > 0
        except Exception:
            continue
    return False   # 查不出来就当没占用，后面重试兜底


def wait_for_close(limit: int = 120) -> bool:
    """等用户把窗口关掉。返回 True 表示已经没有占用。"""
    if not profile_in_use():
        return True
    say()
    say("  ⚠ 检测到那个 Chrome 窗口还开着。")
    say("    同一个配置目录同时只能开一个实例，窗口不关我就读不到令牌。")
    say(f"    请把它关掉，我在这儿等（最多 {limit} 秒）…")
    deadline = time.time() + limit
    while time.time() < deadline:
        time.sleep(4)
        if not profile_in_use():
            say("  √ 窗口已关，继续。")
            return True
        say("  … 还开着，继续等")
    say("  × 等太久了，窗口好像还在。")
    return False


def fetch_token_once() -> tuple[str, str]:
    """用同一个专用目录跑一次 headless，读令牌页面。返回 (令牌, 说明)。"""
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
        return "", "Chrome 没有输出页面内容"

    token = token_from_json(raw) or extract_token(raw)
    if token:
        return token, "ok"

    # 分不清是「没登录」还是「返回了空 data」，都按失败处理，绝不瞎写
    if '"data"' in raw and re.search(r'"data"\s*:\s*\[\s*\]', raw):
        return "", "页面返回了空数据 —— 专用配置目录里没有有效的 Steam 登录态"
    if '"webapi_token"' not in raw:
        return "", "页面里找不到 webapi_token —— 多半是没登录成功"
    return "", "读到了字段但解析不出令牌"


def extract_token(raw: str) -> str:
    """从 --dump-dom 的输出里抠出 webapi_token。"""
    # 正常情况：<pre>{"success":1,"data":{"webapi_token":"..."}}</pre>
    match = re.search(r'"webapi_token"\s*:\s*"([^"]{20,})"', raw)
    if match:
        return match.group(1).strip()
    # 兜底：HTML 转义过的引号
    match = re.search(r"&quot;webapi_token&quot;\s*:\s*&quot;([^&]{20,})&quot;", raw)
    if match:
        return match.group(1).strip()
    return ""


def token_from_json(raw: str) -> str:
    """有些时候输出是干净的 JSON，直接解析更稳。"""
    text = raw.strip()
    try:
        obj = json.loads(text)
    except Exception:
        return ""
    data = obj.get("data") if isinstance(obj, dict) else None
    if isinstance(data, dict):
        token = data.get("webapi_token")
        if isinstance(token, str) and len(token) > 20:
            return token.strip()
    return ""


def read_env_token() -> str:
    if not ENV.exists():
        return ""
    try:
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{KEY}="):
                return line[len(KEY) + 1:].strip()
    except Exception:
        pass
    return ""


def write_env(token: str) -> None:
    """只动 STEAM_ACCESS_TOKEN 这一行，其他行原样保留。"""
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


def main() -> int:
    fix_console()
    say("=" * 62)
    say("  Steam 家庭共享令牌 · 一次性登录助手")
    say("=" * 62)
    say()
    say(f"专用配置目录：{PROFILE}")
    say("（这个目录就是你的「登录状态」，别删；删了就要重新登录一次）")
    say()

    if not chrome_ok():
        say(f"× 找不到 Chrome：{CHROME}")
        say("  装好 Chrome，或者改这个脚本里的 CHROME 路径再试。")
        return 1

    try:
        PROFILE.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        say(f"× 建不了专用目录：{exc}")
        return 1

    old = read_env_token()
    if old:
        say(f"现在 .env 里的令牌长度：{len(old)} 字符（约 24 小时后过期）")
    else:
        say("现在 .env 里没有令牌。")
    say()

    proc = launch_visible()
    if proc is None:
        return 1

    say()
    say("【该你动手了】—— 只需登录这一次，登录状态能维持一年左右")
    say("  1) 在弹出的 Chrome 窗口里登录你的 Steam 账号")
    say("     ★ 如果 Steam 问「记住我 / 保持登录」，一定要勾上！")
    say("       不勾的话会退化成「临时登录」，关窗口就掉线，白忙一场。")
    say("  2) 等它跳回商店首页、确认右上角已经是你的头像")
    say("  3) 把这个 Chrome 窗口关掉（一定要关，不然读不出令牌）")
    say("  4) 回到这个黑窗口，按回车")
    say()
    say("  说明：登录 cookie 存在专用配置目录里，实测有效期到 2027 年，")
    say("        到期前你都不用再管这件事。")
    say()
    try:
        input("登录完成并关掉窗口后，在这里按回车继续…")
    except Exception:
        say("（没读到回车，直接继续）")

    say()
    say("[2/3] 正在用同一个专用目录后台读取令牌…")

    # 窗口没关的话先等它关（同一个配置目录不能同时开两个 Chrome）
    if not wait_for_close():
        say()
        say("[3/3] × 没读到令牌，什么都没改（.env 保持原样）。")
        say("  原因：专用 Chrome 窗口还开着，headless 打不开同一个配置目录。")
        say("  补救：关掉那个窗口，重新跑一次这个脚本。")
        return 1

    token = ""
    reason = ""
    for attempt in range(1, ATTEMPTS + 1):
        if attempt > 1:
            say(f"  第 {attempt} 次重试（等 3 秒）…")
            time.sleep(3)
        token, reason = fetch_token_once()
        if token:
            break

    if not token:
        say()
        say("[3/3] × 没读到令牌，什么都没改（.env 保持原样）。")
        say(f"  原因：{reason}")
        say()
        say("怎么补救：")
        say("  1) 确认那个 Chrome 窗口是真的关掉了（任务管理器里别有残留）")
        say("  2) 重新跑一次这个脚本，登录时勾上「记住我」")
        say("  3) 确认你登录的是 store.steampowered.com，不是社区/帮助页")
        return 1

    say(f"  读到了，长度 {len(token)} 字符。")
    if token == old:
        say()
        say("[3/3] 和 .env 里现有的一模一样，不用改。")
        return 0

    write_env(token)
    say()
    say("[3/3] √ 已写进 .env（只动了 STEAM_ACCESS_TOKEN 这一行）。")
    say()
    say("注意：站点是带 --noreload 跑的，改 .env 不会立刻生效；")
    say("      下次站点重启（双击 stop-win.vbs 再 start-win.vbs）后生效。")
    say()
    say("以后不用再手工粘令牌了：")
    say("      每天 03:50 计划任务「SteamTracker TokenRefresh」会自动刷新。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
