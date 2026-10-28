# -*- coding: utf-8 -*-
"""真正的「软导航进各页」验收。

流程（每个页面都一样）：
  A) 整页直接打开目标页 → 记基线
  B) 回首页（整页），再用 htmx.ajax 发起与顶栏点击完全相同的软导航请求
     （target=#main-content, select=#main-content, X-Soft-Navigation: true）
     → 不刷新页面，记同样的指标
两者必须一致；head 里 style 允许更多（累积是正常的，因为换页不删旧样式）。
"""
import json
import pathlib
import sys
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent  # 脚本在 .tools 下，上一级即项目根
SID = (ROOT / ".tools" / "_sid.txt").read_text(encoding="utf-8").strip()
LOGS = ROOT / ".tools" / "logs"

HEAD = r"""() => ({
  headStyles: document.head.querySelectorAll('style').length,
  headLinks: document.head.querySelectorAll('link[rel="stylesheet"]').length,
  bodyH: Math.round(document.documentElement.scrollHeight),
  lens: [...document.head.querySelectorAll('style')]
          .map(s => (s.textContent||'').replace(/\s+/g,' ').trim().length)
})"""

NAV = """(u) => htmx.ajax('GET', u, {target: '#main-content', select: '#main-content',
       swap: 'outerHTML show:window:top', headers: {'X-Soft-Navigation': 'true'}})"""

DETAIL = {}
p = LOGS / "detail_urls.txt"
if p.exists():
    for line in p.read_text(encoding="utf-8").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            DETAIL[k.strip()] = v.strip()

TARGETS = [
    ("/", "home", r"""() => { const e=document.querySelector('.lj-hm .now-cover');
        if(!e) return null; const c=getComputedStyle(e); const r=e.getBoundingClientRect();
        return {w:Math.round(r.width), h:Math.round(r.height), fit:c.objectFit}; }"""),
    ("/statistics", "statistics", r"""() => { const e=document.querySelector('.lj-stats');
        if(!e) return null; const c=getComputedStyle(e);
        return {pad:c.padding, h:Math.round(e.getBoundingClientRect().height)}; }"""),
    ("/journal", "journal", r"""() => { const e=document.querySelector('main');
        if(!e) return null; const c=getComputedStyle(e);
        return {h:Math.round(e.getBoundingClientRect().height), fs:c.fontSize}; }"""),
]
PROBE_DETAIL = r"""() => { const e=document.querySelector('main');
    if(!e) return null; const c=getComputedStyle(e);
    return {h:Math.round(e.getBoundingClientRect().height), fs:c.fontSize}; }"""
for k in ("movie", "tv", "book", "game"):
    if k in DETAIL:
        TARGETS.append((DETAIL[k], f"detail_{k}", PROBE_DETAIL))


def run(port):
    res = []
    with sync_playwright() as p2:
        b = p2.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1600, "height": 1000})
        ctx.add_cookies([{"name": "sessionid", "value": SID,
                          "domain": "127.0.0.1", "path": "/"}])
        page = ctx.new_page()
        base = f"http://127.0.0.1:{port}"
        for url, name, probe in TARGETS:
            rec = {"name": name, "url": url}
            try:
                page.goto(base + url, wait_until="networkidle")
                page.wait_for_timeout(1200)
                rec["full"] = {**page.evaluate(HEAD), "probe": page.evaluate(probe)}
                page.goto(base + "/", wait_until="networkidle")
                page.wait_for_timeout(1000)
                page.evaluate(NAV, url)
                page.wait_for_timeout(2600)
                rec["soft"] = {**page.evaluate(HEAD), "probe": page.evaluate(probe)}
                rec["ok"] = (rec["full"]["bodyH"] == rec["soft"]["bodyH"]
                             and rec["full"]["probe"] == rec["soft"]["probe"])
                page.screenshot(path=str(LOGS / f"softnav-multi-{port}-{name}.png"))
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"
                rec["ok"] = False
            res.append(rec)
        b.close()
    return res


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8011
    out = run(port)
    (LOGS / f"multipage-{port}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"===== port {port}")
    for r in out:
        print(f"{r['name']:<14} ok={str(r.get('ok')):<5} "
              f"bodyH {r.get('full',{}).get('bodyH')} -> {r.get('soft',{}).get('bodyH')} | "
              f"headStyles {r.get('full',{}).get('headStyles')} -> {r.get('soft',{}).get('headStyles')} | "
              f"probe_same={r.get('full',{}).get('probe') == r.get('soft',{}).get('probe')} "
              f"{r.get('error','')}")
