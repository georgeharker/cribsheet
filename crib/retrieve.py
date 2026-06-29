"""Hybrid retrieval: fuse a dense (vector) ranking with a sparse (BM25) one.

Dense embeddings nail paraphrase/semantics but underweight exact-term matches,
so terse keyword queries ("restart server") can rank generic-but-on-topic prose
above the section that literally documents the command. BM25 is the opposite —
it rewards exact terms. Fusing the two rankings recovers both.

We fuse with **Reciprocal Rank Fusion** (DESIGN §10.3): score by position in each
list, not by raw score, so the incomparable cosine and BM25 magnitudes never have
to be reconciled. `K` damps the contribution of low ranks (the standard value 60
means rank 1 ≈ 1/61, rank 10 ≈ 1/71 — a gentle, long-tailed weighting).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Store

_TOKEN = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


class BM25:
    """Okapi BM25 over a fixed corpus of tokenized documents.

    `k1` controls term-frequency saturation, `b` the document-length penalty —
    the standard defaults are fine for prose and code.
    """

    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.corpus = corpus
        self.k1, self.b = k1, b
        self.n = len(corpus)
        self.lengths = [len(d) for d in corpus]
        self.avgdl = (sum(self.lengths) / self.n) if self.n else 0.0
        self.tf: list[Counter[str]] = [Counter(d) for d in corpus]
        df: Counter[str] = Counter()
        for d in corpus:
            df.update(set(d))
        # BM25+ idf: always positive, so common terms still rank, never subtract.
        self.idf = {t: math.log(1 + (self.n - c + 0.5) / (c + 0.5))
                    for t, c in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        out = [0.0] * self.n
        for t in query:
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i, tf in enumerate(self.tf):
                f = tf.get(t)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.lengths[i] / self.avgdl)
                out[i] += idf * (f * (self.k1 + 1)) / denom
        return out


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60) -> list[str]:
    """Fuse ranked id-lists (each best-first) into one ranking by RRF score."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(fused, key=lambda d: fused[d], reverse=True)


class LexicalCache:
    """Per-project BM25 index, built lazily from the store and kept warm for the
    daemon's life (DESIGN §10.3). Rebuilding tokenizes the whole project corpus,
    so doing it every query wastes the daemon's warmth — instead we cache and
    invalidate on write.

    Invalidation is a dirty-flag drop, not an incremental edit: the write path
    (`IndexEngine.index_file`) calls `invalidate(project)` whenever it mutates a
    project, and the next query rebuilds. BM25 is a ranking aid over the vector
    store (the source of truth), so a momentarily stale index only delays a
    just-written chunk's lexical findability by one query — a far weaker
    correctness bar than the vector index, which makes the simple flag safe.

    One-shot callers (the `--no-daemon` CLI, tests) build a fresh Crib per run,
    so the cache is simply cold each time — no benefit, no harm.
    """

    def __init__(self, store: "Store") -> None:
        self._store = store
        # project -> (ids aligned to the BM25 corpus, {id: (doc, meta)}, BM25)
        self._entries: dict[str, tuple[list[str], dict, BM25]] = {}

    def invalidate(self, project: str) -> None:
        self._entries.pop(project, None)

    def get(self, project: str) -> tuple[list[str], dict, BM25]:
        entry = self._entries.get(project)
        if entry is None:
            docs = self._store.get_docs({"project": project})
            ids = list(docs)
            entry = (ids, docs, BM25([tokenize(docs[i][0]) for i in ids]))
            self._entries[project] = entry
        return entry
