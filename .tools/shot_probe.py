import sys, time, json, pathlib
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8012"
SID = open((pathlib.Path(__file__).resolve().parent / "_sid.txt"), encoding="utf-8").read().strip().splitlines()[0]
url = sys.argv[1]
out = sys.argv[2]

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1,
                        extra_http_headers={"Cookie": f"sessionid={SID}"})
    pg = ctx.new_page()
    t0 = time.time()
    pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(2500)
    # scroll through to trigger lazy images
    h = pg.evaluate("document.body.scrollHeight")
    y = 0
    while y < h:
        pg.mouse.wheel(0, 900); y += 900; pg.wait_for_timeout(60)
        h = pg.evaluate("document.body.scrollHeight")
    pg.evaluate("window.scrollTo(0,0)")
    pg.wait_for_timeout(600)
    info = pg.evaluate("""() => {
        const imgs = [...document.images].map(i => ({src: i.currentSrc || i.src, nw: i.naturalWidth, nh: i.naturalHeight, w: i.width, h: i.height, loading: i.loading}));
        return {bodyH: document.body.scrollHeight, docH: document.documentElement.scrollHeight,
                title: document.title, url: location.href, n: imgs.length,
                bad: imgs.filter(i => i.nw === 0), imgs: imgs.slice(0, 40)};
    }""")
    print(json.dumps({"elapsed": round(time.time()-t0, 1), **info}, ensure_ascii=False, indent=1)[:4000])
    pg.screenshot(path=out, full_page=True)
    print("shot done", round(time.time()-t0,1))
    b.close()
