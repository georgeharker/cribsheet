"""Which of a facet's files changed since they were last mapped into the store.

The shared mechanics of keeping the store in line with the files (design:
"Unified item and edge tables in the DuckDB cache", Layering). A facet owns the
meaning (which files are its, how a file maps to items and chunks); this module
only answers "which files need mapping again", by comparing each file's
(mtime_ns, size) with the stamp the store recorded when the facet last mapped
it. The facet parses and maps the changed files itself and hands the rows to
`Store.replace_files`, which records the new stamps in the same transaction.

Reads and directory watchers both call into this; whichever sees a change first
maps it, and the stamp makes the other a no-op.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Stamp = tuple[int, int]  # (st_mtime_ns, st_size)


@dataclass(frozen=True)
class Changes:
    """Files whose stamp moved (or that the store has never seen), with their
    current stamps, and files the store has a stamp for that are gone."""

    changed: dict[str, Stamp]
    removed: list[str]

    def __bool__(self) -> bool:
        return bool(self.changed or self.removed)


def scan(directory: Path, suffix: str) -> dict[str, Stamp]:
    """{filename: stamp} for the files in `directory` ending in `suffix` (one
    scandir; a missing directory has none). Dot-prefixed names count: a record's
    filename is a slug, and a source under a dot directory yields one."""
    out: dict[str, Stamp] = {}
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return out
    for e in entries:
        if not e.name.endswith(suffix):
            continue
        try:
            st = e.stat()
        except FileNotFoundError:
            continue  # removed between the listing and the stat
        out[e.name] = (st.st_mtime_ns, st.st_size)
    return out


def changes(
    store: Any,
    facet: str,
    project: str,
    directory: Path,
    suffix: str,
    only: list[str] | None = None,
) -> Changes:
    """What `facet` must map again in `directory` for `project`. `only` limits it
    to the named files (a watcher batch, or the files a write just touched);
    otherwise the whole directory is scanned."""
    known = {f: (m, z) for f, (m, z, _ids) in store.file_stamps(facet, project).items()}
    if only is None:
        now = scan(directory, suffix)
        names = set(now) | set(known)
    else:
        now = {}
        for name in only:
            try:
                st = (directory / name).stat()
            except FileNotFoundError:
                continue
            now[name] = (st.st_mtime_ns, st.st_size)
        names = set(only)
    changed = {n: s for n, s in now.items() if known.get(n) != s}
    removed = sorted(n for n in names if n in known and n not in now)
    return Changes(changed, removed)
