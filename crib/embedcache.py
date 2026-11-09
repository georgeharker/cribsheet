"""A persistent embedding cache: text -> vector, per embedding model.

crib re-embeds text it has embedded before at every daemon start: summary
alias vectors (SummaryVectorCache, deliberately not persisted) and code
symbol descriptions (the resident code index). Those vectors are a pure
function of (model, text), so `CachedEmbedder` wraps the embedder and stores
them in the DuckDB store, keyed by a hash of the exact text. A warm start
then embeds only text it has never seen.

Only VECTORS are stored, with no index: search builds whatever it needs in
memory (exact cosine today; an HNSW option would be built at load), so
nothing index-shaped is persisted. Query embeddings (embed_query) are not
cached: a query is one short string, embedded once.

The model key includes the model name and dimension, so a profile switch
(bge-small -> bge-large) never serves a stale vector. It comes from the BUILT
embedder's `model_id`, not from the config string, because `build_embedder`
falls back to the hash embedder when a backend is missing: keyed on the config,
hash vectors would be filed under the real model's name and go on being served
after the backend is installed. A hash embedder is not wrapped at all --- it
computes faster than the cache can be read.

Like every derived tier the cache is rebuildable: losing it costs one re-embed.
"""

from __future__ import annotations

import sys

import hashlib
from typing import Any, Protocol


class EmbedStorage(Protocol):
    def ec_get(self, model: str, hashes: list[str]) -> dict[str, list[float]]: ...
    def ec_put(self, model: str, rows: list[tuple[str, list[float]]]) -> None: ...


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CachedEmbedder:
    """`embedder` with `embed()` served from (and filling) a persistent cache.
    Everything else (`dim`, `embed_query`, any backend attribute) passes through."""

    def __init__(
        self, embedder: Any, storage: EmbedStorage, model: str | None = None
    ) -> None:
        self._inner = embedder
        self._s = storage
        # `model_id` says what was BUILT; `model` overrides it only for tests that
        # want to name a key directly.
        self.model_id: str = model or getattr(embedder, "model_id", "?")
        self.dim: int = getattr(embedder, "dim", 0)
        # Spelled off the same attributes callers read, so a wrapper and a bare
        # embedder answer `model_id`/`dim` identically.
        self.model_key = f"{self.model_id}#{getattr(embedder, 'dim', '?')}"
        self.hits = 0
        self.misses = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        hashes = [text_hash(t) for t in texts]
        try:
            cached = self._s.ec_get(self.model_key, sorted(set(hashes)))
        except Exception:  # noqa: BLE001 — a cache failure is just a miss
            cached = {}
        todo = sorted({h: t for h, t in zip(hashes, texts) if h not in cached}.items())
        if todo:
            vecs = self._inner.embed([t for _, t in todo])
            fresh = {h: [float(x) for x in v] for (h, _), v in zip(todo, vecs)}
            try:
                self._s.ec_put(self.model_key, list(fresh.items()))
            except Exception:  # noqa: BLE001 — persistence is an optimisation only
                pass
            cached = {**cached, **fresh}
        self.misses += len(todo)
        self.hits += len(texts) - len(todo)
        return [cached[h] for h in hashes]


def wrap(embedder: Any, store: Any) -> Any:
    """`embedder` wrapped in a persistent cache when `store` can hold one (the
    DuckDB stores); otherwise unchanged.

    A `HashEmbedder` is returned unwrapped: its vectors are a few hashes of a
    short string, cheaper to recompute than to fetch, and caching them is how a
    missing-backend fallback leaves hash vectors behind in the store."""
    from .embed import HashEmbedder

    if isinstance(embedder, HashEmbedder):
        return embedder
    if all(callable(getattr(store, m, None)) for m in ("ec_get", "ec_put")):
        cached = CachedEmbedder(embedder, store)
        # Vectors from any other embedder can never be read again: drop them,
        # so a profile switch doesn't leave the old model's cache behind.
        prune = getattr(store, "ec_prune", None)
        if callable(prune):
            try:
                if n := prune(cached.model_key):
                    print(f"[crib] embedding cache: dropped {n} vectors from "
                          "other embedders", file=sys.stderr)
            except Exception as e:  # noqa: BLE001 — a cache; never block startup
                print(f"[crib] embedding cache prune failed: {e}", file=sys.stderr)
        return cached
    return embedder
