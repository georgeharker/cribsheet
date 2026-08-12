"""Vector store behind one interface (DESIGN §10.1).

`InMemoryStore`  — brute-force cosine, dependency-free; default for dev/tests.
`ChromaStore`    — embedded PersistentClient or shared HttpClient.

The embedder is always client-side: we store and query by explicit vector, so a
shared `chroma run` never needs the embedding model.

The in-process stores are **thread-safe**: the daemon writes from worker threads
(indexing is offloaded off the event loop) while FastMCP's sync tools read from
its threadpool, so every `_recs` touch is under one reentrant lock and readers
iterate a snapshot taken under it.
"""

from __future__ import annotations

import functools
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar

from .errors import CribUserError


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
    def get_docs(self, where: dict[str, Any]
                 ) -> dict[str, tuple[str, dict[str, Any]]]:
        """Return {id: (document, metadata)} for matches — the corpus a lexical
        (BM25) index needs alongside the vector index."""
        ...
    def query(self, embedding: list[float], k: int,
              where: dict[str, Any] | None = None) -> list[Hit]: ...
    def current_dim(self) -> int | None:
        """Dimension of stored vectors, or None if empty — lets a full reindex
        detect an embedder change (e.g. a profile switch bge-small→bge-large)."""
        ...
    def recreate(self) -> None:
        """Drop all vectors so the next upserts define a fresh dimension. For a
        fixed-dim backend (Chroma) this recreates the collection; emptying the
        store also makes the content-hash gate re-embed every chunk."""
        ...


def _cosine(a: list[float], b: list[float]) -> float:
    """Dot product of two PRE-NORMALIZED vectors — cosine similarity.

    Dimensions must match: `zip` would otherwise truncate to the shorter one and
    return a plausible-looking score for vectors from two different embedders, so
    a profile flip (bge-small→bge-large) would silently degrade every ranking
    instead of failing. Chroma refuses a dim mismatch outright; the in-process
    stores have to say so themselves."""
    if len(a) != len(b):
        raise CribUserError(
            f"embedding dimension mismatch: query has {len(a)}, stored vector has "
            f"{len(b)} — the store holds vectors from a different embedder; run "
            "`crib project reconcile` to re-embed at the current dimension")
    return sum(x * y for x, y in zip(a, b))


def _matches(meta: dict[str, Any], where: dict[str, Any] | None) -> bool:
    return not where or all(meta.get(k) == v for k, v in where.items())


