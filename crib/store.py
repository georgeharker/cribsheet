"""Vector store behind one interface (DESIGN §10.1).

`InMemoryStore`  — brute-force cosine, dependency-free; the reference for tests.
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
from typing import Any, Protocol

from .errors import CribUserError
from .items import Item, dep_kind_default


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

    def apply(
        self,
        records: list[Record],
        deletes: list[str],
        meta: dict[str, dict[str, Any]],
        items: list[Item] | None = None,
        item_deletes: list[str] | None = None,
    ) -> None:
        """`upsert(records)`, `delete(deletes)` and `set_meta(meta)`, plus the
        item rows for the same files (`items` replaces each id's row and facet,
        `item_deletes` drops ids), as one atomic write, in that order."""
        ...

    def items(self, where: dict[str, Any]) -> dict[str, Item]:
        """{id: Item} for items whose core fields (`id`, `kind`, `project`,
        `relpath`) match `where`; values are exact or `{"$in": [...]}`."""
        ...

    def item_hashes(self, where: dict[str, Any]) -> dict[str, str]:
        """{id: content_hash} for items matching `where` (as `items`): the cheap
        read a sweep diffs against before writing only what changed."""
        ...

    def edges(self, where: dict[str, Any]) -> list[tuple[str, str, str, str | None]]:
        """(src, dst, kind, recorded_hash) for edges matching `where` on `src`,
        `dst` or `kind` (exact or `{"$in": [...]}`). A `dep` edge comes back
        with its RESOLVED kind: the default for what its target is."""
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
    def score_ids(self, embedding: list[float], ids: list[str]) -> dict[str, float]:
        """Cosine of `embedding` against the STORED vectors of `ids` (those that
        exist): scores candidates found another way on exactly the vectors the
        dense query ranks, with no re-embedding."""
        ...

    def current_dim(self) -> int | None:
        """Dimension of stored vectors, or None if empty — lets a full reindex
        detect an embedder change (e.g. a profile switch bge-small→bge-large)."""
        ...

    def recreate(self) -> None:
        """Drop all vectors so the next upserts define a fresh dimension. For a
        fixed-dim backend (DuckDB) this drops the table; emptying the
        store also makes the content-hash gate re-embed every chunk."""
        ...

    def count(self, where: dict[str, Any], relpath_prefix: str = "") -> int:
        """How many records match `where` (and, if given, whose `relpath` starts
        with `relpath_prefix`), without fetching them."""
        ...

    def projects(self) -> set[str]:
        """Every `project` that has at least one record."""
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


def _match_one(have: Any, want: Any) -> bool:
    """One `where` entry, the same semantics as the DuckDB store's: a plain value
    matches only an equal value of the same type (`True` is not `1`, `1` is not
    `1.0`, `None` matches nothing); `{"$any": [...]}` matches a list sharing an
    element with it; `{"$in": [...]}` matches a string that is one of them."""
    if isinstance(want, dict):
        if set(want) not in ({"$any"}, {"$in"}):
            raise CribUserError(f"unsupported filter {want!r}")
        (op, wanted), = want.items()
        if not isinstance(wanted, (list, tuple)) or not all(
            isinstance(v, str) for v in wanted
        ):
            raise CribUserError(f'"{op}" takes a list of strings, got {wanted!r}')
        if op == "$in":
            return isinstance(have, str) and have in wanted
        return isinstance(have, list) and bool(set(wanted) & set(have))
    if want is None or isinstance(want, (list, dict)):
        return False
    return type(have) is type(want) and have == want


ITEM_KEYS = ("id", "kind", "project", "relpath")  # what `Store.items` filters on
EDGE_KEYS = ("src", "dst", "kind")  # what `Store.edges` filters on


def _check_edge_where(where: dict[str, Any]) -> None:
    if bad := set(where) - set(EDGE_KEYS):
        raise CribUserError(f"edges can be filtered only on {EDGE_KEYS}, not {sorted(bad)}")


def resolve_edge_kind(kind: str, target: Item | None) -> str:
    """A `dep` edge's kind is its target's default; every other kind is as written."""
    return dep_kind_default(target.kind if target else None) if kind == "dep" else kind


def _check_item_where(where: dict[str, Any]) -> None:
    if bad := set(where) - set(ITEM_KEYS):
        raise CribUserError(f"items can be filtered only on {ITEM_KEYS}, not {sorted(bad)}")


def _matches(meta: dict[str, Any], where: dict[str, Any] | None) -> bool:
    return not where or all(_match_one(meta.get(k), v) for k, v in where.items())


class InMemoryStore:
    def __init__(self) -> None:
        self._recs: dict[str, Record] = {}
        self._items: dict[str, Item] = {}
        self._meta: dict[str, str] = {}
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
        # Best first, ties by id: the DuckDB store's order, so backends agree.
        scored.sort(key=lambda h: (-h.score, h.id))
        return scored[: max(k, 0)]

    def score_ids(self, embedding: list[float], ids: list[str]) -> dict[str, float]:
        wanted = set(ids)
        return {
            r.id: _cosine(embedding, r.embedding)
            for r in self._snapshot()
            if r.id in wanted
        }

    def current_dim(self) -> int | None:
        for r in self._snapshot():
            return len(r.embedding)
        return None

    def recreate(self) -> None:
        with self._lock:
            self._recs.clear()
            self._items.clear()  # item vectors came from the same embedder

    def apply(
        self,
        records: list[Record],
        deletes: list[str],
        meta: dict[str, dict[str, Any]],
        items: list[Item] | None = None,
        item_deletes: list[str] | None = None,
    ) -> None:
        with self._lock:
            self.upsert(records)
            self.delete(deletes)
            self.set_meta(meta)
            for item_id in item_deletes or ():
                self._items.pop(item_id, None)
            for item in items or ():
                self._items[item.id] = item

    def item_hashes(self, where: dict[str, Any]) -> dict[str, str]:
        return {i: it.content_hash for i, it in self.items(where).items()}

    def edges(self, where: dict[str, Any]) -> list[tuple[str, str, str, str | None]]:
        _check_edge_where(where)
        with self._lock:
            rows = [
                (it.id, e.dst, resolve_edge_kind(e.kind, self._items.get(e.dst)),
                 e.recorded_hash)
                for it in self._items.values() for e in it.edges
            ]
        keys = {"src": 0, "dst": 1, "kind": 2}
        return sorted(r for r in rows
                      if all(_match_one(r[keys[k]], v) for k, v in where.items()))

    def items(self, where: dict[str, Any]) -> dict[str, Item]:
        _check_item_where(where)
        with self._lock:
            return {
                i: it for i, it in self._items.items()
                if all(_match_one(getattr(it, k), v) for k, v in where.items())
            }

    def count(self, where: dict[str, Any], relpath_prefix: str = "") -> int:
        return sum(
            1
            for r in self._snapshot()
            if _matches(r.metadata, where)
            and str(r.metadata.get("relpath") or "").startswith(relpath_prefix)
        )

    def projects(self) -> set[str]:
        return {
            p
            for r in self._snapshot()
            if isinstance(p := r.metadata.get("project"), str)
        }

    def meta_get(self, key: str) -> str | None:
        return self._meta.get(key)

    def meta_set(self, key: str, value: str) -> None:
        self._meta[key] = value
