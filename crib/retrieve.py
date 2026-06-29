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


class CrossEncoderReranker:
    """A cross-encoder that re-scores (query, passage) pairs jointly (DESIGN
    §10.3). Unlike the bi-encoder (which embeds query and passage independently),
    it attends across both, so it bridges vocabulary gaps that cosine and BM25
    both miss — the precision stage over a recall-oriented candidate set.

    Wraps fastembed's ONNX `TextCrossEncoder` (CPU-pinned), lazy so the model
    loads once and stays warm in the daemon.
    """

    def __init__(self, model_name: str) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # lazy

        try:
            self._model = TextCrossEncoder(
                model_name, providers=["CPUExecutionProvider"])
        except TypeError:  # older fastembed without a providers kwarg
            self._model = TextCrossEncoder(model_name)

    def scores(self, query: str, documents: list[str]) -> list[float]:
        """Relevance score per document (higher = more relevant). Not comparable
        to cosine — use it to *order*, not to threshold."""
        return [float(s) for s in self._model.rerank(query, documents)]


class QwenReranker:
    """Qwen3-Reranker (a causal-LM reranker) via transformers/torch.

    Not a sequence-classification cross-encoder: it judges each (query, document)
    as a yes/no question and we read the probability of "yes". Heavier than the
    ONNX MiniLM path (a ~0.6B LM forward per pair on CPU), but a stronger judge.
    """

    _PREFIX = ("<|im_start|>system\nJudge whether the Document meets the "
               "requirements based on the Query and the Instruct provided. Note "
               'that the answer can only be "yes" or "no".<|im_end|>\n'
               "<|im_start|>user\n")
    _SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    _INSTRUCT = "Given a search query, retrieve relevant passages that answer it"

    def __init__(self, model_name: str) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer  # lazy

        self._torch = torch
        self._tok = AutoTokenizer.from_pretrained(model_name, padding_side="left")
        self._model = AutoModelForCausalLM.from_pretrained(model_name).eval()
        self._yes = self._tok.convert_tokens_to_ids("yes")
        self._no = self._tok.convert_tokens_to_ids("no")

    def scores(self, query: str, documents: list[str]) -> list[float]:
        torch = self._torch
        out: list[float] = []
        with torch.no_grad():
            for doc in documents:
                body = (f"<Instruct>: {self._INSTRUCT}\n<Query>: {query}\n"
                        f"<Document>: {doc}")
                text = self._PREFIX + body + self._SUFFIX
                inputs = self._tok(text, return_tensors="pt", truncation=True,
                                   max_length=2048)
                last = self._model(**inputs).logits[0, -1]
                pair = torch.stack([last[self._no], last[self._yes]])
                out.append(float(torch.softmax(pair, dim=0)[1]))   # P(yes)
        return out


def build_reranker(model_name: str):
    """Pick a reranker backend by model name: Qwen → transformers LM judge,
    everything else → fastembed ONNX cross-encoder."""
    if "qwen" in model_name.lower():
        return QwenReranker(model_name)
    return CrossEncoderReranker(model_name)


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
