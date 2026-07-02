"""Vector store behind one interface (DESIGN §10.1).

`InMemoryStore`  — brute-force cosine, dependency-free; default for dev/tests.
`ChromaStore`    — embedded PersistentClient or shared HttpClient.

The embedder is always client-side: we store and query by explicit vector, so a
shared `chroma run` never needs the embedding model.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


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
    return sum(x * y for x, y in zip(a, b))  # vectors are pre-normalized


def _matches(meta: dict[str, Any], where: dict[str, Any] | None) -> bool:
    return not where or all(meta.get(k) == v for k, v in where.items())


class InMemoryStore:
    def __init__(self) -> None:
        self._recs: dict[str, Record] = {}

    def upsert(self, records: list[Record]) -> None:
        for r in records:
            self._recs[r.id] = r

    def delete(self, ids: list[str]) -> None:
        for i in ids:
            self._recs.pop(i, None)

    def set_meta(self, updates: dict[str, dict[str, Any]]) -> None:
        for i, meta in updates.items():
            if i in self._recs:
                self._recs[i].metadata = meta

    def get_meta(self, where: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {i: r.metadata for i, r in self._recs.items()
                if _matches(r.metadata, where)}

    def get_docs(self, where: dict[str, Any]
                 ) -> dict[str, tuple[str, dict[str, Any]]]:
        return {i: (r.document, r.metadata) for i, r in self._recs.items()
                if _matches(r.metadata, where)}

    def query(self, embedding: list[float], k: int,
              where: dict[str, Any] | None = None) -> list[Hit]:
        scored = [
            Hit(r.id, r.document, r.metadata, _cosine(embedding, r.embedding))
            for r in self._recs.values() if _matches(r.metadata, where)
        ]
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:k]

    def current_dim(self) -> int | None:
        for r in self._recs.values():
            return len(r.embedding)
        return None

    def recreate(self) -> None:
        self._recs.clear()


class JsonStore(InMemoryStore):
    """Persistent brute-force store: InMemoryStore + a JSON file on disk.

    The dependency-free default when Chroma isn't installed — fine for personal-
    scale memory, and it makes crib fully usable with zero heavy deps.
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
        import json
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps([vars(r) for r in self._recs.values()]))
        tmp.replace(self._path)

    def upsert(self, records: list[Record]) -> None:
        super().upsert(records)
        self._save()

    def delete(self, ids: list[str]) -> None:
        super().delete(ids)
        self._save()

    def set_meta(self, updates: dict[str, dict[str, Any]]) -> None:
        super().set_meta(updates)
        self._save()

    def recreate(self) -> None:
        super().recreate()
        self._save()


class ChromaStore:
    """Embedded or shared Chroma. Collection has no embedding function."""

    COLLECTION = "crib_chunks"

    def __init__(self, client: Any) -> None:
        self._client = client
        self._col = client.get_or_create_collection(
            name=self.COLLECTION, metadata={"hnsw:space": "cosine"}
        )

    def current_dim(self) -> int | None:
        res = self._col.get(limit=1, include=["embeddings"])
        embs = res.get("embeddings")
        return len(embs[0]) if embs is not None and len(embs) else None

    def recreate(self) -> None:
        """Drop and remake the collection — Chroma fixes a collection's dimension
        at first upsert, so an embedder change (new dim) needs a fresh one. In
        shared mode this affects every project's chunks, so it belongs only on a
        full reindex that re-embeds them all."""
        try:
            self._client.delete_collection(self.COLLECTION)
        except Exception:  # noqa: BLE001 — absent/already-gone is fine
            pass
        self._col = self._client.get_or_create_collection(
            name=self.COLLECTION, metadata={"hnsw:space": "cosine"}
        )

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
        self._col.upsert(
            ids=[r.id for r in records],
            embeddings=[r.embedding for r in records],
            documents=[r.document for r in records],
            metadatas=[r.metadata for r in records],
        )

    def delete(self, ids: list[str]) -> None:
        if ids:
            self._col.delete(ids=ids)

    def set_meta(self, updates: dict[str, dict[str, Any]]) -> None:
        # Chroma updates metadata in place; embeddings/documents untouched.
        if updates:
            ids = list(updates)
            self._col.update(ids=ids, metadatas=[updates[i] for i in ids])

    def get_meta(self, where: dict[str, Any]) -> dict[str, dict[str, Any]]:
        where_clause = _chroma_where(where)
        res = self._col.get(where=where_clause, include=["metadatas"])
        ids = res.get("ids") or []
        metas = res.get("metadatas") or []
        return {i: m for i, m in zip(ids, metas)}

    def get_docs(self, where: dict[str, Any]
                 ) -> dict[str, tuple[str, dict[str, Any]]]:
        res = self._col.get(where=_chroma_where(where),
                            include=["documents", "metadatas"])
        ids = res.get("ids") or []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        return {i: (d, m) for i, d, m in zip(ids, docs, metas)}

    def query(self, embedding: list[float], k: int,
              where: dict[str, Any] | None = None) -> list[Hit]:
        res = self._col.query(
            query_embeddings=[embedding], n_results=k,
            where=_chroma_where(where) if where else None,
            include=["documents", "metadatas", "distances"],
        )
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
