"""Per-project code-index state, owned by one object.

`CodeStore` holds the shared mutable state the whole code subsystem reaches into —
the resident cache, the freshness epoch, the per-project write locks, and the
in-flight / sweep progress tracking. It was extracted from the `Crib` god object so
that state has a single owner (the seam the CodeStore refactor builds on). This first
step relocates the *fields* here; the state-owning *methods* (`_code_lock`,
`_resident_code`, `_revalidate`, …) still live on `Crib` and reach through
`crib.code`, and migrate onto `CodeStore` in later steps.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .config import Config
    from .paths import Paths


class _ResidentCode:
    """A project's code index kept RESIDENT so a `code_*` query need not re-parse
    every symbol TOML and re-embed every description (the dominant cost). Built once
    per freshness token (`tok`); rebuilt only when the token changes — and even then
    description embeddings are reused by description text, so an unchanged symbol is
    never re-embedded. Holds the parsed entries, an fqname index, the description→
    vector map, and the precomputed dense/sparse query arrays (`_prepare`)."""

    def __init__(self, tok: Any, entries: list[dict[str, Any]],
                 emb: dict[str, list[float]]) -> None:
        self.tok = tok
        self.entries = entries
        self.emb = emb                                       # description text → vector
        self.by_fq: dict[str, dict[str, Any]] = {e["fqname"]: e for e in entries}
        self._prepare()

    def by_fqname(self, name: str) -> list[dict[str, Any]]:
        """Entries whose fqname is, ends with `.name`, or has last segment `name` —
        the resident mirror of SymbolIndex.by_fqname (no disk read)."""
        return [e for e in self.entries
                if e["fqname"] == name or e["fqname"].endswith("." + name)
                or e["fqname"].split(".")[-1] == name]

    def _prepare(self) -> None:
        from .retrieve import BM25, _as_tf
        # Only symbols with a description or name terms are query candidates.
        self.lk = [e for e in self.entries
                   if e.get("description") or e.get("name_terms")]
        self.lk_ids = [e["fqname"] for e in self.lk]
        self.bm25 = BM25([_as_tf([t.lower() for t in (e.get("name_terms") or [])])
                          for e in self.lk])
        self._dense: list[list[float] | None] | None = None   # built lazily (code_lookup only)

    def dense(self, embedder: Any) -> list[list[float] | None]:
        """Dense vectors aligned to `lk` — embedding only the descriptions not already
        cached in `emb` (reused across queries AND across reloads). ONLY code_lookup
        needs these, so dossier/graph/xref never pay to embed."""
        if self._dense is None:
            missing = list(dict.fromkeys(
                e["description"] for e in self.lk
                if e.get("description") and e["description"] not in self.emb))
            if missing:
                self.emb.update(zip(missing, embedder.embed(missing)))
            self._dense = [self.emb.get(e["description"]) if e.get("description")
                           else None for e in self.lk]
        return self._dense


class CodeStore:
    """The code subsystem's shared per-project state + its access primitives, in one
    place (see module docstring). Owns the resident cache and its freshness/locking
    invariants; the pipeline-coupled parts (revalidation, single-file drop) stay on
    Crib and are injected into `resident()` so this object stays free of the LSP."""

    def __init__(self, paths: Paths, config: Config) -> None:
        self.paths = paths
        self.config = config
        # Resident code index (per project): parsed symbols + description embeddings, so
        # a query skips the full TOML re-parse + re-embed. `epoch` bumps on every
        # in-process index write (trust-mode invalidation); `locks` serialize the store
        # read-modify-write so concurrent reindexes (watcher vs query vs explicit index)
        # can't corrupt the cross-file call graph.
        self.cache: dict[str, _ResidentCode] = {}
        self.epoch: dict[str, int] = {}
        # In-flight code indexing (project → files currently in the tracked indexer),
        # surfaced by `status`. Sweep progress (project → {done, total}) — a reliable
        # wait signal for an agent polling `status` on a background index: present while
        # the sweep runs, gone when it finishes.
        self.indexing: dict[str, list[str]] = {}
        self.sweeps: dict[str, dict[str, int]] = {}
        self.indexing_lock = threading.Lock()
        self.locks: dict[str, threading.Lock] = {}
        self.locks_guard = threading.Lock()

    # --- lock + epoch + freshness ---------------------------------------------
    def lock(self, proj: str) -> threading.Lock:
        """Per-project lock guarding the symbol_index read-modify-write."""
        with self.locks_guard:
            lk = self.locks.get(proj)
            if lk is None:
                lk = self.locks[proj] = threading.Lock()
            return lk

    def bump_epoch(self, proj: str) -> None:
        self.epoch[proj] = self.epoch.get(proj, 0) + 1

    def freshness(self) -> str:
        return getattr(self.config.retrieve, "code_freshness", "scan")

    def dir_sig(self, proj: str) -> tuple[int, int]:
        """Cheap signature of the symbol_index dir — (toml count, max mtime_ns) — so
        ANY on-disk change (our writes, a `git pull` of the store) flips it. One
        scandir; no parse. In-place body edits keep the filename, so dir-mtime alone
        misses them — hence max(file mtime), not the dir's."""
        from .codeindex import SymbolIndex
        root = SymbolIndex(self.paths.project_dir(proj)).root
        if not root.exists():
            return (0, 0)
        n, mx = 0, 0
        with os.scandir(root) as it:
            for e in it:
                if e.name.endswith(".toml"):
                    n += 1
                    try:
                        m = e.stat().st_mtime_ns
                    except OSError:
                        continue
                    if m > mx:
                        mx = m
        return (n, mx)

    def tok(self, proj: str) -> tuple[str, Any]:
        """Freshness token the resident cache is keyed on: an in-process epoch in
        `trust` mode (no stat), the dir signature in `scan` mode (catches external
        writes too)."""
        if self.freshness() == "trust":
            return ("epoch", self.epoch.get(proj, 0))
        return ("sig", self.dir_sig(proj))

    # --- resident cache -------------------------------------------------------
    def resident(self, proj: str, revalidate: Callable[[str], None] | None = None,
                 watched: bool = False) -> _ResidentCode:
        """Return the project's resident code index, rebuilding only when its token
        moved. On a COLD cache we always run the injected `revalidate` once (catches
        edits made while the daemon — and its watcher — were down); when warm, we skip
        it in `trust` mode and whenever the watcher already covers the project
        (`watched` — edits refreshed eagerly on save). `revalidate` is Crib's
        pipeline-coupled lazy source→index gate, kept OUT of this object."""
        rc = self.cache.get(proj)
        if revalidate is not None and (rc is None
                                       or (self.freshness() == "scan" and not watched)):
            revalidate(proj)                                # source → index freshness
        tok = self.tok(proj)
        rc = self.cache.get(proj)
        if rc is not None and rc.tok == tok:
            return rc
        return self.reload(proj, tok, rc)

    def reload(self, proj: str, tok: Any,
               prev: _ResidentCode | None) -> _ResidentCode:
        """Reparse the symbol TOMLs and rebuild the resident cache, CARRYING FORWARD
        every description embedding whose text is unchanged (from `prev.emb`, pruned to
        current descriptions). Nothing is embedded here — code_lookup fills in only the
        genuinely new/edited descriptions lazily, so a reload after an edit re-embeds
        just what changed, and dossier/graph/xref reloads embed nothing at all."""
        from .codeindex import SymbolIndex
        entries = SymbolIndex(self.paths.project_dir(proj)).all()
        prev_emb = prev.emb if prev is not None else {}
        emb = {d: prev_emb[d]
               for d in dict.fromkeys(e["description"] for e in entries
                                      if e.get("description"))
               if d in prev_emb}
        rc = _ResidentCode(tok, entries, emb)
        self.cache[proj] = rc
        return rc

    def revalidate(self, proj: str, reindex: Callable[..., Any]) -> None:
        """Lazy staleness gate: stat every indexed source file; reindex any whose mtime
        moved since it was indexed, and drop symbols of deleted files. Keeps queries
        honest under live editing without a watcher (the watcher just makes it eager).
        Best-effort — an LSP hiccup leaves the stale entry rather than failing the query.
        No-op when the source root is unknown (older index / no meta). `reindex` is Crib's
        pipeline-coupled per-file indexer, injected so this object stays free of the LSP."""
        from .codeindex import SymbolIndex, _parse
        store = SymbolIndex(self.paths.project_dir(proj))
        root = store.source_root()
        if root is None or not store.root.exists():
            return
        # Baseline = the on-disk mtime of a source file's symbol tomls (= WHEN indexed),
        # derived locally + cheap. NOT the toml's stored `mtime` field (that's a portable
        # git-date record, not comparable to a local st_mtime — and would need git here).
        baseline: dict[str, int] = {}        # source file → oldest mtime of its tomls
        dirty: set[str] = set()              # files with merge-dirtied symbols (blank
        for p in store.root.glob("*.toml"):  # content_hash, written by the sync merge
            try:                             # driver on divergent code states) — their
                mt = p.stat().st_mtime_ns    # tomls are FRESH (post-pull mtime), so the
                e = _parse(p.read_text())    # mtime gate alone would never catch them
                f = e.get("file", "")
            except OSError:
                continue
            if f:
                baseline[f] = min(baseline.get(f, mt), mt)
                if not e.get("content_hash"):
                    dirty.add(f)
        for rel, base_mt in baseline.items():
            src = root / rel
            try:
                cur = src.stat().st_mtime_ns
            except OSError:                  # deleted → drop all its symbols + its edges
                self.drop_file(proj, rel)
                continue
            if rel in dirty or cur > base_mt:   # merge-dirtied, or edited after indexing
                try:                         # (content_hash gate no-ops if unchanged)
                    reindex(root, rel, proj, patch_edges=True)
                except Exception:  # noqa: BLE001 — keep the stale entry over a failed query
                    pass

    def drop_file(self, proj: str, relpath: str) -> None:
        """Remove a deleted file's symbols and strip edges that originated from it —
        under the per-project lock (a delete mutates the same cross-file edges a
        concurrent reindex does), bumping the resident-cache epoch. Pure symbol_index
        mutation (no LSP), so its integrity invariants live with the state."""
        from .codeindex import SymbolIndex
        tag = f"[{relpath}]"
        with self.lock(proj):
            store = SymbolIndex(self.paths.project_dir(proj))
            for e in store.all():
                if e.get("file") == relpath:
                    store.delete(e["fqname"])
                    continue
                cb = [x for x in (e.get("called_by") or []) if not x.endswith(tag)]
                rf = [x for x in (e.get("references") or []) if not x.endswith(tag)]
                if cb != (e.get("called_by") or []) or rf != (e.get("references") or []):
                    e["called_by"], e["references"] = cb, rf
                    store.write(e)
        self.bump_epoch(proj)

    @staticmethod
    def patch_called_by(store: Any, new_entries: list[dict[str, Any]],
                        relpath: str) -> None:
        """Keep the cross-file call graph consistent after a single-file reindex: every
        `A→B` in the reindexed file A's fresh outbound `calls` must show as `A` in B's
        `called_by`. Strip stale edges originating from A (`… [A]`), then re-add the
        current ones. Cheap (in-memory from A's calls; no extra LSP)."""
        tag = f"[{relpath}]"
        entries = store.all()
        by_key = {(e.get("name", ""), e.get("file", "")): e for e in entries}
        changed: dict[str, dict] = {}
        for e in entries:                                   # 1) strip edges from A
            if e.get("file") == relpath:
                continue
            cb = [x for x in (e.get("called_by") or []) if not x.endswith(tag)]
            if cb != (e.get("called_by") or []):
                e["called_by"] = cb
                changed[e["fqname"]] = e
        for s in new_entries:                               # 2) re-add A's current edges
            for call in s.get("calls") or []:
                name, _, rest = call.partition(" [")
                tgt = by_key.get((name.strip(), rest.rstrip("]")))
                if tgt is None or tgt.get("file") == relpath:
                    continue
                e = changed.get(tgt["fqname"], tgt)
                edge = f"{s['name']} [{relpath}]"
                if edge not in (e.get("called_by") or []):
                    e["called_by"] = sorted(set((e.get("called_by") or []) + [edge]))
                    changed[e["fqname"]] = e
        for e in changed.values():
            store.write(e)
