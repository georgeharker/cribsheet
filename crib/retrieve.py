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
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .store import Store

_TOKEN = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


# Function words carried by natural-language queries that shouldn't count toward the
# keyword-coverage gate (they match everything). Deliberately small — content words win.
STOPWORDS = frozenset({
    "the", "a", "an", "that", "to", "of", "for", "and", "or", "in", "on", "by", "its",
    "it", "is", "then", "so", "with", "into", "which", "given", "this", "them", "only",
    "before", "after", "up", "re", "back", "single", "one",
})


def _as_tf(doc) -> dict[str, float]:
    """A doc's term-frequency map. Accepts a token list (each token counts 1) or a
    pre-weighted mapping token→weight, so callers can down/up-weight sources —
    e.g. elaboration terms below body terms."""
    if isinstance(doc, dict):
        return {k: float(v) for k, v in doc.items()}
    return {k: float(v) for k, v in Counter(doc).items()}


class BM25:
    """Okapi BM25 over a fixed corpus. Each doc is a token list OR a weighted
    term-frequency mapping (token→float); weighted docs let a caller score some
    token sources below others (e.g. LLM-elaboration terms at <1.0).

    `k1` controls term-frequency saturation, `b` the document-length penalty —
    the standard defaults are fine for prose and code.
    """

    def __init__(self, corpus, k1: float = 1.5, b: float = 0.75):
        self.corpus = corpus
        self.k1, self.b = k1, b
        self.n = len(corpus)
        self.tf: list[dict[str, float]] = [_as_tf(d) for d in corpus]
        # doc length = total (weighted) term mass; equals token count for lists.
        self.lengths = [sum(t.values()) for t in self.tf]
        self.avgdl = (sum(self.lengths) / self.n) if self.n else 0.0
        df: Counter[str] = Counter()
        for t in self.tf:
            df.update(t.keys())
        # BM25+ idf: always positive, so common terms still rank, never subtract.
        self.idf = {t: math.log(1 + (self.n - c + 0.5) / (c + 0.5))
                    for t, c in df.items()}

    def coverage(self, qtokens: set[str]) -> list[float]:
        """Fraction of the query's informative tokens present in each doc's field
        (∈ [0,1]) — the covpc gate (same shape as the code path's
        `_ResidentCode.coverage`): demotes a diffuse BM25 match that shares only a
        stray rare token with the query."""
        n = max(len(qtokens), 1)
        return [sum(1 for t in qtokens if t in tf) / n for tf in self.tf]

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


def _lexical_text(document: str, meta: dict | None) -> str:
    """BM25 corpus text: the section's heading breadcrumb (from metadata)
    prepended to the body, mirroring the dense side's `Chunk.index_text` so a
    keyword query matching only a section's *subject* still scores. Applies on
    the next cache rebuild — no re-embed needed (docs/retrieval-and-adoption.md §3)."""
    head = (meta or {}).get("heading_path", "")
    return f"{head.replace('/', ' ')}\n{document}" if head else document


# Split a token on camelCase / PascalCase / acronym boundaries (the tokenizer
# keeps these whole because there's no separator). `_TOKEN` already keeps
# underscores, so snake_case is handled by splitting on "_" first.
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+|[0-9]+")


def _subtokens(text: str) -> list[str]:
    """Component words of compound identifiers the tokenizer keeps whole —
    snake_case (`_` is a word char) and camel/PascalCase (no separator at all).
    So a spaced query ("restart server", "index file", "lexical cache") matches a
    single-token identifier (`:MCPRestartServer`, `index_file`, `LexicalCache`).
    Only genuine compounds contribute — plain words are already indexed from the
    body. Tier-1 keyword sidecar (docs/retrieval-and-adoption.md §3): on-the-fly,
    no storage, always current."""
    extra: list[str] = []
    for tok in _TOKEN.findall(text):
        parts = [p for seg in tok.split("_") for p in _CAMEL.findall(seg)]
        if len(parts) > 1:
            extra.extend(p.lower() for p in parts)
    return extra


def _lexical_tokens(document: str, meta: dict | None,
                    extra_terms: list[str] | None = None) -> list[str]:
    """Body BM25 tokens for one chunk: heading-enriched body plus the split
    components of any compound identifiers. `extra_terms` (elaboration terms) are
    appended at full weight — for the weighted path use `_lexical_tf` instead."""
    text = _lexical_text(document, meta)
    toks = tokenize(text) + _subtokens(text)
    if extra_terms:
        blob = " ".join(extra_terms)
        toks += tokenize(blob) + _subtokens(blob)
    return toks


