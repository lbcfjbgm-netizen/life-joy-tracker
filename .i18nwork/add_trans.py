# -*- coding: utf-8 -*-
"""Add {% trans %} to Yamtrack templates (text nodes + selected attributes)."""
import os
import re
import sys

RAW_TAGS = {"script", "style", "pre", "code", "textarea"}
ATTRS = ("placeholder", "title", "aria-label")
TEXT_RE = re.compile(r"[A-Za-z]{2}")


def find_tag_end(s, i):
    """Return index just past the '>' of the tag starting at s[i] == '<'."""
    quote = None
    j = i + 1
    while j < len(s):
        c = s[j]
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == ">":
            return j + 1
        j += 1
    return -1


def i18n_quote(core):
    """Pick a quote char for {% trans %} or return None if impossible."""
    if '"' in core:
        if "'" in core:
            return None
        return "'"
    return '"'


def wrap_text(core):
    """Return the trans-tag for a text node, or None if it must be skipped."""
    core = core.strip()
    if not core or not TEXT_RE.search(core):
        return None
    if core.startswith("&") and " " not in core:
        return None
    core = re.sub(r"\s+", " ", core)
    q = i18n_quote(core)
    if q is None:
        return None
    return "{% trans " + q + core + q + " %}"


def transform_text(seg, stats):
    m = re.match(r"^(\s*)(.*?)(\s*)$", seg, re.S)
    lead, core, tail = m.group(1), m.group(2), m.group(3)
    tag = wrap_text(core)
    if tag is None:
        return seg
    stats["text"] += 1
    return lead + tag + tail


def transform_attrs(tag, stats):
    """Replace translatable attribute values inside a single HTML tag."""
    if "{{" in tag or "{%" in tag:
        return tag

    def repl(m):
        name, value = m.group(1), m.group(2)
        core = value.strip()
        if not TEXT_RE.search(core) or not core:
            return m.group(0)
        q = i18n_quote(core)
        if q is None:
            return m.group(0)
        stats["attr"] += 1
        return "%s=\"%s{%% trans %s%s%s %%}%s\"" % (name, value[:len(value) - len(value.lstrip())], q, core, q, value[len(value.rstrip()):])

    pattern = re.compile(
        r'\b(%s)="([^"<>{}%%]*)"' % "|".join(re.escape(a) for a in ATTRS),
    )
    return pattern.sub(repl, tag)


def process(src, stats):
    out = []
    i = 0
    n = len(src)
    while i < n:
        if src.startswith("{#", i):
            j = src.find("#}", i)
            j = n if j < 0 else j + 2
            out.append(src[i:j])
            i = j
        elif src.startswith("{%", i):
            j = src.find("%}", i)
            j = n if j < 0 else j + 2
            out.append(src[i:j])
            i = j
        elif src.startswith("{{", i):
            j = src.find("}}", i)
            j = n if j < 0 else j + 2
            out.append(src[i:j])
            i = j
        elif src[i] == "<":
            j = find_tag_end(src, i)
            if j < 0:
                out.append(src[i:])
                break
            tag = transform_attrs(src[i:j], stats)
            out.append(tag)
            i = j
            mt = re.match(r"<\s*([A-Za-z0-9]+)", tag)
            name = mt.group(1).lower() if mt else ""
            if name in RAW_TAGS and not tag.rstrip().endswith("/>"):
                close = src.lower().find("</" + name, i)
                if close < 0:
                    out.append(src[i:])
                    i = n
                else:
                    end = src.find(">", close)
                    end = n if end < 0 else end + 1
                    out.append(src[i:end])
                    i = end
        else:
            nxt = n
            for tok in ("<", "{#", "{%", "{{"):
                p = src.find(tok, i)
                if 0 <= p < nxt:
                    nxt = p
            seg = src[i:nxt]
            out.append(transform_text(seg, stats))
            i = nxt
    return "".join(out)


def ensure_load_i18n(src):
    if re.search(r"\{%\s*load[^%]*\bi18n\b", src):
        return src
    # insert after the last leading {% load %} / extends / comment line
    lines = src.split("\n")
    idx = 0
    for k, line in enumerate(lines[:6]):
        st = line.strip()
        if st.startswith("{%") and ("load" in st or "extends" in st or "comment" in st):
            idx = k + 1
        elif st == "":
            continue
        else:
            break
    lines.insert(idx, "{% load i18n %}")
    return "\n".join(lines)


def main(root):
    stats = {"files": 0, "text": 0, "attr": 0}
    changed = []
    for dirpath, _, names in os.walk(root):
        for name in sorted(names):
            if not name.endswith(".html"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            before = dict(stats)
            new = process(src, stats)
            if new == src:
                continue
            new = ensure_load_i18n(new)
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(new)
            stats["files"] += 1
            changed.append((path, stats["text"] - before["text"], stats["attr"] - before["attr"]))
    for c in changed:
        print("changed", c[0], "text+%d attr+%d" % (c[1], c[2]))
    print("SUMMARY", stats)


if __name__ == "__main__":
    main(sys.argv[1])
