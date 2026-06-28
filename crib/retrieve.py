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
