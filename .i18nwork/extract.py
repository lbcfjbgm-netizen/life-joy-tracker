import re, json, os, collections

ROOT = "src/templates"
tag_re = re.compile(r'\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}', re.S)
attr_re = re.compile(r'(placeholder|title|aria-label|alt|value)="([^"{}%<>]{2,80})"')

texts = collections.Counter()
attrs = collections.Counter()
files = {}
for dirpath, _, names in os.walk(ROOT):
    for n in names:
        if not n.endswith(".html"):
            continue
        p = os.path.join(dirpath, n)
        s = open(p, encoding="utf-8").read()
        # text nodes
        for m in re.finditer(r'>([^<>{}%]+)<', s):
            t = m.group(1).strip()
            if t and re.search(r'[A-Za-z]{2}', t) and not t.startswith("&"):
                texts[t] += 1
        for m in attr_re.finditer(s):
            v = m.group(2).strip()
            if v and re.search(r'[A-Za-z]{2}', v):
                attrs[v] += 1

print("== TEXTS", len(texts), "total", sum(texts.values()))
for t, c in sorted(texts.items(), key=lambda kv: (-kv[1], kv[0])):
    print(c, "|", t)
print("== ATTRS", len(attrs), "total", sum(attrs.values()))
for t, c in sorted(attrs.items(), key=lambda kv: (-kv[1], kv[0])):
    print(c, "|", t)
