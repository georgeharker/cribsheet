"""Per-project code-index state, owned by one object.

`CodeStore` holds the shared mutable state the whole code subsystem reaches into —
the symbol facet's refresh into the store, the freshness epoch, the per-project
write locks, and the in-flight / sweep progress tracking. `StoreCode` is one
project's records as the store holds them, which is what the code queries read.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any, Callable


from .symbols import decode_edge, edge_is_from, encode_edge
from .symbols import key as symbol_key
from .symbols import match_entry

if TYPE_CHECKING:
    from .config import Config
    from .paths import Paths


class StoreCode:
    """One project's code index read from the store: the same `entries` and
    `by_fqname` the resident index offers, rebuilt from the `symbol` items
    (crib.items.symbol_record) in record-file order. Reads skip the vectors.
    Built per query, so it holds nothing between calls."""

    def __init__(self, store: Any, proj: str) -> None:
        self._store = store
        self._proj = proj
        self._all: list[dict[str, Any]] | None = None

    def _records(self, names: set[str] | None = None) -> list[dict[str, Any]]:
        from .items import symbol_record
        from .symbols import ref_slug

        facet = {"name": {"$in": sorted(names)}} if names is not None else None
        items = self._store.items({"project": self._proj, "kind": "symbol"},
                                  facet=facet, vectors=False)
        recs = [symbol_record(it) for it in items.values()]
        recs.sort(key=lambda e: ref_slug(symbol_key(e)) + ".toml")
        return recs

    @property
    def entries(self) -> list[dict[str, Any]]:
        if self._all is None:
            self._all = self._records()
        return self._all

    def by_fqname(self, name: str) -> list[dict[str, Any]]:
        """Entries matching `name` under `match_entry`'s rule: the store narrows
        to the possible names, the rule decides. Copies, as the resident index
        hands out (callers annotate what they get)."""
        from .symbols import name_candidates

        return [dict(e) for e in self._records(name_candidates(name))
                if match_entry(e, name)]


def _mmss(seconds: float) -> str:
    """Seconds as `m:ss` (or `h:mm:ss` past the hour) — compact, no decimals.
    Floor-then-format, never round: a 59.9s eta reads 0:59, not 0:60."""
    s = max(seconds, 0.0) // 1.0
    h, s = divmod(s, 3600.0)
    m, s = divmod(s, 60.0)
    return f"{h:.0f}:{m:02.0f}:{s:02.0f}" if h else f"{m:.0f}:{s:02.0f}"


def format_sweep(sw: dict[str, Any], now: float | None = None) -> str:
    """One human line for a live sweep entry ({done, total[, started]}) — the
    shared renderer for the CLI ticker and the MCP progress message, so every
    surface agrees on shape: `42/317 files (13%)`, plus `· rate · eta` once
    `started` exists and enough has elapsed for the numbers to mean anything
    (below ~2s the rate is startup noise, not throughput)."""
    done = sw.get("done", 0)
    total = sw.get("total", 0) or 0
    if not total:
        return ""
    line = f"{done}/{total} files ({done * 100 // total}%)"
    failed = sw.get("failed") or 0
    if failed:
        # a describe failing fast (429) must never read as progress — surface it
        line += f" · {failed} desc-fail"
    started = sw.get("started")
    if started is not None and done:
        elapsed = (now if now is not None else time.monotonic()) - started
        if elapsed >= 2.0:
            rate = done / elapsed
            line += f" · {rate:.1f}/s · eta {_mmss((total - done) / rate)}"
    return line


class CodeStore:
    """The code subsystem's shared per-project state + its access primitives, in one
    place (see module docstring): keeps the store's symbol items in line with the
    records (`refresh`, `ensure`) and owns the freshness/locking invariants; the
    pipeline-coupled parts (revalidation's reindex) stay on Crib and are injected
    so this object stays free of the LSP."""

    def __init__(self, paths: Paths, config: Config, store: Any = None,
                 embedder: Any = None) -> None:
        self.paths = paths
        self.config = config
        # Embeds the descriptions the store's symbol items lack (`fill_vectors`).
        self.embedder = embedder
        # The store the symbol items live in; None reads the records from disk.
        self.store = store
        # `epoch` bumps on every in-process index write (trust-mode freshness);
        # `locks` serialize the records' read-modify-write so concurrent reindexes
        # (watcher vs query vs explicit index) can't corrupt the cross-file graph.
        # Per project, the write epoch at its last refresh (see `ensure`); absent
        # = not refreshed in this process yet.
        self.refreshed: dict[str, int] = {}
        self.epoch: dict[str, int] = {}
        # In-flight code indexing (project → files currently in the tracked indexer),
        # surfaced by `status`. Sweep progress (project → {done, total}) — a reliable
        # wait signal for an agent polling `status` on a background index: present while
        # the sweep runs, gone when it finishes.
        self.indexing: dict[str, list[str]] = {}
        # int values, `started`/`crib_at` floats or None (PEP 484 numeric tower
        # covers the ints; None is the "not stamped" marker).
        self.sweeps: dict[str, dict[str, float | None]] = {}
        self.indexing_lock = threading.Lock()
        self.locks: dict[str, threading.Lock] = {}
        self.locks_guard = threading.Lock()
        # Per project, held while the store is brought in line with the records
        # (refresh, fill_vectors): concurrent queries' first touch would otherwise
        # each map every file and their transactions conflict. Separate from
        # `locks` (the records' read-modify-write), which a reindex takes.
        self.refresh_locks: dict[str, threading.RLock] = {}

    # --- lock + epoch + freshness ---------------------------------------------
    def lock(self, proj: str) -> threading.Lock:
        """Per-project lock guarding the symbol_index read-modify-write."""
        with self.locks_guard:
            lk = self.locks.get(proj)
            if lk is None:
                lk = self.locks[proj] = threading.Lock()
            return lk

    def refresh_lock(self, proj: str) -> threading.RLock:
        with self.locks_guard:
            lk = self.refresh_locks.get(proj)
            if lk is None:
                lk = self.refresh_locks[proj] = threading.RLock()
            return lk

    def bump_epoch(self, proj: str) -> None:
        self.epoch[proj] = self.epoch.get(proj, 0) + 1

    def freshness(self) -> str:
        return getattr(self.config.retrieve, "code_freshness", "scan")

    def forget(self, proj: str) -> None:
        """Drop `proj`'s symbol items and stamps from the store (project delete,
        index reset): nothing would refresh a project that is gone."""
        with self.refresh_lock(proj):
            self.refreshed.pop(proj, None)
            if self.store is not None:
                gone = list(self.store.file_stamps("symbol", proj))
                self.store.replace_files("symbol", proj, [], gone)

    def refresh(self, proj: str, only: list[str] | None = None) -> None:
        """The symbol facet's refresh: map the record files that changed since
        the store last saw them (crib.filesync) into `symbol` items, one per
        record, and drop the items of records that are gone, in one
        transaction. `only` limits it to the named record files (a watcher
        batch); otherwise the whole symbol_index directory is diffed. Only
        changed files are parsed. Vectors are a separate step (`fill_vectors`)."""
        with self.refresh_lock(proj):
            self._refresh(proj, only)

    def _refresh(self, proj: str, only: list[str] | None) -> None:
        from .codeindex import SymbolIndex, _parse
        from .filesync import changes
        from .items import symbol_item

        si = SymbolIndex(self.paths.project_dir(proj))
        ch = changes(self.store, "symbol", proj, si.root, ".toml", only)
        if not ch:
            return
        files: list[tuple[str, int, int, list[Any]]] = []
        owner: dict[str, int] = {}  # item id -> index in `files` that produced it
        for name, (mtime, size) in sorted(ch.changed.items()):
            try:
                rec = _parse((si.root / name).read_text())
            except OSError:
                continue  # vanished after the stat: removed on the next refresh
            items = ([symbol_item(proj, rec)]
                     if rec.get("file") and rec.get("name") else [])
            for it in items:
                # two record files briefly holding one symbol (mid-conversion):
                # the store keeps one row, the later file's
                if it.id in owner:
                    files[owner[it.id]][3].clear()
                owner[it.id] = len(files)
            files.append((name, mtime, size, items))
        self.store.replace_files("symbol", proj, files, ch.removed)

    def records(self, proj: str) -> list[dict[str, Any]]:
        """Every symbol record of `proj`, as `SymbolIndex.all()` would parse
        them, read from the store after a refresh: one stat per record file,
        and only changed files are parsed. The refresh makes the store match the
        files at this moment, so a writer holding the project lock reads its own
        writes. Fresh dicts: callers may mutate them."""
        if self.store is None:
            from .codeindex import SymbolIndex

            return SymbolIndex(self.paths.project_dir(proj)).all()
        return self.view(proj).entries

    def view(self, proj: str) -> StoreCode:
        """`proj`'s records as the store holds them, refreshed first (no source
        gate: that is `ensure`, for queries)."""
        with self.refresh_lock(proj):
            self.refresh(proj)
        return StoreCode(self.store, proj)

    def fill_vectors(self, proj: str) -> None:
        """The vector step: embed each symbol item's text that has no vector yet
        (one batch of distinct texts; the embedding cache answers repeats)."""
        if self.embedder is None:
            return
        with self.refresh_lock(proj):
            missing = self.store.missing_body_vectors(
                {"project": proj, "kind": "symbol"})
            if not missing:
                return
            texts = list(dict.fromkeys(missing.values()))
            vec = dict(zip(texts, self.embedder.embed(texts)))
            self.store.set_body_vectors({i: list(vec[t]) for i, t in missing.items()})

    def ensure(
        self,
        proj: str,
        revalidate: Callable[[str], None] | None = None,
        watched: bool = False,
    ) -> None:
        """Bring the store in line with the files before a code query: the source
        gate (`revalidate`, as for the resident index), then the symbol refresh.
        `scan` mode refreshes on every query (a directory diff; only changed
        files are parsed); `trust` mode on first touch and after an in-process
        write has moved the epoch."""
        first = proj not in self.refreshed
        if revalidate is not None and (
            first or (self.freshness() == "scan" and not watched)
        ):
            revalidate(proj)  # source → records freshness
        with self.refresh_lock(proj):
            # re-read under the lock: a concurrent query may just have refreshed
            epoch = self.epoch.get(proj, 0)
            if (self.freshness() == "trust" and proj in self.refreshed
                    and self.refreshed[proj] == epoch):
                return
            self.refresh(proj)
            self.fill_vectors(proj)
            self.refreshed[proj] = epoch

    def revalidate(self, proj: str, reindex: Callable[..., Any]) -> None:
        """Lazy staleness gate: stat every indexed source file; reindex any whose mtime
        moved since it was indexed, and drop symbols of deleted files. Keeps queries
        honest under live editing without a watcher (the watcher just makes it eager).
        Best-effort — an LSP hiccup leaves the stale entry rather than failing the query.
        No-op when the source root is unknown (older index / no meta). `reindex` is Crib's
        pipeline-coupled per-file indexer, injected so this object stays free of the LSP."""
        from .codeindex import SymbolIndex

        store = SymbolIndex(self.paths.project_dir(proj))
        root = store.source_root()
        if root is None or not store.root.exists():
            return
        # Baseline = the on-disk mtime of a source file's symbol tomls (= WHEN indexed),
        # derived locally + cheap. NOT the toml's stored `mtime` field (that's a portable
        # git-date record, not comparable to a local st_mtime — and would need git here).
        # From the store's per-file stamps (crib.filesync): the refresh stats every
        # toml anyway and parses only the changed ones, and a stamp IS the toml's
        # mtime when it was last mapped, i.e. now.
        from .items import symbol_item_id

        baseline: dict[str, int] = {}  # source file → oldest mtime of its tomls
        # files with merge-dirtied symbols (blank content_hash, written by the sync
        # merge driver on divergent code states): their tomls are FRESH (post-pull
        # mtime), so the mtime gate alone would never catch them.
        dirty: set[str] = set()
        if self.store is None:
            return
        by_id = {symbol_item_id(proj, symbol_key(e)): e for e in self.records(proj)}
        for _name, (mt, _size, ids) in self.store.file_stamps("symbol", proj).items():
            for i in ids:
                e = by_id.get(i)
                if e is None:
                    continue
                f = e.get("file", "")
                if f:
                    baseline[f] = min(baseline.get(f, mt), mt)
                    if not e.get("content_hash"):
                        dirty.add(f)
        for rel, base_mt in baseline.items():
            src = root / rel
            try:
                cur = src.stat().st_mtime_ns
            except OSError:  # deleted → drop all its symbols + its edges
                self.drop_file(proj, rel)
                continue
            if rel in dirty or cur > base_mt:  # merge-dirtied, or edited after indexing
                try:  # (content_hash gate no-ops if unchanged)
                    reindex(root, rel, proj, patch_edges=True)
                except Exception:  # noqa: BLE001 — keep the stale entry over a failed query
                    pass

    def drop_file(self, proj: str, relpath: str) -> None:
        """Remove a deleted file's symbols and strip edges that originated from it —
        under the per-project lock (a delete mutates the same cross-file edges a
        concurrent reindex does), bumping the resident-cache epoch. Pure symbol_index
        mutation (no LSP), so its integrity invariants live with the state."""
        from .codeindex import SymbolIndex

        with self.lock(proj):
            store = SymbolIndex(self.paths.project_dir(proj))
            for e in self.records(proj):
                if e.get("file") == relpath:
                    store.delete(symbol_key(e))
                    continue
                cb = [
                    x
                    for x in (e.get("called_by") or [])
                    if not edge_is_from(x, relpath)
                ]
                rf = [
                    x
                    for x in (e.get("references") or [])
                    if not edge_is_from(x, relpath)
                ]
                if cb != (e.get("called_by") or []) or rf != (
                    e.get("references") or []
                ):
                    e["called_by"], e["references"] = cb, rf
                    store.write(e)
        self.bump_epoch(proj)

    def patch_edges(
        self,
        store: Any,
        new_entries: list[dict[str, Any]],
        relpath: str,
        proj: str | None = None,
    ) -> None:
        """Keep the cross-file graph consistent after a single-file reindex of A.

        BOTH reverse relations are patched, symmetrically: every `A→B` in A's fresh
        outbound `calls` must show up as `A` in B's `called_by` AND in B's
        `references` (a call site is also a mention — it is exactly what B's own
        extraction would have recorded). Stale edges originating from A (`… [A]`) are
        stripped from both first, so a removed call removes both reverse edges.
        Patching `called_by` alone left every `references` list stale until B's own
        file happened to be reindexed, and dossier/graph lied under watcher operation.

        Known bound: a NON-call mention from A (a type annotation, an import) isn't
        observable from A's side — the LSP only answers "who references B?" when B is
        the one being extracted — so such an edge is dropped here and restored the
        next time B itself is extracted. Bounded staleness, never a wrong edge.

        The stored edge string carries only `name [file]`, so name+file has to
        identify the target: a bare name must be UNIQUE in its file, else the pair is
        skipped rather than guessed (the old `(name, file)` dict kept whichever
        same-named symbol was parsed last and patched the wrong one). A server that
        hands back a QUALIFIED name instead resolves through the one matcher on an
        exact tier, scoped to the file — so `a.B.run [a.py]` lands on `B.run` even
        though two `run`s live there, whatever id shape the store is at. Cheap —
        in-memory from A's own fresh edges; no extra LSP.

        With `proj`, the records are read through the store (`records`: only
        changed files parsed); without it, `store` parses them all."""
        entries = self.records(proj) if proj is not None else store.all()
        by_key: dict[tuple[str, str], list[dict]] = {}
        by_file: dict[str, list[dict]] = {}
        for e in entries:
            by_key.setdefault((e.get("name", ""), e.get("file", "")), []).append(e)
            by_file.setdefault(str(e.get("file") or ""), []).append(e)

        def target(name: str, file: str) -> dict | None:
            hits = by_key.get((name, file)) or []
            if len(hits) == 1:
                return hits[0]  # a bare name, unique in its file
            if hits:
                return None  # ambiguous bare name: never guess
            # not a bare name there — a qualified spelling, matched exactly
            cands = [
                e
                for e in by_file.get(file, [])
                if match_entry(e, name) in ("ref", "exact", "was")
            ]
            return cands[0] if len(cands) == 1 else None

        changed: dict[str, dict] = {}
        for e in entries:  # 1) strip every edge originating in A
            if e.get("file") == relpath:
                continue
            for rel_key in ("called_by", "references"):
                cur = e.get(rel_key) or []
                kept = [x for x in cur if not edge_is_from(x, relpath)]
                if kept != cur:
                    e[rel_key] = kept
                    changed[symbol_key(e)] = e
        for s in new_entries:  # 2) re-add A's current edges
            edge = encode_edge(s["name"], relpath)
            for call in s.get("calls") or []:
                _proj, name, rel, _loc = decode_edge(call, "")
                tgt = target(name, rel)
                if tgt is None or tgt.get("file") == relpath:
                    continue
                e = changed.get(symbol_key(tgt), tgt)
                for rel_key in ("called_by", "references"):
                    if edge not in (e.get(rel_key) or []):
                        e[rel_key] = sorted(set((e.get(rel_key) or []) + [edge]))
                        changed[symbol_key(e)] = e
        for e in changed.values():
            store.write(e)
