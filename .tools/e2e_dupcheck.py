# -*- coding: utf-8 -*-
"""收尾三项验收：
1) 反复软导航（首页→统计→首页→统计→首页）后，head 里的 style 不会越堆越多、base 自己那块不会重复
2) 新插入的 style 确实落在 main.css 那个 <link> 之前（层叠顺序没反）
3) 截图存证
"""
import json
import pathlib
import sys
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent  # 脚本在 .tools 下，上一级即项目根
SID = (ROOT / ".tools" / "_sid.txt").read_text(encoding="utf-8").strip()
LOGS = ROOT / ".tools" / "logs"

SNAP = r"""() => {
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  const kids = [...document.head.children];
  const mainIdx = kids.findIndex(el => el.tagName === 'LINK' &&
        /\/css\/main\.css/.test(el.getAttribute('href')||''));
  return {
    styles: [...document.head.querySelectorAll('style')].map((s,i) => {
      const t = norm(s.textContent);
      return { i, len: t.length, isSkipLink: t.includes('.skip-link'),
               isHome: t.includes('.lj-hm'), isStats: t.includes('.lj-stats'),
               beforeMainCss: mainIdx >= 0
                   ? kids.indexOf(s) < mainIdx : null };
    }),
    mainCssIdx: mainIdx,
    total: document.head.querySelectorAll('style').length,
    bodyH: Math.round(document.documentElement.scrollHeight)
  };
}"""


def run(port):
    out = {"port": port, "steps": []}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1600, "height": 1000})
        ctx.add_cookies([{"name": "sessionid", "value": SID,
                          "domain": "127.0.0.1", "path": "/"}])
        page = ctx.new_page()
        base = f"http://127.0.0.1:{port}"
        page.goto(base + "/", wait_until="networkidle")
        page.wait_for_timeout(1000)
        out["steps"].append({"step": "fullload /", **page.evaluate(SNAP)})

        for n in range(1, 4):
            page.click("#lj-topnav a[href='/statistics']")
            page.wait_for_timeout(1800)
            out["steps"].append({"step": f"softnav /statistics #{n}", **page.evaluate(SNAP)})
            page.click("#lj-topnav a.lj-tn-brand")
            page.wait_for_timeout(1800)
            out["steps"].append({"step": f"softnav / #{n}", **page.evaluate(SNAP)})

        page.screenshot(path=str(LOGS / f"softnav-fix-{port}-final-round3.png"))
        b.close()
    return out


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8011
    o = run(port)
    (LOGS / f"dupcheck-{port}.json").write_text(
        json.dumps(o, ensure_ascii=False, indent=2), encoding="utf-8")
    for s in o["steps"]:
        st = s["styles"]
        print(f"{s['step']:<26} total={s['total']} bodyH={s['bodyH']} "
              f"skipLinkBlocks={sum(1 for x in st if x['isSkipLink'])} "
              f"homeBlocks={sum(1 for x in st if x['isHome'])} "
              f"statsBlocks={sum(1 for x in st if x['isStats'])} "
              f"lens={[x['len'] for x in st]} "
              f"beforeMainCss={[x['beforeMainCss'] for x in st]}")
