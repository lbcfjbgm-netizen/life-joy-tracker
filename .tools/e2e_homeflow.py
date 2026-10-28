# -*- coding: utf-8 -*-
"""从各个页面点「首页」回首页，检查内联样式是否还在。"""
import pathlib
import sys
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent  # 脚本在 .tools 下，上一级即项目根
SID = (ROOT / ".tools" / "_sid.txt").read_text(encoding="utf-8").strip()

PROBE = r"""
() => {
  const h1 = document.querySelector('.lj-hm .h1');
  const cover = document.querySelector('.lj-hm .now-cover');
  const out = { styles: [...document.head.querySelectorAll('style')]
      .map(s => (s.textContent||'').replace(/\s+/g,' ').trim().length),
    bodyH: Math.round(document.documentElement.scrollHeight),
    path: location.pathname };
  if (h1) out.h1 = getComputedStyle(h1).fontSize;
  if (cover) { const r = cover.getBoundingClientRect();
    out.cover = Math.round(r.width) + 'x' + Math.round(r.height); }
  return out;
}
"""

ENTRIES = ["/statistics", "/journal", "/games", "/year-in-review", "/books"]


def run(port):
    base = f"http://127.0.0.1:{port}"
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1600, "height": 1000})
        ctx.add_cookies([{"name": "sessionid", "value": SID,
                          "domain": "127.0.0.1", "path": "/"}])
        page = ctx.new_page()

        page.goto(base + "/", wait_until="networkidle")
        page.wait_for_timeout(1200)
        baseline = page.evaluate(PROBE)
        print(f"BASELINE  {baseline}")

        for entry in ENTRIES:
            resp = page.goto(base + entry, wait_until="networkidle")
            if resp is None or resp.status >= 400:
                print(f"  {entry} 跳过（状态 {resp.status if resp else '?'}）")
                continue
            page.wait_for_timeout(900)
            page.evaluate("() => { window.__marker = 'alive'; }")
            # 顶栏 LIFE JOY 标志 = 回首页
            page.click("#lj-topnav a.lj-tn-brand", timeout=10000)
            page.wait_for_timeout(2500)
            soft = page.evaluate("() => window.__marker === 'alive'")
            cur = page.evaluate(PROBE)
            ok = (cur.get("h1") == baseline.get("h1")
                  and cur.get("cover") == baseline.get("cover")
                  and cur.get("bodyH") == baseline.get("bodyH"))
            print(f"  {entry:<16} softnav={soft} h1={cur.get('h1')} "
                  f"cover={cur.get('cover')} bodyH={cur.get('bodyH')} "
                  f"styles={len(cur['styles'])} {'PASS' if ok else 'FAIL'}")
        b.close()


if __name__ == "__main__":
    for port in (int(x) for x in sys.argv[1:]):
        run(port)