class InMemoryStore:
    def __init__(self) -> None:
        self._recs: dict[str, Record] = {}
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
        return {r.id: r.metadata for r in self._snapshot()
                if _matches(r.metadata, where)}

    def get_docs(self, where: dict[str, Any]
                 ) -> dict[str, tuple[str, dict[str, Any]]]:
        return {r.id: (r.document, r.metadata) for r in self._snapshot()
                if _matches(r.metadata, where)}

    def query(self, embedding: list[float], k: int,
              where: dict[str, Any] | None = None) -> list[Hit]:
        scored = [
            Hit(r.id, r.document, r.metadata, _cosine(embedding, r.embedding))
            for r in self._snapshot() if _matches(r.metadata, where)
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


class JsonStore(InMemoryStore):
    """Persistent brute-force store: InMemoryStore + a JSON file on disk.

    The fallback when Chroma is unavailable (or `[chroma].mode = "json"`) — fine
    for personal-scale memory, and what keeps the core loop testable with no
    backend installed. Chroma ships in the base install and is the default store.
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


_T = TypeVar("_T")

# Names Chroma has used for "that collection isn't there" across the versions we
# support (>=0.5): 1.x raises `chromadb.errors.NotFoundError`; the 0.5/0.6 line
# raised `InvalidCollectionException`. Resolved by name so an absent class on the
# installed version is simply skipped rather than an ImportError at import time.
_NOT_FOUND_ERROR_NAMES = ("NotFoundError", "InvalidCollectionException")


@functools.lru_cache(maxsize=1)
def _not_found_errors() -> tuple[type[BaseException], ...]:
    try:
        from chromadb import errors  # lazy: chroma is optional at runtime
    except Exception:  # noqa: BLE001 — no chroma installed, nothing to match
        return ()
    found = []
    for name in _NOT_FOUND_ERROR_NAMES:
        exc = getattr(errors, name, None)
        if isinstance(exc, type) and issubclass(exc, BaseException):
            found.append(exc)
    return tuple(found)


def _is_missing_collection(exc: Exception) -> bool:
    """True only for Chroma's "collection does not exist" — never for auth,
    connection, dimension or any other failure, which must propagate."""
    types = _not_found_errors()
    if types and isinstance(exc, types):
        return True
    # Pre-1.0 embedded clients surfaced a bare ValueError for a dropped
    # collection; keep that path recognizable without widening to Exception.
    return isinstance(exc, ValueError) and "does not exist" in str(exc)


class ChromaStore:
    """Embedded or shared Chroma. Collection has no embedding function.

    The collection handle is bound to a collection *UUID*, so it goes stale the
    moment any process calls `recreate()` (a reindex after an embedder-dim
    change). Every op therefore runs through `_run`, which re-resolves the
    handle and retries once when Chroma reports the collection missing.
    """

    COLLECTION = "crib_chunks"

    def __init__(self, client: Any) -> None:
        self._client = client
        self._col = self._resolve()

    def _resolve(self) -> Any:
        return self._client.get_or_create_collection(
            name=self.COLLECTION, metadata={"hnsw:space": "cosine"}
        )

    def _run(self, op: Callable[[Any], _T]) -> _T:
        """Run `op` against the collection, refreshing a stale handle once.

        Another process (shared mode, or daemon+CLI on one embedded dir) may have
        dropped and remade the collection under us; all ops here are idempotent,
        so a single retry on the fresh handle is safe.
        """
        try:
            return op(self._col)
        except Exception as exc:
            if not _is_missing_collection(exc):
                raise
        self._col = self._resolve()
        return op(self._col)

    def current_dim(self) -> int | None:
        res = self._run(lambda c: c.get(limit=1, include=["embeddings"]))
        embs = res.get("embeddings")
        return len(embs[0]) if embs is not None and len(embs) else None

    def recreate(self) -> None:
        """Drop and remake the collection — Chroma fixes a collection's dimension
        at first upsert, so an embedder change (new dim) needs a fresh one. In
        shared mode this affects every project's chunks, so it belongs only on a
        full reindex that re-embeds them all."""
        try:
            self._client.delete_collection(self.COLLECTION)
        except Exception as exc:
            # Already gone is the goal state; anything else (auth, connection,
            # server error) is a real failure the caller must see.
            if not _is_missing_collection(exc):
                raise
        self._col = self._resolve()

    @classmethod
    def embedded(cls, path: str) -> "ChromaStore":
        import chromadb  # lazy

        return cls(chromadb.PersistentClient(path=path))

    @classmethod
    def shared(cls, host: str, port: int) -> "ChromaStore":
        import chromadb  # lazy

        return cls(chromadb.HttpClient(host=host, port=port))

    def upsert(self, records: list[Record]) -> None:
        if not records:
            return
        self._run(lambda c: c.upsert(
            ids=[r.id for r in records],
            embeddings=[r.embedding for r in records],
            documents=[r.document for r in records],
            metadatas=[r.metadata for r in records],
        ))

    def delete(self, ids: list[str]) -> None:
        if ids:
            self._run(lambda c: c.delete(ids=ids))

    def set_meta(self, updates: dict[str, dict[str, Any]]) -> None:
        # Chroma updates metadata in place; embeddings/documents untouched.
        if updates:
            ids = list(updates)
            self._run(lambda c: c.update(
                ids=ids, metadatas=[updates[i] for i in ids]))

    def get_meta(self, where: dict[str, Any]) -> dict[str, dict[str, Any]]:
        where_clause = _chroma_where(where)
        res = self._run(
            lambda c: c.get(where=where_clause, include=["metadatas"]))
        ids = res.get("ids") or []
        metas = res.get("metadatas") or []
        return {i: m for i, m in zip(ids, metas)}

    def get_docs(self, where: dict[str, Any]
                 ) -> dict[str, tuple[str, dict[str, Any]]]:
        res = self._run(lambda c: c.get(where=_chroma_where(where),
                                        include=["documents", "metadatas"]))
        ids = res.get("ids") or []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        return {i: (d, m) for i, d, m in zip(ids, docs, metas)}

    def query(self, embedding: list[float], k: int,
              where: dict[str, Any] | None = None) -> list[Hit]:
        res = self._run(lambda c: c.query(
            query_embeddings=[embedding], n_results=k,
            where=_chroma_where(where) if where else None,
            include=["documents", "metadatas", "distances"],
        ))
        hits: list[Hit] = []
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        for i, d, m, dist in zip(ids, docs, metas, dists):
            hits.append(Hit(i, d, m, 1.0 - dist))  # cosine distance -> similarity
        return hits


def _chroma_where(where: dict[str, Any] | None) -> dict[str, Any] | None:
    if not where:
        return None
    if len(where) == 1:
        return where
    return {"$and": [{k: v} for k, v in where.items()]}
