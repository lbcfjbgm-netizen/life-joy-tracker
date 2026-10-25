"""Minimal self-test for :mod:`progress_cursor`.

Scenario it proves
------------------
1. three keys are written and flushed -> the file exists and holds 3 keys;
2. the process "dies" (a second cursor writes more keys but never flushes, and
   a torn ``.tmp`` file is left behind) -> the real file still holds 3 keys;
3. a new run reopens the file and only the missing keys are reported as
   pending -> the work resumes instead of restarting.

Run it with::

    .venv/Scripts/python.exe scripts/test_progress_cursor.py

Exit code 0 means every check passed.  It writes only into a throwaway
directory under ``.tools/_cursor_selftest/`` and deletes its own file at the
end, so no real progress file is touched.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from progress_cursor import ProgressCursor, default_progress_path, show  # noqa: E402

SANDBOX = Path(__file__).resolve().parent.parent / ".tools" / "_cursor_selftest"
KEYS = [f"page-{i:03d}" for i in range(1, 8)]
failures: list[str] = []


def check(label: str, got, want) -> None:
    """Record one assertion result."""
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        failures.append(label)


def main() -> int:
    path = SANDBOX / "cursor_selftest_progress.json"
    tmp = path.with_name(path.name + ".tmp")
    path.unlink(missing_ok=True)
    tmp.unlink(missing_ok=True)

    print("[1] run #1: handle 3 keys, flush")
    cur = ProgressCursor(path=path, every=2, verbose=False)
    for key in KEYS[:3]:
        cur[key] = "done"
    cur.flush(force=True)
    check("file exists", path.exists(), True)
    check("keys stored", len(ProgressCursor(path=path, verbose=False)), 3)

    print("[2] simulated crash: 4 more keys in memory, no flush, torn tmp file")
    crashed = ProgressCursor(path=path, every=100, verbose=False)
    for key in KEYS[3:]:
        crashed[key] = "done"
    tmp.write_text('{"version": 1, "updated_at": "x", "done": {"page-00', encoding="utf-8")
    del crashed  # nothing was flushed, exactly like a killed process
    check("on-disk keys after crash", len(ProgressCursor(path=path, verbose=False)), 3)
    check("torn tmp ignored", ProgressCursor(path=path, verbose=False).get("page-004"), None)

    print("[3] run #2: resume, skip the done keys, handle the rest")
    resumed = ProgressCursor(path=path, every=4, verbose=False)
    seen = set(resumed)
    pending = [key for key in KEYS if key not in seen]
    check("resumed done count", len(seen), 3)
    check("pending keys", pending, KEYS[3:])
    resumed.mark_all(pending, "done")
    resumed.flush(force=True)
    show(ProgressCursor(path=path, verbose=False), title="after run #2", labels={"done": "done"})

    print("[4] run #3: nothing left to do")
    third = ProgressCursor(path=path, verbose=False)
    check("all keys stored", len(third), len(KEYS))
    check("pending now empty", [k for k in KEYS if k not in third], [])
    check("tmp cleaned after crash", tmp.exists(), False)

    print("[5] broken file tolerance")
    path.write_text("this is not json {", encoding="utf-8")
    broken = ProgressCursor(path=path, verbose=False)
    check("broken file -> empty", len(broken), 0)
    broken["page-001"] = "done"
    broken.flush(force=True)
    check("recovers by rewriting", len(ProgressCursor(path=path, verbose=False)), 1)

    print("[6] name -> path helper")
    check(
        "default path",
        default_progress_path("cn_names").name,
        "cn_names_progress.json",
    )

    path.unlink(missing_ok=True)
    tmp.unlink(missing_ok=True)
    try:
        SANDBOX.rmdir()
    except OSError:
        pass

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) -> {failures}")
        return 1
    print("ALL CHECKS PASSED (3 runs, one simulated crash, resume worked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
