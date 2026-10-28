# -*- coding: utf-8 -*-
"""诊断：软导航之后 head 里每个 style 到底是什么。"""
import json
import pathlib
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent  # 脚本在 .tools 下，上一级即项目根
SID = (ROOT / ".tools" / "_sid.txt").read_text(encoding="utf-8").strip()

DUMP = r"""
() => [...document.head.children].map((el, i) => {
  const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
  return { i, tag: el.tagName, len: t.length,
           head: t.slice(0, 70),
           tail: t.slice(-60),
           hasHmScoped: t.includes('.lj-hm'),
           hasSkipLink: t.includes('.skip-link'),
           hasHmHd: t.includes('.lj-hm .hd') };
})
"""

for port in (8001, 8011):
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1600, "height": 1000})
        ctx.add_cookies([{"name": "sessionid", "value": SID,
                          "domain": "127.0.0.1", "path": "/"}])
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
        page.wait_for_timeout(1200)
        full = page.evaluate(DUMP)
        page.goto(f"http://127.0.0.1:{port}/statistics", wait_until="networkidle")
        page.wait_for_timeout(800)
        page.click("#lj-topnav a.lj-tn-brand")
        page.wait_for_timeout(2500)
        soft = page.evaluate(DUMP)
        print(f"===== port {port}")
        print("  FULL LOAD head children:")
        for x in full:
            if x["tag"] == "STYLE":
                print("   ", json.dumps(x, ensure_ascii=False))
        print("  AFTER SOFTNAV head children:")
        for x in soft:
            if x["tag"] == "STYLE":
                print("   ", json.dumps(x, ensure_ascii=False))
        b.close()
