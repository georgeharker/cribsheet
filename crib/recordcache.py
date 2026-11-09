"""A read cache for crib's derived-index records (one TOML file per record).

The derived tiers (symbol_index/, and later summary_index/ and keyword_index/)
are many small TOML files: the git-synced source of truth, shared across
machines and merge-driver aware. Reading them is the expensive part: on
un-bien's 10,578 symbol records, listing takes 6 ms, stat 110 ms, read
~0.6-1.2 s and parse ~0.6 s, and startup reads every project's records several
times (the describe backlog, the dirty-code rebuild, each first code query).

This cache keeps the TOMLs as the truth and avoids re-parsing them:

- a per-process MEMO per tier directory, {filename: (mtime_ns, size, record)};
- a DuckDB table behind it (when the store has one), so a fresh daemon fills
  its memo with ONE query instead of parsing every file again.

Every `load(dir)` re-stats the directory, so a TOML edited by git, a merge or
another crib process is re-parsed the next time it's read; nothing is
trusted past its (mtime, size). The cache is per machine, never synced, and
fully rebuildable: dropping it costs one re-parse.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

Stamp = tuple[int, int]  # (st_mtime_ns, st_size)


class CacheStorage(Protocol):
    """Rows the cache persists; the DuckDB stores implement it natively."""

    def rc_load(self, key: str) -> dict[str, tuple[int, int, str]]: ...
    def rc_put(self, key: str, rows: list[tuple[str, int, int, str]]) -> None: ...
    def rc_delete(self, key: str, names: list[str]) -> None: ...


class RecordCache:
    def __init__(self, storage: CacheStorage | None = None) -> None:
        self._s = storage
        # filename -> (stamp, record as JSON): a JSON string, so every reader gets
        # its own fresh dict (callers mutate what all() returns) at json.loads cost.
        self._memo: dict[str, dict[str, tuple[Stamp, str]]] = {}
        self._loaded: set[str] = set()  # keys whose DuckDB rows were pulled this run
        self._lock = threading.Lock()

    def _pull(self, key: str) -> None:
        """First touch of a tier this run: seed the memo from DuckDB."""
        if key in self._loaded or self._s is None:
            self._loaded.add(key)
            return
        try:
            rows = self._s.rc_load(key)
        except Exception:  # noqa: BLE001 — a cache miss is just a re-parse
            rows = {}
        memo = self._memo.setdefault(key, {})
        for name, (mtime, size, rec) in rows.items():
            memo.setdefault(name, ((mtime, size), rec))
        self._loaded.add(key)

    def load(
        self,
        tier_dir: Path,
        parse: Callable[[str], dict],
        suffix: str = ".toml",
    ) -> list[tuple[str, Stamp, dict]]:
        """Every record in `tier_dir`, as (filename, (mtime_ns, size), record),
        sorted by filename. Re-parses only files whose stamp changed."""
        key = str(tier_dir)
        try:
            entries = list(os.scandir(tier_dir))
        except FileNotFoundError:
            entries = []
        stamps: dict[str, Stamp] = {}
        for e in entries:
            # suffix only: a record's filename is its symbol_ref's slug, so a source
            # under a dot dir (.config/..., .build/...) yields a dot-prefixed RECORD.
            # crib's own metadata (.schema, .source_root) never ends in the suffix.
            if e.name.endswith(suffix):
                try:
                    st = e.stat()
                except FileNotFoundError:
                    continue  # removed between listing and stat
                stamps[e.name] = (st.st_mtime_ns, st.st_size)
        with self._lock:
            self._pull(key)
            memo = self._memo.setdefault(key, {})
            changed = [n for n, s in stamps.items() if n not in memo or memo[n][0] != s]
            gone = [n for n in memo if n not in stamps]
        fresh: list[tuple[str, int, int, str]] = []
        parsed: dict[str, tuple[Stamp, str]] = {}
        for n in changed:
            try:
                rec = json.dumps(parse((tier_dir / n).read_text()), sort_keys=True)
            except OSError:
                continue  # vanished after the stat: picked up as gone next time
            parsed[n] = (stamps[n], rec)
            fresh.append((n, stamps[n][0], stamps[n][1], rec))
        with self._lock:
            memo.update(parsed)
            for n in gone:
                memo.pop(n, None)
            snap = [(n, memo[n][0], memo[n][1]) for n in sorted(memo) if n in stamps]
        if self._s is not None and (fresh or gone):
            try:
                if fresh:
                    self._s.rc_put(key, fresh)
                if gone:
                    self._s.rc_delete(key, gone)
            except Exception:  # noqa: BLE001 — persistence is an optimisation only
                pass
        return [(n, s, json.loads(rec)) for n, s, rec in snap]


_CACHE: RecordCache = RecordCache()  # memo-only until a store is attached


def get() -> RecordCache:
    return _CACHE


def attach(store: Any) -> RecordCache:
    """Back the process cache with `store`'s database when it has one (the DuckDB
    stores). Called once by Crib when it opens its store."""
    global _CACHE
    has = all(
        callable(getattr(store, m, None)) for m in ("rc_load", "rc_put", "rc_delete")
    )
    _CACHE = RecordCache(store if has else None)
    return _CACHE
