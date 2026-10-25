"""把手札 JSON 回填进 app_aicopy.note。绝不碰 summary/quote/已有 batch。

用法（必须在 src 目录下跑）：
    cd src
    ..\\.venv\\Scripts\\python.exe ..\\.tools\\import_ai_note.py
可重复跑。已有行只更新 note；没有行的新建（batch='note'）。
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

SRC = r"C:\Users\USERNAME\Downloads\ui-redesign\quotes\batch-note.json"


def main():
    data = json.load(open(SRC, encoding="utf-8"))
    rows = data["items"]
    print(f"[LOAD] {os.path.basename(SRC)} 条数={len(rows)}")

    valid_ids = set(
        Item.objects.filter(pk__in=[r["item_id"] for r in rows]).values_list("pk", flat=True),
    )
    updated = created = skipped = 0
    for row in rows:
        item_id = row["item_id"]
        note = (row.get("note") or "").strip()
        if item_id not in valid_ids:
            skipped += 1
            print(f"  [SKIP] item_id={item_id} 不存在")
            continue
        obj = AICopy.objects.filter(item_id=item_id).first()
        if obj:
            obj.note = note
            obj.save(update_fields=["note"])
            updated += 1
        else:
            AICopy.objects.create(
                item_id=item_id,
                note=note,
                confidence=row.get("confidence", ""),
                batch="note",
            )
            created += 1

    print(f"[DONE] 更新已有 {updated} / 新建 {created} / 跳过 {skipped}")
    n_note = AICopy.objects.exclude(note="").count()
    print(f"[DB] note 非空的行数 = {n_note} / 总行数 = {AICopy.objects.count()}")
    by_batch = Counter(AICopy.objects.values_list("batch", flat=True))
    print(f"[DB] batch 分布 = {dict(by_batch)}")


if __name__ == "__main__":
    main()
