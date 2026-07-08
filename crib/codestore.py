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

import threading
from typing import Any


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
    """The code subsystem's shared per-project state, in one place (see module docstring)."""

    def __init__(self) -> None:
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
