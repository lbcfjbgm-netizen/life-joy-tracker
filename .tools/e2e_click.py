# -*- coding: utf-8 -*-
"""真实点击验收（不注入 htmx.ajax）：顶栏点「统计」「日志」是不是样式也在。"""
import json
import pathlib
import sys
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent  # 脚本在 .tools 下，上一级即项目根
SID = (ROOT / ".tools" / "_sid.txt").read_text(encoding="utf-8").strip()
LOGS = ROOT / ".tools" / "logs"

HEAD = r"""() => ({
  headStyles: document.head.querySelectorAll('style').length,
  bodyH: Math.round(document.documentElement.scrollHeight),
  lens: [...document.head.querySelectorAll('style')]
          .map(s => (s.textContent||'').replace(/\s+/g,' ').trim().length)
})"""

CASES = [
    ("/statistics", r"""() => { const e=document.querySelector('.lj-stats');
        if(!e) return null; const c=getComputedStyle(e);
        return {pad:c.padding, h:Math.round(e.getBoundingClientRect().height)}; }"""),
    ("/journal", r"""() => { const e=document.querySelector('main');
        if(!e) return null; const c=getComputedStyle(e);
        return {h:Math.round(e.getBoundingClientRect().height), fs:c.fontSize}; }"""),
]


def run(port):
    out = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1600, "height": 1000})
        ctx.add_cookies([{"name": "sessionid", "value": SID,
                          "domain": "127.0.0.1", "path": "/"}])
        page = ctx.new_page()
        base = f"http://127.0.0.1:{port}"
        for url, probe in CASES:
            rec = {"url": url}
            page.goto(base + url, wait_until="networkidle")
            page.wait_for_timeout(1200)
            rec["full"] = {**page.evaluate(HEAD), "probe": page.evaluate(probe)}
            page.goto(base + "/", wait_until="networkidle")
            page.wait_for_timeout(1000)
            page.click(f"#lj-topnav a[href='{url}']", timeout=10000)
            page.wait_for_timeout(2600)
            rec["soft"] = {**page.evaluate(HEAD), "probe": page.evaluate(probe)}
            rec["ok"] = (rec["full"]["bodyH"] == rec["soft"]["bodyH"]
                         and rec["full"]["probe"] == rec["soft"]["probe"])
            page.screenshot(path=str(LOGS / f"softnav-click-{port}-{url.strip('/')}.png"))
            out.append(rec)
        b.close()
    return out


if __name__ == "__main__":
    port = int(sys.argv[1])
    res = run(port)
    print(f"===== port {port}")
    for r in res:
        print(f"  {r['url']:<14} ok={str(r['ok']):<5} bodyH {r['full']['bodyH']} -> "
              f"{r['soft']['bodyH']} | headStyles {r['full']['headStyles']} -> "
              f"{r['soft']['headStyles']} | probe_same="
              f"{r['full']['probe'] == r['soft']['probe']}")
