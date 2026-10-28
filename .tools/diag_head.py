# -*- coding: utf-8 -*-
"""诊断 2：用 MutationObserver 看软导航瞬间 head 里到底发生了什么。"""
import json
import pathlib
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent  # 脚本在 .tools 下，上一级即项目根
SID = (ROOT / ".tools" / "_sid.txt").read_text(encoding="utf-8").strip()
LOGS = ROOT / ".tools" / "logs"

INSTALL = r"""
() => {
  window.__log = [];
  // 给现有每个 style 打标，便于追踪是不是同一个节点
  [...document.head.querySelectorAll('style')].forEach((s, i) => {
    s.setAttribute('data-probe', 'orig-' + i);
  });
  const mo = new MutationObserver(recs => {
    recs.forEach(r => {
      window.__log.push({
        type: r.type,
        target: r.target.nodeType === 1
            ? (r.target.tagName + (r.target.getAttribute && r.target.getAttribute('data-probe') ? '#' + r.target.getAttribute('data-probe') : ''))
            : ('text-node parent=' + (r.target.parentNode && r.target.parentNode.tagName)),
        added: [...r.addedNodes].map(n => n.nodeType === 1 ? n.tagName + ':' + (n.textContent||'').trim().length : '#text'),
        removed: [...r.removedNodes].map(n => n.nodeType === 1 ? n.tagName + ':' + (n.textContent||'').trim().slice(0,30) : '#text'),
      });
    });
  });
  mo.observe(document.head, { childList: true, characterData: true, subtree: true });
  return true;
}
"""

SNAP = r"""
() => [...document.head.children]
  .map((el, i) => ({ i, tag: el.tagName,
                     probe: el.getAttribute && el.getAttribute('data-probe'),
                     len: (el.textContent||'').trim().length,
                     head: (el.textContent||'').replace(/\s+/g,' ').trim().slice(0,55) }))
  .filter(x => x.tag === 'STYLE')
"""

for port in (8001, 8011):
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1600, "height": 1000})
        ctx.add_cookies([{"name": "sessionid", "value": SID,
                          "domain": "127.0.0.1", "path": "/"}])
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/statistics", wait_until="networkidle")
        page.wait_for_timeout(900)
        page.evaluate(INSTALL)
        before = page.evaluate(SNAP)
        page.click("#lj-topnav a.lj-tn-brand")
        page.wait_for_timeout(2500)
        after = page.evaluate(SNAP)
        log = page.evaluate("() => window.__log")
        out = {"port": port, "before": before, "after": after, "mutations": log}
        (LOGS / f"head-diag-{port}.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"===== port {port}")
        print("BEFORE:")
        for x in before:
            print("  ", json.dumps(x, ensure_ascii=False))
        print("AFTER:")
        for x in after:
            print("  ", json.dumps(x, ensure_ascii=False))
        print(f"MUTATIONS ({len(log)}):")
        for m in log:
            print("  ", json.dumps(m, ensure_ascii=False))
        b.close()
