"""Vector store behind one interface (DESIGN §10.1).

`InMemoryStore`  — brute-force cosine, dependency-free; default for dev/tests.
`JsonStore`      — InMemoryStore persisted to a JSON file.
`DuckDBStore` / `QuackStore` (crib.store_duckdb / crib.store_quack) — the real
store: an embedded DuckDB file, or that file served over Quack.

The embedder is always client-side: we store and query by explicit vector, so a
store server never needs the embedding model. Every store is a rebuildable
cache of what is on disk.

The in-process stores are **thread-safe**: the daemon writes from worker threads
(indexing is offloaded off the event loop) while FastMCP's sync tools read from
its threadpool, so every `_recs` touch is under one reentrant lock and readers
iterate a snapshot taken under it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .errors import CribUserError


def is_loopback_host(host: str) -> bool:
    """True when `host` can only be reached from this machine: `localhost`,
    127.0.0.0/8 or ::1. Any other name is treated as NOT loopback without
    resolving it — a hostname can resolve to a routable address."""
    import ipaddress

    h = (host or "").strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


@dataclass
class Record:
    id: str
    embedding: list[float]
    document: str
    metadata: dict[str, Any]


@dataclass
class Hit:
    id: str
    document: str
    metadata: dict[str, Any]
    score: float  # cosine similarity, higher = closer
    # Per-hit signal annotation ("which route drove this hit"): the route map
    # from retrieval, e.g. {"dense": 0.91, "sparse": 0.37, "alias": "asks"} —
    # dense = raw cosine (alias-promoted values carry the alias's trust scale),
    # sparse = the min-max'd BM25 term that entered the blend, alias = the label
    # whose alias vector won the MAX merge. None when the producer doesn't track
    # routes (e.g. the raw vector-store query path). Pure provenance — never
    # consumed by ranking — so the blended `score` stays the single source of order.
    signals: dict[str, Any] | None = None


class Store(Protocol):
    def upsert(self, records: list[Record]) -> None: ...
    def delete(self, ids: list[str]) -> None: ...
    def set_meta(self, updates: dict[str, dict[str, Any]]) -> None:
        """Replace metadata for the given ids WITHOUT re-embedding — for cheap
        metadata-schema/frontmatter drift when a chunk's content is unchanged."""
        ...

    def get_meta(self, where: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Return {id: metadata} for records matching `where` (exact-match)."""
        ...

    def get_docs(self, where: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
        """Return {id: (document, metadata)} for matches — the corpus a lexical
        (BM25) index needs alongside the vector index."""
        ...

    def query(
        self, embedding: list[float], k: int, where: dict[str, Any] | None = None
    ) -> list[Hit]: ...
    def current_dim(self) -> int | None:
        """Dimension of stored vectors, or None if empty — lets a full reindex
        detect an embedder change (e.g. a profile switch bge-small→bge-large)."""
        ...

    def recreate(self) -> None:
        """Drop all vectors so the next upserts define a fresh dimension. For a
        fixed-dim backend (DuckDB) this drops the table; emptying the
        store also makes the content-hash gate re-embed every chunk."""
        ...

    def meta_get(self, key: str) -> str | None:
        """A store-level setting, e.g. `embed_model` (which embedder wrote the
        stored vectors), or None if never set."""
        ...

    def meta_set(self, key: str, value: str) -> None: ...


def _cosine(a: list[float], b: list[float]) -> float:
    """Dot product of two PRE-NORMALIZED vectors — cosine similarity.

    Dimensions must match: `zip` would otherwise truncate to the shorter one and
    return a plausible-looking score for vectors from two different embedders, so
    a profile flip (bge-small→bge-large) would silently degrade every ranking
    instead of failing. DuckDB refuses a dim mismatch outright; the in-process
    stores have to say so themselves."""
    if len(a) != len(b):
        raise CribUserError(
            f"embedding dimension mismatch: query has {len(a)}, stored vector has "
            f"{len(b)} — the store holds vectors from a different embedder; run "
            "`crib project reconcile` to re-embed at the current dimension"
        )
    return sum(x * y for x, y in zip(a, b))


def _matches(meta: dict[str, Any], where: dict[str, Any] | None) -> bool:
    return not where or all(meta.get(k) == v for k, v in where.items())


class InMemoryStore:
    def __init__(self) -> None:
        self._recs: dict[str, Record] = {}
        self._meta: dict[str, str] = {}
        # Reentrant so JsonStore can hold it across super() + _save.
        self._lock = threading.RLock()

    def _snapshot(self) -> list[Record]:
        """The records to read, captured under the lock — a concurrent writer
        rebinds the dict's contents, never a record already handed out."""
        with self._lock:
            return list(self._recs.values())

    def upsert(self, records: list[Record]) -> None:
        with self._lock:
            for r in records:
                self._recs[r.id] = r

    def delete(self, ids: list[str]) -> None:
        with self._lock:
            for i in ids:
                self._recs.pop(i, None)

    def set_meta(self, updates: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            for i, meta in updates.items():
                if i in self._recs:
                    self._recs[i].metadata = meta

    def get_meta(self, where: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            r.id: r.metadata for r in self._snapshot() if _matches(r.metadata, where)
        }

    def get_docs(self, where: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
        return {
            r.id: (r.document, r.metadata)
            for r in self._snapshot()
            if _matches(r.metadata, where)
        }

    def query(
        self, embedding: list[float], k: int, where: dict[str, Any] | None = None
    ) -> list[Hit]:
        scored = [
            Hit(r.id, r.document, r.metadata, _cosine(embedding, r.embedding))
            for r in self._snapshot()
            if _matches(r.metadata, where)
        ]
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:k]

    def current_dim(self) -> int | None:
        for r in self._snapshot():
            return len(r.embedding)
        return None

    def recreate(self) -> None:
        with self._lock:
            self._recs.clear()

    def meta_get(self, key: str) -> str | None:
        return self._meta.get(key)

    def meta_set(self, key: str, value: str) -> None:
        self._meta[key] = value


class JsonStore(InMemoryStore):
    """Persistent brute-force store: InMemoryStore + a JSON file on disk.

    `[store] backend = "json"`: fine for personal-scale memory, and what keeps
    the core loop testable with no backend installed. DuckDB ships in the base
    install and is the default store.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self._load()

    def _load(self) -> None:
        import json

        if self._path.exists():
            for d in json.loads(self._path.read_text()):
                r = Record(d["id"], d["embedding"], d["document"], d["metadata"])
                self._recs[r.id] = r

    def _save(self) -> None:
        """Whole-file rewrite — callers must hold `_lock`, so two writer threads
        can't interleave into one tmp file or race the rename."""
        import json

        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps([vars(r) for r in self._recs.values()]))
        tmp.replace(self._path)

    def upsert(self, records: list[Record]) -> None:
        with self._lock:
            super().upsert(records)
            self._save()

    def delete(self, ids: list[str]) -> None:
        with self._lock:
            super().delete(ids)
            self._save()

    def set_meta(self, updates: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            super().set_meta(updates)
            self._save()

    def recreate(self) -> None:
        with self._lock:
            super().recreate()
            self._save()
