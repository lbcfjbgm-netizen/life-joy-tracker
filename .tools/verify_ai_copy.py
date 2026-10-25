"""验证 app_aicopy 的导入结果，并与 JSON 原文逐字对照。"""

import json
import os
import random
import sys

SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
)
sys.path.insert(0, SRC_DIR)
os.chdir(SRC_DIR)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from app.models import AICopy  # noqa: E402

SOURCES = [
    (r"C:\Users\USERNAME\Downloads\ui-redesign\quotes\pilot-batch.json", "pilot"),
    (r"C:\Users\USERNAME\Downloads\ui-redesign\quotes\batch-002.json", "002"),
]

source = {}
for path, batch in SOURCES:
    with open(path, encoding="utf-8") as fh:
        for row in json.load(fh).get("items") or []:
            source[row["item_id"]] = {
                "summary": (row.get("summary") or "").strip(),
                "quote": (row.get("quote") or "").strip(),
                "confidence": (row.get("confidence") or "").strip(),
                "batch": batch,
                "title": row.get("title", ""),
            }

print("=== 1) 总数 ===")
print("AICopy.objects.count() =", AICopy.objects.count())
print("JSON 去重后条数 =", len(source))

print()
print("=== 2) 全量逐字比对（summary / quote / confidence / batch）===")
mismatch = []
missing = []
for item_id, expect in source.items():
    obj = AICopy.objects.filter(pk=item_id).first()
    if obj is None:
        missing.append(item_id)
        continue
    got = {
        "summary": obj.summary,
        "quote": obj.quote,
        "confidence": obj.confidence,
        "batch": obj.batch,
    }
    for field in got:
        if got[field] != expect[field]:
            mismatch.append((item_id, field, expect[field], got[field]))

print("缺失行数 =", len(missing), missing[:10])
print("字段不一致 =", len(mismatch))
for row in mismatch[:10]:
    print("  ", row)

print()
print("=== 3) 随机抽 5 条 ===")
random.seed(20261024)
ids = random.sample(sorted(source), 5)
for item_id in ids:
    obj = AICopy.objects.select_related("item").get(pk=item_id)
    expect = source[item_id]
    ok = (
        obj.summary == expect["summary"]
        and obj.quote == expect["quote"]
        and obj.batch == expect["batch"]
    )
    print(f"item_id={item_id} batch={obj.batch} 一致={ok}")
    print(f"  title   = {obj.item.title}")
    print(f"  summary = {obj.summary}")
    print(f"  quote   = {obj.quote}")
    print(f"  confidence = {obj.confidence!r} reviewed={obj.reviewed}")
