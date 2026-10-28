import time
import pathlib
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    # synthetic page 52000px tall with blocks so we can see seams
    html = "<body style='margin:0;background:#111'>" + "".join(
        f"<div style='height:100px;background:hsl({i*7%360},60%,45%);color:#fff;font:16px sans-serif'>{i*100}px</div>"
        for i in range(520)) + "</body>"
    pg.set_content(html)
    t0 = time.time()
    pg.screenshot(path=(pathlib.Path(__file__).resolve().parent / "_shot_tall.png"), full_page=True)
    print("tall ok", round(time.time()-t0,1), pg.evaluate("document.body.scrollHeight"))
    b.close()
from PIL import Image
im = Image.open((pathlib.Path(__file__).resolve().parent / "_shot_tall.png"))
print("size", im.size)
