# -*- coding: utf-8 -*-
"""覆盖剩余带内联 style 的页面：year_in_review / near_complete / 媒体库列表。"""
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

NAV = """(u) => htmx.ajax('GET', u, {target: '#main-content', select: '#main-content',
       swap: 'outerHTML show:window:top', headers: {'X-Soft-Navigation': 'true'}})"""

TARGETS = [
    "/year-in-review",
    "/near-complete",
    "/admin/movie",
    "/admin/tv",
    "/admin/book",
    "/admin/game",
]


def run(port):
    res = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1600, "height": 1000})
        ctx.add_cookies([{"name": "sessionid", "value": SID,
                          "domain": "127.0.0.1", "path": "/"}])
        page = ctx.new_page()
        base = f"http://127.0.0.1:{port}"
        for url in TARGETS:
            rec = {"url": url}
            try:
                page.goto(base + url, wait_until="networkidle")
                page.wait_for_timeout(1500)
                rec["full"] = page.evaluate(HEAD)
                page.goto(base + "/", wait_until="networkidle")
                page.wait_for_timeout(1000)
                page.evaluate(NAV, url)
                page.wait_for_timeout(3000)
                rec["soft"] = page.evaluate(HEAD)
                rec["ok"] = rec["full"]["bodyH"] == rec["soft"]["bodyH"]
                page.screenshot(path=str(LOGS / f"softnav-extra-{port}-"
                                            f"{url.strip('/').replace('/', '_')}.png"))
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"
                rec["ok"] = False
            res.append(rec)
        b.close()
    return res


if __name__ == "__main__":
    port = int(sys.argv[1])
    out = run(port)
    (LOGS / f"extra-{port}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"===== port {port}")
    for r in out:
        f, s = r.get("full", {}), r.get("soft", {})
        print(f"  {r['url']:<16} ok={str(r.get('ok')):<5} bodyH {f.get('bodyH')} -> "
              f"{s.get('bodyH')} | headStyles {f.get('headStyles')} -> {s.get('headStyles')} "
              f"{r.get('error','')}")
