# -*- coding: utf-8 -*-
"""最终存证：首页软导航 A/B + 控制台报错 + 去重 + 插入位置 + 截图。

用法: python .tools/e2e_final.py 8001 8011
"""
import json
import pathlib
import sys
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent  # 脚本在 .tools 下，上一级即项目根
SID = (ROOT / ".tools" / "_sid.txt").read_text(encoding="utf-8").strip()
LOGS = ROOT / ".tools" / "logs"

PROBE = r"""() => {
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  const q = s => document.querySelector(s);
  const kids = [...document.head.children];
  const mainIdx = kids.findIndex(el => el.tagName === 'LINK' &&
        /\/css\/main\.css/.test(el.getAttribute('href')||''));
  const out = {
    headStyles: document.head.querySelectorAll('style').length,
    styleLens: [...document.head.querySelectorAll('style')].map(s => norm(s.textContent).length),
    skipLinkBlocks: [...document.head.querySelectorAll('style')]
        .filter(s => norm(s.textContent).includes('.skip-link')).length,
    homeBlocks: [...document.head.querySelectorAll('style')]
        .filter(s => norm(s.textContent).includes('.lj-hm')).length,
    beforeMainCss: [...document.head.querySelectorAll('style')]
        .map(s => mainIdx >= 0 ? kids.indexOf(s) < mainIdx : null),
    bodyH: Math.round(document.documentElement.scrollHeight),
    hasHm: !!q('.lj-hm'),
  };
  const h1 = q('.lj-hm .h1');
  if (h1) { const c = getComputedStyle(h1);
    out.h1 = { fontSize: c.fontSize, letterSpacing: c.letterSpacing,
               fontWeight: c.fontWeight, color: c.color,
               fontFamily: c.fontFamily.split(',')[0].replace(/["']/g,'').trim() }; }
  const cov = q('.lj-hm .now-cover') || q('.lj-hm img');
  if (cov) { const c = getComputedStyle(cov); const r = cov.getBoundingClientRect();
    out.cover = { w: Math.round(r.width), h: Math.round(r.height),
                  objectFit: c.objectFit, radius: c.borderRadius }; }
  return out;
}"""


def measure(port, tag):
    r = {"port": port, "consoleErrors": [], "pageErrors": []}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1600, "height": 1000})
        ctx.add_cookies([{"name": "sessionid", "value": SID,
                          "domain": "127.0.0.1", "path": "/"}])
        page = ctx.new_page()
        page.on("console", lambda m: r["consoleErrors"].append(m.text[:200])
                if m.type == "error" else None)
        page.on("pageerror", lambda e: r["pageErrors"].append(str(e)[:200]))
        base = f"http://127.0.0.1:{port}"

        # A) 整页直接打开首页 = 基线
        page.goto(base + "/", wait_until="networkidle")
        page.wait_for_timeout(1200)
        r["full_load"] = page.evaluate(PROBE)
        page.screenshot(path=str(LOGS / f"softnav-fix-{tag}-1-fullload.png"))

        # B) 整页打开 /statistics → 点顶栏 logo 回首页（真·点击，不刷新）
        page.goto(base + "/statistics", wait_until="networkidle")
        page.wait_for_timeout(1000)
        page.click("#lj-topnav a.lj-tn-brand", timeout=10000)
        page.wait_for_timeout(2500)
        r["softnav"] = page.evaluate(PROBE)
        page.screenshot(path=str(LOGS / f"softnav-fix-{tag}-2-softnav.png"))

        # C) 再来两轮，验重复叠加
        for n in (2, 3):
            page.click("#lj-topnav a[href='/statistics']")
            page.wait_for_timeout(1500)
            page.click("#lj-topnav a.lj-tn-brand")
            page.wait_for_timeout(2000)
            r[f"softnav_round{n}"] = page.evaluate(PROBE)
        page.screenshot(path=str(LOGS / f"softnav-fix-{tag}-3-round3.png"))
        b.close()

    f, s = r["full_load"], r["softnav"]
    r["compare"] = {
        "h1_same": f.get("h1") == s.get("h1"),
        "cover_same": f.get("cover") == s.get("cover"),
        "bodyH_same": f.get("bodyH") == s.get("bodyH"),
    }
    r["verdict"] = "PASS" if all(r["compare"].values()) else "FAIL"
    r["dedup_ok"] = (f["skipLinkBlocks"] == s["skipLinkBlocks"]
                     == r["softnav_round3"]["skipLinkBlocks"])
    r["no_growth"] = (s["headStyles"] == r["softnav_round2"]["headStyles"]
                      == r["softnav_round3"]["headStyles"])
    return r


if __name__ == "__main__":
    ports = [int(x) for x in sys.argv[1:]] or [8001, 8011]
    out = {}
    for port in ports:
        try:
            out[str(port)] = measure(port, str(port))
        except Exception as e:
            out[str(port)] = {"error": f"{type(e).__name__}: {e}"}
    (LOGS / "softnav-final.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    for port, o in out.items():
        if "error" in o:
            print(f"port {port}: ERROR {o['error']}")
            continue
        f, s = o["full_load"], o["softnav"]
        print(f"===== port {port}  verdict={o['verdict']}  dedup_ok={o['dedup_ok']}  "
              f"no_growth={o['no_growth']}")
        print(f"  full  h1={f.get('h1')}  cover={f.get('cover')}  bodyH={f.get('bodyH')}")
        print(f"  soft  h1={s.get('h1')}  cover={s.get('cover')}  bodyH={s.get('bodyH')}")
        print(f"  compare={o['compare']}")
        print(f"  headStyles full={f['headStyles']} soft={s['headStyles']} "
              f"r2={o['softnav_round2']['headStyles']} r3={o['softnav_round3']['headStyles']}")
        print(f"  styleLens full={f['styleLens']} soft={s['styleLens']}")
        print(f"  skipLinkBlocks full={f['skipLinkBlocks']} soft={s['skipLinkBlocks']} "
              f"r3={o['softnav_round3']['skipLinkBlocks']} | "
              f"homeBlocks full={f['homeBlocks']} soft={s['homeBlocks']} "
              f"r3={o['softnav_round3']['homeBlocks']}")
        print(f"  beforeMainCss full={f['beforeMainCss']} soft={s['beforeMainCss']}")
        print(f"  pageErrors={o['pageErrors']}  consoleErrors={o['consoleErrors'][:3]}")
