"""把 AI 金句 JSON 回填进 app_aicopy 表。

用法（必须在 src 目录下跑）：
    cd src
    ..\\.venv\\Scripts\\python.exe ..\\.tools\\import_ai_copy.py

可重复跑：以 item_id 为唯一键做 update_or_create，不会产生重复行。
只写 app_aicopy，绝不碰 app_highlight / app_item / app_game。
"""

import json
import os
import sys
from collections import Counter

SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
)
sys.path.insert(0, SRC_DIR)
os.chdir(SRC_DIR)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from app.models import AICopy, Item  # noqa: E402

# 素材目录在仓库根上一级的 <life joy>\..\ui-redesign\quotes
REPO_ROOT = os.path.dirname(SRC_DIR)                       # ...\yamtrack
PROJECT_ROOT = os.path.dirname(REPO_ROOT)                  # ...\life joy
_DOWNLOADS = os.path.dirname(PROJECT_ROOT)                 # ...\Downloads
_QUOTES = os.path.join(_DOWNLOADS, "ui-redesign", "quotes")


def _q(name):
    return os.path.join(_QUOTES, name)


SOURCES = [
    (_q("pilot-batch.json"), "pilot"),
    (_q("batch-002.json"), "002"),
    (_q("batch-003.json"), "003"),
    (_q("batch-004.json"), "004"),
    (_q("batch-005.json"), "005"),
    (_q("batch-006.json"), "006"),
    (_q("batch-007.json"), "007"),
]

MAX_LEN = 200


def load(path, batch):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    items = data.get("items") or []
    print(f"[LOAD] {os.path.basename(path)} batch={batch} 条数={len(items)}")
    for row in items:
        row["_batch"] = batch
        row["_file"] = os.path.basename(path)
    return items


def main():
    rows = []
    for path, batch in SOURCES:
        rows.extend(load(path, batch))

    all_items = {r["item_id"]: r for r in rows if r.get("item_id") is not None}
    dup_in_json = len(rows) - len(all_items)

    existing_ids = set(
        Item.objects.filter(pk__in=all_items.keys()).values_list("pk", flat=True),
    )

    created = 0
    updated = 0
    skipped = []
    truncated = []
    seen = set()

    for item_id, row in all_items.items():
        if item_id in seen:
            skipped.append((item_id, "JSON 内重复出现（已保留后一条）"))
            continue
        seen.add(item_id)

        if item_id not in existing_ids:
            skipped.append((item_id, "app_item 里没有这个 item_id"))
            continue

        summary = (row.get("summary") or "").strip()
        quote = (row.get("quote") or "").strip()
        confidence = (row.get("confidence") or "").strip()

        for field, val in (("summary", summary), ("quote", quote)):
            if len(val) > MAX_LEN:
                truncated.append((item_id, field, len(val)))
                if field == "summary":
                    summary = summary[:MAX_LEN]
                else:
                    quote = quote[:MAX_LEN]

        _, was_created = AICopy.objects.update_or_create(
            item_id=item_id,
            defaults={
                "summary": summary,
                "quote": quote,
                "confidence": confidence,
                "batch": row["_batch"],
                "reviewed": False,
            },
        )
        if was_created:
            created += 1
        else:
            updated += 1

    print()
    print(f"[DONE] 新建 {created} / 更新 {updated} / 跳过 {len(skipped)}")
    if dup_in_json:
        print(f"[NOTE] JSON 内重复 item_id 共 {dup_in_json} 条")
    if truncated:
        print(f"[WARN] 超长被截断 {len(truncated)} 处: {truncated[:10]}")
    if skipped:
        reasons = Counter(reason for _, reason in skipped)
        for reason, count in reasons.items():
            print(f"  - {reason}: {count}")
        for item_id, reason in skipped[:20]:
            print(f"    item_id={item_id} -> {reason}")

    by_batch = Counter(AICopy.objects.values_list("batch", flat=True))
    print(f"[DB] app_aicopy 总行数 = {AICopy.objects.count()}  分布 = {dict(by_batch)}")


if __name__ == "__main__":
    main()
