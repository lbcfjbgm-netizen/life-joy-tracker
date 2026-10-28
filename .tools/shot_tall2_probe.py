import time
import pathlib
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    n = 1300
    html = "<body style='margin:0;background:#111'>" + "".join(
        f"<div style='height:100px;background:hsl({i*7%360},60%,45%);color:#fff;font:16px sans-serif'>{i*100}px</div>"
        for i in range(n)) + "</body>"
    pg.set_content(html)
    t0 = time.time()
    pg.screenshot(path=(pathlib.Path(__file__).resolve().parent / "_shot_tall2.png"), full_page=True)
    print("tall2 ok", round(time.time()-t0,1), pg.evaluate("document.body.scrollHeight"))
    b.close()
from PIL import Image
im = Image.open((pathlib.Path(__file__).resolve().parent / "_shot_tall2.png"))
print("size", im.size, "px@y=120000", im.getpixel((10, 120000)) if im.size[1] > 120000 else "n/a")
print("px@bottom", im.getpixel((10, im.size[1]-2)))
