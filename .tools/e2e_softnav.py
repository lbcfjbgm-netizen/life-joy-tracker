# -*- coding: utf-8 -*-
"""端到端实测：软导航回首页时页面内联样式是否还在。

A/B 对比（同一份脚本跑两台服务）：
  8001 = 线上活站，模板缓存还是旧的 → 复现 bug
  8011 = 临时实例，读盘读到新模板  → 验证修复
"""
import json
import pathlib
import sys
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent  # 脚本在 .tools 下，上一级即项目根
SID = (ROOT / ".tools" / "_sid.txt").read_text(encoding="utf-8").strip()
LOGS = ROOT / ".tools" / "logs"
LOGS.mkdir(parents=True, exist_ok=True)

PROBE = r"""
() => {
  const q = s => document.querySelector(s);
  const out = { headStyles: document.head.querySelectorAll('style').length,
                headLinks: document.head.querySelectorAll('link[rel="stylesheet"]').length,
                hasHm: !!q('.lj-hm'),
                title: document.title };
  const h1 = q('.lj-hm .h1');
  const cover = q('.lj-hm .now-cover');
  if (h1) { const cs = getComputedStyle(h1);
    out.h1 = { fontSize: cs.fontSize, letterSpacing: cs.letterSpacing,
               fontWeight: cs.fontWeight, color: cs.color,
               fontFamily: cs.fontFamily.split(',')[0].replace(/["']/g,'').trim() }; }
  if (cover) { const r = cover.getBoundingClientRect(); const cs = getComputedStyle(cover);
    out.cover = { w: Math.round(r.width), h: Math.round(r.height),
                  objectFit: cs.objectFit, radius: cs.borderRadius }; }
  out.bodyH = Math.round(document.documentElement.scrollHeight);
  out.styleLens = [...document.head.querySelectorAll('style')]
      .map(s => (s.textContent||'').replace(/\s+/g,' ').trim().length);
  return out;
}
"""


def measure(port, tag):
    r = {"port": port}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1600, "height": 1000})
        ctx.add_cookies([{"name": "sessionid", "value": SID,
                          "domain": "127.0.0.1", "path": "/"}])
        page = ctx.new_page()
        base = f"http://127.0.0.1:{port}"

        # ---- A) 整页直接打开首页 = 基线 ----
        page.goto(base + "/", wait_until="networkidle")
        page.wait_for_timeout(1200)
        full = page.evaluate(PROBE)
        r["full_load"] = full
        page.screenshot(path=str(LOGS / f"softnav-fix-{tag}-1-fullload.png"))

        # ---- B) 整页打开 /statistics → 点顶栏回首页（软导航，不刷新） ----
        page.goto(base + "/statistics", wait_until="networkidle")
        page.wait_for_timeout(1000)
        r["statistics_styles"] = page.evaluate("() => document.head.querySelectorAll('style').length")
        page.click("#lj-topnav a.lj-tn-brand", timeout=10000)
        page.wait_for_timeout(2500)
        soft = page.evaluate(PROBE)
        r["softnav"] = soft
        page.screenshot(path=str(LOGS / f"softnav-fix-{tag}-2-softnav.png"))

        # ---- C) 再来一轮，验重不会重复叠加 ----
        page.click("#lj-topnav a[href='/statistics']", timeout=10000)
        page.wait_for_timeout(1500)
        page.click("#lj-topnav a.lj-tn-brand", timeout=10000)
        page.wait_for_timeout(2000)
        r["softnav_round2"] = page.evaluate(PROBE)
        page.screenshot(path=str(LOGS / f"softnav-fix-{tag}-3-round2.png"))
        b.close()

    # 对比
    def cmp(field):
        a, c = full.get(field), soft.get(field)
        return {"field": field, "baseline": a, "softnav": c, "same": a == c}

    r["compare"] = [cmp("h1"), cmp("cover"), cmp("bodyH")]
    r["verdict"] = "PASS" if all(x["same"] for x in r["compare"]) else "FAIL"
    r["dup_check"] = {
        "headStyles_fullload": full.get("headStyles"),
        "headStyles_softnav": soft.get("headStyles"),
        "headStyles_round2": r["softnav_round2"].get("headStyles"),
        "no_duplicate": full.get("headStyles") == soft.get("headStyles")
                        == r["softnav_round2"].get("headStyles"),
    }
    return r


if __name__ == "__main__":
    ports = [int(x) for x in sys.argv[1:]] or [8001, 8011]
    out = {}
    for port in ports:
        try:
            out[str(port)] = measure(port, f"{port}")
        except Exception as e:
            out[str(port)] = {"error": f"{type(e).__name__}: {e}"}
    (LOGS / "softnav-e2e-result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