def _lexical_tf(document: str, meta: dict | None,
                extra_terms: list[str] | None = None,
                extra_weight: float = 1.0) -> dict[str, float]:
    """Weighted term-frequency for one chunk: body+heading+subtokens at weight
    1.0, plus `extra_terms` (LLM elaborations) at `extra_weight` — so elaboration
    tokens can be scored below body tokens (§3.1). At weight 1.0 this is
    equivalent to counting `_lexical_tokens`."""
    text = _lexical_text(document, meta)
    tf: dict[str, float] = {}
    for tok in tokenize(text) + _subtokens(text):
        tf[tok] = tf.get(tok, 0.0) + 1.0
    if extra_terms and extra_weight:
        blob = " ".join(extra_terms)
        for tok in tokenize(blob) + _subtokens(blob):
            tf[tok] = tf.get(tok, 0.0) + extra_weight
    return tf


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60,
                           weights: list[float] | None = None) -> list[str]:
    """Fuse ranked id-lists (each best-first) into one ranking by RRF score.

    `weights` (one per ranking, default all 1.0) scales each list's vote — so a
    noisy-but-useful signal (e.g. summary alias vectors) can contribute below the
    primary dense/sparse lists instead of swamping them by equal vote."""
    ws = weights or [1.0] * len(rankings)
    fused: dict[str, float] = {}
    for ranking, w in zip(rankings, ws):
        for rank, doc_id in enumerate(ranking):
            fused[doc_id] = fused.get(doc_id, 0.0) + w / (k + rank + 1)
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

    Keyed by ``(project, labels, weight)``: a corpus that folds in a given set of
    keyword_index labels (§3.1) at a given weight is cached separately, so the
    warm config-default corpus and an eval's per-run overrides coexist without
    clobbering each other. ``keyword_terms(project, section_hash, labels)``
    supplies the per-**section** keyword terms (section-identified, so they
    survive re-windowing; None/no-labels → plain body+heading BM25). Alias
    (summary_index) records are skipped — those feed the dense side, not BM25.
    """

    def __init__(self, store: "Store",
                 keyword_terms: Callable[[str, str, tuple[str, ...]],
                                         list[str]] | None = None) -> None:
        self._store = store
        self._elab = keyword_terms
        # (project, labels, weight) -> (ids, {id:(doc,meta)}, BM25)
        self._entries: dict[tuple[str, tuple[str, ...], float],
                            tuple[list[str], dict, BM25]] = {}

    def invalidate(self, project: str) -> None:
        for key in [k for k in self._entries if k[0] == project]:
            del self._entries[key]

    def get(self, project: str, labels: tuple[str, ...] = (),
            weight: float = 1.0) -> tuple[list[str], dict, BM25]:
        labels = tuple(labels)
        key = (project, labels, weight)
        entry = self._entries.get(key)
        if entry is None:
            docs = {i: (d, m) for i, (d, m)
                    in self._store.get_docs({"project": project}).items()
                    if not (m or {}).get("alias")}   # dense-only summary aliases
            ids = list(docs)
            corpus: list[dict[str, float]] = []
            for i in ids:
                doc, meta = docs[i]
                extra: list[str] | None = None
                if labels and self._elab is not None:
                    # section-identified; fall back to content_hash pre-reindex
                    sh = (meta or {}).get("section_hash") \
                        or (meta or {}).get("content_hash", "")
                    if sh:
                        extra = self._elab(project, sh, labels)
                corpus.append(_lexical_tf(doc, meta, extra, weight))
            entry = (ids, docs, BM25(corpus))
            self._entries[key] = entry
        return entry


class SummaryVectorCache:
    """Warm per-(project, labels) cache of summary_index **alias vectors** — the
    dense counterpart to `LexicalCache` (§3). For active summary labels it embeds
    each section's LLM rephrasings once and holds the vectors, so a paraphrased
    query can match a section via cosine on its summary even when query and body
    share no tokens. Keyed by (project, labels) so an eval can A/B label sets
    without a rebuild; invalidated with the project on any write.

    Not persisted: the summaries (text) are the git-tracked asset; these vectors
    are derived and rebuilt on demand (like BM25 tokens). `summary_terms(project,
    section_hash, labels)` supplies the per-section rephrasings.
    """

    def __init__(self, store: "Store", embedder,
                 summary_terms: Callable[[str, str, tuple[str, ...]],
                                         list[str]] | None = None) -> None:
        self._store = store
        self._embed = embedder
        self._sum = summary_terms
        # (project, labels) -> (section_hash -> [chunk_id], [(section_hash, vec)])
        self._entries: dict[tuple[str, tuple[str, ...]],
                            tuple[dict[str, list[str]], list[tuple[str, list[float]]]]] = {}

    def invalidate(self, project: str) -> None:
        for key in [k for k in self._entries if k[0] == project]:
            del self._entries[key]

    def get(self, project: str, labels: tuple[str, ...]
            ) -> tuple[dict[str, list[str]], list[tuple[str, list[float]]]]:
        key = (project, tuple(labels))
        entry = self._entries.get(key)
        if entry is None:
            reps: dict[str, list[str]] = {}
            docs = self._store.get_docs({"project": project})
            for cid, (_doc, meta) in docs.items():
                if (meta or {}).get("alias"):
                    continue
                sh = (meta or {}).get("section_hash") or (meta or {}).get("content_hash")
                if sh:
                    reps.setdefault(sh, []).append(cid)
            pairs: list[tuple[str, str]] = []
            if labels and self._sum is not None:
                for sh in reps:
                    for t in self._sum(project, sh, labels):
                        pairs.append((sh, t))
            vecs: list[tuple[str, list[float]]] = []
            if pairs:
                embs = self._embed.embed([t for _, t in pairs])
                vecs = [(pairs[j][0], embs[j]) for j in range(len(pairs))]
            entry = (reps, vecs)
            self._entries[key] = entry
        return entry

    def best_cosines(self, project: str, labels: tuple[str, ...],
                     query_vec: list[float]) -> dict[str, float]:
        """MAX query↔alias cosine per section, keyed by a representative chunk id.

        The multi-vector semantics the index was built for: an alias embedding is
        ANOTHER dense vector pointing at the same section, so a query matching an
        alias IS a dense match for that section — the caller folds these into the
        dense arm by max, not into a separate ranked list (the old RRF-era port
        reduced aliases to a rank bonus on top of the BODY cosine, which under
        dense-dominant score fusion could never express a strong alias match —
        measured to only hurt, at any weight)."""
        reps, vecs = self.get(project, labels)
        best: dict[str, float] = {}
        for sh, v in vecs:
            c = sum(a * b for a, b in zip(query_vec, v))
            if c > best.get(sh, -2.0):
                best[sh] = c
        out: dict[str, float] = {}
        for sh, c in best.items():
            ids = reps.get(sh) or []
            if ids:
                out[ids[0]] = c      # section representative; dedupe collapses windows
        return out
