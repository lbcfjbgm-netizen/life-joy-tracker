# -*- coding: utf-8 -*-
"""Minimal PO parser/writer: fill msgstr from a translations dict."""
import re
import sys


def parse(text):
    """Parse a .po file into a list of entry dicts."""
    entries = []
    cur = {"comments": [], "msgid": None, "msgstr": None, "flags": []}
    lines = text.split("\n")
    i = 0
    state = None
    while i < len(lines):
        line = lines[i]
        if line.startswith("#"):
            if line.startswith("#,"):
                cur["flags"].append(line[2:].strip())
            else:
                cur["comments"].append(line)
        elif line.startswith("msgid "):
            if cur["msgid"] is not None:
                entries.append(cur)
                cur = {"comments": [], "msgid": None, "msgstr": None, "flags": []}
            cur["msgid"] = unquote(line[6:])
            state = "msgid"
        elif line.startswith("msgstr "):
            cur["msgstr"] = unquote(line[7:])
            state = "msgstr"
        elif line.startswith('"'):
            if state == "msgid":
                cur["msgid"] += unquote(line)
            elif state == "msgstr":
                cur["msgstr"] += unquote(line)
        elif line.strip() == "":
            if cur["msgid"] is not None:
                entries.append(cur)
                cur = {"comments": [], "msgid": None, "msgstr": None, "flags": []}
                state = None
        i += 1
    if cur["msgid"] is not None:
        entries.append(cur)
    return entries


def unquote(fragment):
    m = re.match(r'\s*"(.*)"\s*$', fragment)
    if not m:
        return fragment
    raw = m.group(1)
    out = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "t":
                out.append("\t")
            else:
                out.append(nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def quote(s):
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n").replace("\t", "\\t")
    return '"' + s + '"'


def write(entries, path, header):
    out = [header.rstrip("\n")]
    for e in entries:
        if e["msgid"] == "":
            continue
        out.append("")
        for c in e["comments"]:
            if c.startswith("#~"):
                continue
            out.append(c)
        flags = [f for f in e["flags"] if f != "fuzzy" and f != "untranslated"]
        if e["msgstr"] == "":
            flags.append("fuzzy")
        if flags:
            out.append("#, " + ", ".join(flags))
        out.append("msgid " + quote(e["msgid"]))
        out.append("msgstr " + quote(e["msgstr"]))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out) + "\n")
