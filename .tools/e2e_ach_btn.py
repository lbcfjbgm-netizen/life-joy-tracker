"""点一次「刷新成就」按钮，看三态是否真的工作（主对话验收用，只读不改库）。"""

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8001"
URL = f"{BASE}/details/steam/game/4267800/x"

with open(".tools/_sid.txt", encoding="utf-8") as f:
    sid = f.readline().strip()

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    ctx.add_cookies(
        [{"name": "sessionid", "value": sid, "domain": "127.0.0.1", "path": "/"}]
    )
    page = ctx.new_page()
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(800)

    btn = page.locator(".m-sync-btn")
    print("按钮个数:", btn.count())
    print("按钮文字:", btn.first.inner_text().replace("\n", " ").strip())

    card = page.locator("#achievement-card")
    print("点前卡片:", card.inner_text().replace("\n", " | "))

    page.screenshot(path=".tools/logs/ach_btn_before.png", clip={
        "x": 640, "y": 340, "width": 420, "height": 160,
    })

    # 拦一下网络，看它到底打了几次同步接口
    hits = []
    page.on("request", lambda r: hits.append(r.url) if "sync-achievements" in r.url else None)

    btn.first.click()
    page.wait_for_timeout(3500)

    print("同步接口调用次数:", len(hits))
    print("点后卡片:", page.locator("#achievement-card").inner_text().replace("\n", " | "))
    page.screenshot(path=".tools/logs/ach_btn_after.png", clip={
        "x": 640, "y": 340, "width": 420, "height": 160,
    })

    # 立刻再点一次，验证节流
    if page.locator(".m-sync-btn").count():
        page.locator(".m-sync-btn").first.click()
        page.wait_for_timeout(2500)
        print("连点后卡片:", page.locator("#achievement-card").inner_text().replace("\n", " | "))
        page.screenshot(path=".tools/logs/ach_btn_throttle.png", clip={
            "x": 640, "y": 340, "width": 420, "height": 160,
        })

    browser.close()
