# Plan: retrieval-quality investigation (notes path MRR)

Status: queued behind the feature builds (design-plan-tracking,
repo-local-storage); pull forward at will. Method: everything measured, per
DESIGN §10.3/10.4 discipline — no lever ships without an A/B on the gold
sets.

## Baseline (2026-08-05, quiet tree, daemon on dev@abd7c0a)

`scripts/eval_retrieval.py` against the live daemon, post-recovery corpus
(in-situ docs restored):

- **MRR 0.710, recall@3 0.806** (bars 0.7 / 0.8 — met, barely; historical
  bar was 0.72 before the corpus-drift re-baseline).
- Soft spots by need group: `distill` 2/3, `id-index-file` 1/2 (worst=4),
  `⚠ weak phrasing` on both — plus the standing observation that MRR has
  hovered at/below ideal for a while (maintainer, 2026-08-05).

## Levers, in promise order

1. **Per-need failure analysis first (no code).** Re-run with the failing
   phrasings isolated; for each, inspect what ranked above the gold —
   wrong-note-strong-match (fusion problem) vs right-note-weak-section
   (chunking/facet problem) vs vocabulary gap (reranker problem). This
   classification decides which lever below gets built. Artifacts: a short
   findings note per group stored in the cribsheet project.
2. **llm-judge reranker tier** (DESIGN §10.4, designed-not-built): logprob
   yes/no via an OpenAI-compatible endpoint (llama.cpp local lead;
   Anthropic text-score secondary). Integration seam = the zsh-ai provider
   pattern (endpoint/api_key_env/adapter dispatch). It is the designed
   answer to vocab-gap queries that MiniLM provably can't crack (the
   credentials≠tokens class). Gate: must beat MiniLM on the n=1876 notes
   gold set AND the live 31-phrasing set — the two heavier fastembed
   cross-encoders already failed that bar, so a new stage re-clears it or
   doesn't ship.
3. **Facet coverage audit.** summary_index/keyword_index aliases lift only
   where they exist (+0.013 MRR measured, capped by coverage). Measure
   coverage across the current corpus — especially the ~2k restored in-situ
   doc chunks — and run the elaborate/keyword backlog to fill gaps; then
   re-eval. Cheap, possibly the whole story for the `id-*` groups.
4. **Weight re-sweep on the live corpus.** `keyword_weight`/
   `summary_weight` are corpus-dependent (stored finding:
   volume-corpus-retrieval-lift note). The corpus just changed shape
   (doc-chunk share way up); re-sweep on the harvested gold set.
5. **Machine-aware rerank stage selection** (DESIGN §10.4 open item):
   pick MiniLM vs heavier vs llm-judge by host capability + endpoint
   reachability. Only after 2 proves a second stage worth selecting.

## Non-goals

- No RRF revival, no rank-bonus alias ports — both measured strictly worse
  (DESIGN §10.3).
- No new bar lowering: if a lever lands, the MRR bar goes back up to 0.72
  rather than the ratchet slipping further.
