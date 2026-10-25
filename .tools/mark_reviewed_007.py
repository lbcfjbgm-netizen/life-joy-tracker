"""审核收尾：batch-007 审过的条目标 reviewed=True（只动 007 批，走 ORM）。"""
import json, os, sys

SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC_DIR)
os.chdir(SRC_DIR)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django
django.setup()

from app.models import AICopy

patches = json.load(open(r"C:\Users\USERNAME\Downloads\ui-redesign\quotes\audit-007-patches.json", encoding="utf-8"))
# 留在数据里的 = pass + rewrite + downgrade（drop=0 条）
keep_ids = list(patches["pass"]) + [x["item_id"] for x in patches["rewrite"]] + [x["item_id"] for x in patches["downgrade"]]
drop_ids = [x["item_id"] for x in patches["drop"]]
print(f"补丁：保留 {len(keep_ids)} 条 / drop {len(drop_ids)} 条")

if drop_ids:
    deleted, _ = AICopy.objects.filter(item_id__in=drop_ids, batch="007").delete()
    print(f"[DB] 删除 app_aicopy 中 drop 条目：{deleted} 行")
else:
    print("[DB] 无 drop 条目，跳过删除")

updated = AICopy.objects.filter(item_id__in=keep_ids, batch="007").update(reviewed=True)
print(f"[DB] reviewed=True 更新 {updated} 行")

total = AICopy.objects.count()
rev = AICopy.objects.filter(reviewed=True).count()
b7 = AICopy.objects.filter(batch="007").count()
b7_rev = AICopy.objects.filter(batch="007", reviewed=True).count()
print(f"[核对] app_aicopy 总行数={total}  reviewed=True={rev}")
print(f"[核对] 007 批行数={b7}  007 批 reviewed=True={b7_rev}")
