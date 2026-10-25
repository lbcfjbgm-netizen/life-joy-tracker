"""回填数据的质量体检：HTML 残留、日期合理性、类型名合法性、中文字段一致性。

从 yamtrack/ 根目录运行：`.venv\\Scripts\\python.exe scripts\\quality_check.py`
"""

import io
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 库路径按脚本位置推算，不受当前工作目录影响（原先写死相对路径，换目录就断）
DB_PATH = Path(__file__).resolve().parent.parent / "src" / "db" / "db.sqlite3"
c = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
CJK = re.compile(r"[\u4e00-\u9fff]")

print("== synopsis 质量 ==")
rows = c.execute("select media_id, synopsis from app_item where coalesce(synopsis,'')<>''").fetchall()
print("非空条数:", len(rows))
print("含 HTML 标签:", sum(1 for _, s in rows if re.search(r"<[^>]+>", s)))
print("含 &amp; 等实体:", sum(1 for _, s in rows if re.search(r"&[a-z]+;", s)))
print("超短(<10字):", sum(1 for _, s in rows if len(s.strip()) < 10))
print("最长:", max(len(s) for _, s in rows), "最短:", min(len(s) for _, s in rows))

print("\n== release_date 质量 ==")
dates = c.execute("select media_id, release_date from app_item where release_date is not null").fetchall()
print("非空:", len(dates))
years = [int(d[:4]) for _, d in dates]
print("年份范围:", min(years), "~", max(years))
print("晚于 2026:", [d for d in dates if int(d[1][:4]) > 2026][:5])
print("早于 1980:", [d for d in dates if int(d[1][:4]) < 1980][:5])
bad = [d for _, d in dates if not re.match(r"^\d{4}-\d{2}-\d{2}$", d)]
print("格式异常:", bad[:5])

print("\n== genres 质量 ==")
gs = c.execute("select media_id, genres from app_item where genres is not null and genres<>'[]'").fetchall()
print("非空:", len(gs))
import json

allnames = {}
for _, g in gs:
    try:
        arr = json.loads(g)
    except ValueError:
        print("无法解析:", g[:40])
        continue
    for name in arr:
        allnames[name] = allnames.get(name, 0) + 1
print("不同类型名数:", len(allnames))
print("前 12 个:", sorted(allnames.items(), key=lambda kv: -kv[1])[:12])
print("含英文名的类型:", [n for n in allnames if not CJK.search(n)][:10])
print("空数组条数:", c.execute("select count(*) from app_item where genres='[]'").fetchone()[0])

print("\n== title_zh 质量 ==")
tz = c.execute("select media_id, title, title_zh from app_item where coalesce(title_zh,'')<>''").fetchall()
print("非空:", len(tz), "全部含中文:", all(CJK.search(z) for _, _, z in tz))
print("title 与 title_zh 不同的条数:", sum(1 for _, t, z in tz if t != z))
print("样例:", tz[:5])

print("\n== title_en 质量 ==")
print(c.execute("select media_id, title, title_en from app_item where coalesce(title_en,'')<>''").fetchall())
