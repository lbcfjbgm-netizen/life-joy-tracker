# -*- coding: utf-8 -*-
"""判定「点击是否真的软导航」——用 performance.timeOrigin 判文档有没有被换掉。

timeOrigin 是浏览器给每个 document 打的出生时间，页面脚本改不了；
换了文档它必变，htmx 软导航（只换 #main-content）则保持不变。
"""
import pathlib
import sys
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent  # 脚本在 .tools 下，上一级即项目根
SID = (ROOT / ".tools" / "_sid.txt").read_text(encoding="utf-8").strip()

LENS = ("() => [...document.head.querySelectorAll('style')]"
        ".map(s => (s.textContent||'').replace(/\\s+/g,' ').trim().length)")


def run(port):
    print(f"===== port {port}")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1600, "height": 1000})
        ctx.add_cookies([{"name": "sessionid", "value": SID,
                          "domain": "127.0.0.1", "path": "/"}])
        page = ctx.new_page()
        base = f"http://127.0.0.1:{port}"

        for src, dst, sel in (
            ("/", "/statistics", "#lj-topnav a[href='/statistics']"),
            ("/", "/journal", "#lj-topnav a[href='/journal']"),
            ("/statistics", "/", "#lj-topnav a.lj-tn-brand"),
        ):
            page.goto(base + src, wait_until="networkidle")
            page.wait_for_timeout(1000)
            t0 = page.evaluate("() => performance.timeOrigin")
            l0 = page.evaluate(LENS)
            page.click(sel, timeout=10000)
            page.wait_for_timeout(2600)
            t1 = page.evaluate("() => performance.timeOrigin")
            l1 = page.evaluate(LENS)
            print(f"  {src} -> {dst}: sameDocument={t0 == t1} "
                  f"path={page.url.replace(base, '')} lens {l0} -> {l1}")
        b.close()


if __name__ == "__main__":
    for port in (int(x) for x in sys.argv[1:]):
        run(port)
