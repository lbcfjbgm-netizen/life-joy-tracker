import sqlite3
cur = sqlite3.connect(r"C:\Users\USERNAME\Downloads\life joy\yamtrack\src\db\db.sqlite3", timeout=30)
bak = sqlite3.connect(r"C:\Users\USERNAME\Downloads\life joy\yamtrack\.tools\backups\db-before-store-meta-20261022-080200.sqlite3", timeout=30)
q = "select media_id, title, synopsis, image from app_item where source='steam'"
b = {r[0]: r for r in bak.execute(q)}
c = {r[0]: r for r in cur.execute(q)}
print("counts", len(b), len(c))
over = [k for k in b if (b[k][2] or '').strip() and (b[k][2] or '') != (c[k][2] or '')]
print("OVERWRITTEN_SYNOPSIS:", len(over), over[:10])
ttl = [k for k in b if b[k][1] != c[k][1]]
print("TITLE_CHANGED:", len(ttl), [(k, b[k][1], c[k][1]) for k in ttl][:15])
img = [k for k in b if b[k][3] != c[k][3]]
print("IMAGE_CHANGED:", len(img), [(k, b[k][3][:50], c[k][3][:60]) for k in img][:10])
filled = sum(1 for k in c if (c[k][2] or '').strip() and not (b[k][2] or '').strip())
print("NEW_SYNOPSIS:", filled)
print("games", cur.execute("select count(*) from app_game").fetchone()[0])
print("status", dict(cur.execute("select status,count(*) from app_game group by status")))
print("integrity", cur.execute("pragma integrity_check").fetchone())
cur.close(); bak.close()
