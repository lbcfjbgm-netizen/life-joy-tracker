"""详情页整页长截图：用系统 python + Playwright 把一整页拍成 PNG 存到桌面。

调用方式（由 ``src/app/views.py`` 的分享视图拉起）::

    python shot_detail.py --json '{"url": ..., "out": ..., "cookie": ..., "width": 1440}'

参数走一个 JSON 字符串，不拼命令行片段：标题里带空格、中文、引号都无所谓，
Windows 也不会把参数拆坏。结果同样以 JSON 打到 stdout（最后一行），视图只读
最后一行，前面任何 Python/Playwright 的噪声都不会干扰它。

为什么不是前端 html2canvas：本站用 Tailwind v4，页面颜色是 oklch()/oklab()，
html2canvas 不认这种颜色函数，渲出来是错的甚至直接抛异常。这里用 Chromium
自己渲染再整页拍，所见即所得。

为什么不用 venv 的 python：项目 venv 里没装 playwright，系统 Python 3.12
里有（chromium 也已装好）。所以视图用 subprocess 调系统解释器，绝不往 venv
里装新依赖。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

DESKTOP = Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop"

# Chromium 一次整页截图的上限。实测 1440×130000 也能一张出完，但为了躲开
# 那个上限、也为了不在内存里憋一张顶天的位图，这里卡在 4 万像素；超过就走
# 「分段拍再拼」那条路（已单独验证过接缝干净）。绝大多数详情页
# （实测游戏页 1166px、图书页 1266px）一次就拍完，根本进不了备用分支。
MAX_HEIGHT = 40000
MAX_WIDTH = 1440

# 文件名里 Windows 不允许的字符：\ / : * ? " < > |
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_filename(title: str, fallback: str = "详情页") -> str:
    """把条目名变成能在 Windows 上直接用、还能看出是谁的文件名。

    去非法字符、掐掉过长的标签尾巴（"某某游戏 - 商店页面"这种），保留中文
    原名，用户一眼知道这张图是什么。
    """
    name = _BAD_CHARS.sub(" ", title or "").strip()
    name = re.sub(r"\s+", " ", name)
    if len(name) > 60:
        name = name[:60].rstrip()
    return name or fallback


def wait_images(page) -> None:
    """把懒加载图片都哄出来，并等到它们真的解码完。

    ``loading="lazy"`` 的图只有滚进视口才开始下载，所以先分步滚下去再滚回
    顶部；随后等所有 ``<img>`` 的 naturalWidth>0。有图 404 也没关系：那种图
    在页面上本来就是占位块，不会让整张截图变难看，所以超时就放过，不卡死。
    """
    try:
        height = page.evaluate("document.documentElement.scrollHeight") or 0
        step = 800
        y = 0
        guard = 0
        while y < height and guard < 200:
            page.mouse.wheel(0, step)
            y += step
            guard += 1
            page.wait_for_timeout(40)
            height = page.evaluate("document.documentElement.scrollHeight") or height
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
        page.wait_for_function(
            """() => Array.from(document.images)
                     .filter(img => img.getAttribute('loading') !== 'lazy' || img.complete)
                     .every(img => img.naturalWidth > 0 || img.complete)""",
            timeout=8000,
        )
    except Exception:
        # 装饰性兜底：图片等不齐就把已经渲染好的拍下来，总比一张图都没有强。
        pass


def capture(url: str, out: Path, sid: str, width: int = 1440, timeout: int = 60000) -> dict:
    """打开页面并整页截图，返回给视图用的结果字典。"""
    from playwright.sync_api import sync_playwright

    started = time.time()
    out.parent.mkdir(parents=True, exist_ok=True)
    # 站点进程是「无窗口 + 受限」跑起来的，默认的 %TEMP% 可能不让建目录
    # （Playwright 一 launch 就 EPERM）。把临时目录挪到脚本旁边的 .tools\shot-tmp，
    # 这个位置一定可写。
    tmp_root = Path(__file__).resolve().parent / "shot-tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    os.environ["TMP"] = str(tmp_root)
    os.environ["TEMP"] = str(tmp_root)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": width, "height": 1000},
            device_scale_factor=1,
            extra_http_headers={"Cookie": f"sessionid={sid}"},
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        # 详情页的顶栏/侧栏是服务端渲染好再被 Alpine 接管，等网络静下来才稳
        try:
            page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            page.wait_for_timeout(1200)
        page.wait_for_timeout(500)
        wait_images(page)

        full_h = page.evaluate("document.documentElement.scrollHeight") or 0
        full_h = max(int(full_h), 600)
        # 超长页面切成几段分别拍再拼：实测一次拍 13 万像素虽然能出图，但再长
        # Chromium 就开始出白块。分段拍每段都从真实滚动位置渲染，拼接处按滚动
        # 偏移对齐，正好贴着原图接上。
        if full_h <= MAX_HEIGHT:
            page.screenshot(path=str(out), full_page=True, animations="disabled")
            shot_h = full_h
        else:
            _stitch(page, out, full_h, width)
            shot_h = full_h

        browser.close()

    size = out.stat().st_size if out.exists() else 0
    return {
        "ok": True,
        "path": str(out),
        "name": out.name,
        "bytes": size,
        "height": int(shot_h),
        "width": int(width),
        "seconds": round(time.time() - started, 1),
    }


def _stitch(page, out: Path, total_h: int, width: int) -> None:
    """把超过一次截图上限的长页面分块拍下再纵向拼成一张。

    每块都有重叠、并且按「真实滚动位置」贴回画布，不按理论偏移贴：浏览器滚到
    底时会自动夹住 scrollY，按理论值拼就会错位留黑边。重叠部分由后一块覆盖
    前一块，两块又是同一页面同一位置渲染出来的，接缝看不出来。
    """
    from PIL import Image

    band = 8000
    overlap = 400
    step = band - overlap
    page.set_viewport_size({"width": width, "height": band})
    page.wait_for_timeout(250)

    tiles = []
    tmp_dir = out.parent / "_shot_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        y = 0
        index = 0
        guard = 0
        while guard < 60:
            page.evaluate(f"window.scrollTo(0, {y})")
            page.wait_for_timeout(200)
            actual = int(page.evaluate("window.scrollY") or 0)
            tile = tmp_dir / f"tile{index}.png"
            page.screenshot(path=str(tile), full_page=False, animations="disabled")
            tiles.append((tile, actual))
            index += 1
            guard += 1
            if actual + band >= total_h:
                break
            y += step
        canvas = Image.new("RGB", (width, total_h), (10, 10, 10))
        for tile, top in tiles:
            with Image.open(tile) as im:
                canvas.paste(im, (0, top))
        canvas.save(out, optimize=True)
    finally:
        for tile, _ in tiles:
            tile.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", required=True, help="JSON with url/out/title/cookie/width")
    args = parser.parse_args()

    try:
        payload = json.loads(args.json)
        url = payload["url"]
        out = Path(payload["out"])
        sid = payload.get("cookie", "")
        width = int(payload.get("width") or MAX_WIDTH)
        result = capture(url, out, sid, width)
    except Exception as error:  # noqa: BLE001 - 失败了也要给视图一个结构化答案
        result = {
            "ok": False,
            "error": f"{type(error).__name__}: {error}".strip()[:400],
        }

    # 只让视图看最后一行：Playwright 自己会往 stdout 打东西。
    sys.stdout.write("\n_RESULT_JSON_" + json.dumps(result, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
