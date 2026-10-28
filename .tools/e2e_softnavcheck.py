# -*- coding: utf-8 -*-
"""确认点击是不是真的走了 htmx 软导航（用一个只有软导航才存活的 JS 变量做标记）。

如果点击触发整页跳转，window.__marker 会消失；软导航则保留。
"""
import pathlib
import sys
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent  # 脚本在 .tools 下，上一级即项目根
SID = (ROOT / ".tools" / "_sid.txt").read_text(encoding="utf-8").strip()

CASES = ["/statistics", "/journal"]
BRAND = "#lj-topnav a.lj-tn-brand"


def run(port):
    print(f"===== port {port}")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1600, "height": 1000})
        ctx.add_cookies([{"name": "sessionid", "value": SID,
                          "domain": "127.0.0.1", "path": "/"}])
        page = ctx.new_page()
        base = f"http://127.0.0.1:{port}"

        for url in CASES:
            page.goto(base + "/statistics", wait_until="networkidle")
            page.wait_for_timeout(900)
            page.evaluate("() => { window.__marker = 'alive'; }")
            page.click(f"#lj-topnav a[href='{url}']", timeout=10000)
            page.wait_for_timeout(2600)
            alive = page.evaluate("() => window.__marker === 'alive'")
            lens = page.evaluate(
                "() => [...document.head.querySelectorAll('style')]"
                ".map(s => (s.textContent||'').replace(/\\s+/g,' ').trim().length)")
            print(f"  click {url:<14} softnav={alive} path={page.url.replace(base,'')} lens={lens}")

        # 顺时针：首页 -> 统计 -> 首页
        page.goto(base + "/", wait_until="networkidle")
        page.wait_for_timeout(1000)
        page.evaluate("() => { window.__marker = 'alive'; }")
        page.click("#lj-topnav a[href='/statistics']")
        page.wait_for_timeout(1800)
        a1 = page.evaluate("() => window.__marker === 'alive'")
        page.click(BRAND)
        page.wait_for_timeout(2000)
        a2 = page.evaluate("() => window.__marker === 'alive'")
        lens = page.evaluate(
            "() => [...document.head.querySelectorAll('style')]"
            ".map(s => (s.textContent||'').replace(/\\s+/g,' ').trim().length)")
        print(f"  home->stats softnav={a1}, stats->home softnav={a2}, lens={lens}")
        b.close()


if __name__ == "__main__":
    for port in (int(x) for x in sys.argv[1:]):
        run(port)
