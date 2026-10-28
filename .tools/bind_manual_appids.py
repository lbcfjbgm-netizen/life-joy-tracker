"""给手动条目补 Steam 竖版封面。

只做一件事：把 Steam 商店的竖版封面 URL 写进 app_item.image。
绝不改 media_id / source（那会撞唯一索引 unique_item_without_season_episode）。

封面优先级：library_600x900.jpg（竖版）> header.jpg（横版）> library_hero.jpg（横版大图）。
每个候选都先探测 HTTP 状态，200 才用，不写死。

默认 --dry-run 只打印；加 --apply 才写库。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

import requests  # noqa: E402

from app.models import Item  # noqa: E402

# 手动条目 id -> Steam appid
MANUAL_APPIDS = {
    10408: "2689210",  # 女神异闻录3重置版dlc
    10410: "1610440",  # 沉默意志
}

# 只许用这两个域名（本机实测 akamai / cloudflare 全超时）
HOSTS = (
    "https://media.steampowered.com/steam/apps",
    "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps",
)

# 竖版优先，其次横版
SUFFIXES = (
    ("library_600x900.jpg", "竖版"),
    ("header.jpg", "横版"),
    ("library_hero.jpg", "横版大图"),
)

LOG_DIR = PROJECT_ROOT / ".tools" / "logs"
REPORT_PATH = LOG_DIR / "bind_manual_appids.json"
TIMEOUT = 25


def _proxy_from_env() -> dict[str, str] | None:
    """从环境变量取本机代理；没设就不走代理。"""
    https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not https_proxy:
        return None
    return {"http": https_proxy, "https": https_proxy}


def probe(url: str, proxies: dict[str, str] | None) -> tuple[int, int]:
    """探测一个图片地址，返回 (HTTP 状态码, 字节数)。

    本机到 media.steampowered.com 的 TLS 握手偶发失败（代理链路抖动），
    所以同一个地址最多试 3 次，避免把「网络抖一下」误判成「图片不存在」。
    """
    for _ in range(3):
        try:
            response = requests.get(
                url,
                timeout=TIMEOUT,
                stream=True,
                proxies=proxies,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            status = response.status_code
            size = len(response.content) if status == 200 else 0
            response.close()
            if status == 200:
                return status, size
            if status in (403, 404):
                # 明确不存在，不用重试
                return status, size
        except requests.RequestException:
            continue
    return 0, 0


def pick_cover(appid: str, proxies: dict[str, str] | None) -> dict:
    """按优先级探测，返回选中结果和每个候选的探测记录。"""
    attempts = []
    for suffix, label in SUFFIXES:
        for host in HOSTS:
            url = f"{host}/{appid}/{suffix}"
            status, size = probe(url, proxies)
            attempts.append({"url": url, "status": status, "bytes": size})
            if status == 200 and size > 1000:
                return {"url": url, "label": label, "attempts": attempts}
    return {"url": None, "label": None, "attempts": attempts}


def main() -> int:
    parser = argparse.ArgumentParser(description="给手动条目补 Steam 封面")
    parser.add_argument("--apply", action="store_true", help="真的写库")
    args = parser.parse_args()

    proxies = _proxy_from_env()
    print(f"代理：{proxies['https'] if proxies else '未使用（直连）'}")
    print(f"模式：{'APPLY（会写库）' if args.apply else 'DRY-RUN（只打印）'}")

    report = {"created_at": datetime.now().isoformat(timespec="seconds"), "items": []}
    changed = 0

    for item_id, appid in MANUAL_APPIDS.items():
        item = Item.objects.filter(id=item_id, media_type="game").first()
        print("=" * 72)
        if item is None:
            print(f"[跳过] item {item_id} 不存在")
            report["items"].append({"id": item_id, "appid": appid, "result": "条目不存在"})
            continue

        before = item.image or ""
        print(f"[{item.id}] {item.title}  appid={appid}")
        print(f"  当前封面：{before or '（空）'}")

        picked = pick_cover(appid, proxies)
        for attempt in picked["attempts"]:
            state = "OK" if attempt["status"] == 200 else "失败"
            print(f"    探测 {attempt['url'].split('/', 3)[-1]} -> {attempt['status']} {state} {attempt['bytes']}B")

        if not picked["url"]:
            print("  结论：三种尺寸都拿不到，保持空白封面，不改 image")
            report["items"].append(
                {"id": item.id, "title": item.title, "appid": appid, "result": "无可用图", "before": before},
            )
            continue

        after = picked["url"]
        print(f"  选中：{picked['label']} {after}")

        if before == after:
            print("  结论：已是最新，跳过")
            report["items"].append(
                {
                    "id": item.id,
                    "title": item.title,
                    "appid": appid,
                    "result": "已是最新",
                    "before": before,
                    "after": after,
                    "kind": picked["label"],
                    "attempts": picked["attempts"],
                },
            )
            continue

        if len(after) > Item._meta.get_field("image").max_length:
            print("  结论：URL 超过 image 字段长度上限，跳过")
            report["items"].append(
                {"id": item.id, "title": item.title, "appid": appid, "result": "URL 过长", "before": before},
            )
            continue

        if args.apply:
            item.image = after
            item.save(update_fields=["image"])
            print("  已写入数据库")
            changed += 1
        else:
            print("  （dry-run，未写入）")

        report["items"].append(
            {
                "id": item.id,
                "title": item.title,
                "appid": appid,
                "result": "已更新" if args.apply else "待更新",
                "before": before,
                "after": after,
                "kind": picked["label"],
                "attempts": picked["attempts"],
            },
        )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 72)
    print(f"报告已写入：{REPORT_PATH}")
    print(f"本次实际改动：{changed} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
