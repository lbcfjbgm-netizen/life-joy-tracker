"""A small, reusable "where did I stop" file for long running batch jobs.

One JSON file holds a plain mapping of ``key -> status`` plus a timestamp.  A
job that dies in the middle (Ctrl-C, network blocking forever, machine reboot)
reopens the same file on the next run and only handles the keys that are not
in it yet.

Design points
-------------
* **Atomic write**: the payload is written to ``<name>.tmp`` first and then
  moved over the real file with :func:`os.replace`, so a crash during a save
  can never leave a half written JSON behind.  A broken file is never fatal:
  it is reported and the run starts from an empty mapping.
* **Batched flush**: ``set()`` only marks the file dirty; the disk is touched
  every ``every`` keys (default 25) or when ``min_interval`` seconds passed,
  or when ``flush(force=True)`` is called.  This keeps the write cost flat.
* **Custom key / file per task**: ``open_cursor("cn_names")`` resolves to
  ``<repo>/.tools/cn_names_progress.json``, so each job owns its own file and
  two jobs can never overwrite each other.  Pass ``path=`` for anything else.

Usage::

    from progress_cursor import open_cursor

    cur = open_cursor("cn_names")            # .tools/cn_names_progress.json
    if "12345" in cur:
        ...                                   # already done
    cur["12345"] = "updated"                  # marks dirty, maybe flushes
    cur.flush(force=True)                     # always at the end / on SIGINT

The file layout is intentionally boring so old files stay readable::

    {"version": 1, "updated_at": "2026-10-22 04:10:00", "done": {"k": "v"}}
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from collections.abc import Iterable, Iterator, MutableMapping
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / ".tools"
VERSION = 1


def default_progress_path(name: str, directory: Path | None = None) -> Path:
    """Return ``<dir>/<name>_progress.json`` for a task name.

    ``name`` is the per-task key (``snake_case`` file name fragment); it keeps
    two tasks from sharing one file and clobbering each other.
    """
    clean = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(name))
    if not clean:
        raise ValueError("progress cursor name must not be empty")
    return Path(directory or DEFAULT_DIR) / f"{clean}_progress.json"


class ProgressCursor(MutableMapping):
    """Dict-like resume store backed by one atomically written JSON file.

    Acts like a ``dict`` of ``key -> status`` (so ``in``, ``len()``, ``.get``
    all work), and adds the flush policy on top.  Values are stored verbatim,
    they are only ever compared for equality by callers.
    """

    BUCKET = "done"

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        name: str | None = None,
        every: int = 25,
        min_interval: float = 0.0,
        version: int = VERSION,
        directory: Path | None = None,
        verbose: bool = False,
    ) -> None:
        if path is None:
            if name is None:
                raise ValueError("either path= or name= is required")
            path = default_progress_path(name, directory)
        self.path = Path(path)
        self.every = max(1, int(every))
        self.min_interval = max(0.0, float(min_interval))
        self.version = int(version)
        self.verbose = verbose

        self._data: dict[str, object] = {}
        self._dirty = 0
        self._last_write = 0.0
        self._load()

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        """Read the file, tolerating a missing or broken one."""
        if not self.path.exists():
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            self._warn(f"progress file unreadable, starting fresh: {self.path}")
            return
        if not isinstance(data, dict):
            self._warn(f"progress file is not a JSON object, ignoring: {self.path}")
            return
        bucket = data.get(self.BUCKET)
        if isinstance(bucket, dict):
            self._data = dict(bucket)
        else:
            self._warn(f"progress file has no '{self.BUCKET}' mapping: {self.path}")

    def _warn(self, message: str) -> None:
        if self.verbose:
            print(message, flush=True)

    # ------------------------------------------------------------- properties
    @property
    def updated_at(self) -> str:
        """Timestamp of the last successful write (``-`` when never written)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "-"
        return str(data.get("updated_at", "-")) if isinstance(data, dict) else "-"

    @property
    def dirty(self) -> int:
        """How many keys changed since the last write."""
        return self._dirty

    # -------------------------------------------------------------- dict API
    def __getitem__(self, key: str):
        return self._data[key]

    def __setitem__(self, key: str, value) -> None:
        self._data[str(key)] = value
        self._dirty += 1
        self.maybe_flush()

    def __delitem__(self, key: str) -> None:
        del self._data[key]
        self._dirty += 1
        self.maybe_flush()

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    # ----------------------------------------------------------- extra sugar
    def mark(self, key: str, value="done") -> None:
        """Record one handled key (alias of ``cursor[key] = value``)."""
        self[key] = value

    def mark_all(self, keys: Iterable[str], value="done") -> None:
        """Record many handled keys with a single (possibly batched) write."""
        for key in keys:
            self._data[str(key)] = value
            self._dirty += 1
        self.maybe_flush()

    def counts(self) -> Counter:
        """Return ``{status: how many keys}`` for quick progress reports."""
        return Counter(self._data.values())

    def summary(self, labels: dict | None = None) -> dict:
        """Return a JSON friendly snapshot of the store."""
        counts = self.counts()
        return {
            "path": str(self.path),
            "processed": len(self._data),
            "counts": {str(k): v for k, v in counts.items()},
            "labels": dict(labels or {}),
            "updated_at": self.updated_at,
        }

    # ------------------------------------------------------------- persistence
    def maybe_flush(self) -> bool:
        """Flush when the batch is full or enough time has passed."""
        if not self._dirty:
            return False
        if self._dirty >= self.every:
            return self.flush()
        if self.min_interval and (time.time() - self._last_write) >= self.min_interval:
            return self.flush()
        return False

    def flush(self, force: bool = False) -> bool:
        """Write the file atomically. Returns True when something was written.

        ``force`` writes even when nothing changed (used at the end of a run
        so the ``updated_at`` stamp is fresh).
        """
        if not self._dirty and not force:
            return False
        payload = {
            "version": self.version,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            self.BUCKET: self._data,
        }
        self._atomic_write(json.dumps(payload, ensure_ascii=False))
        self._dirty = 0
        self._last_write = time.time()
        return True

    def _atomic_write(self, text: str) -> None:
        """Write ``text`` to a temp file and rename it over the target."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            # Never leave a stray temp file behind, and never touch the target.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def reset(self) -> None:
        """Forget everything: drop the file and the in-memory mapping."""
        self._data.clear()
        self._dirty = 0
        if self.path.exists():
            self.path.unlink()

    def __enter__(self) -> ProgressCursor:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Always persist on the way out, even when the body raised.
        self.flush(force=True)
        return False

    def __repr__(self) -> str:
        return f"ProgressCursor({str(self.path)!r}, entries={len(self._data)}, dirty={self._dirty})"


def open_cursor(
    name: str | None = None,
    *,
    path: Path | str | None = None,
    directory: Path | None = None,
    every: int = 25,
    min_interval: float = 0.0,
    verbose: bool = True,
) -> ProgressCursor:
    """Open (or create) the resume file of one task.

    ``name`` picks the file name (``<name>_progress.json`` under ``.tools``);
    pass ``path=`` when the file must live somewhere else.
    """
    return ProgressCursor(
        path=path,
        name=name,
        every=every,
        min_interval=min_interval,
        directory=directory,
        verbose=verbose,
    )


def show(cursor: ProgressCursor, title: str = "progress", labels: dict | None = None) -> None:
    """Print a human readable status block for a cursor."""
    counts = cursor.counts()
    print(f"{title} file: {cursor.path}")
    print(f"processed: {len(cursor)}")
    if labels:
        for key, label in labels.items():
            print(f"  {label:<8}{counts.get(key, 0)}")
    else:
        for key, count in sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0]))):
            print(f"  {key:<12}{count}")
    print(f"last write: {cursor.updated_at}")
