# Frozen gold data for the retrieval eval scripts

Captured during the fusion/keyword prototyping session that settled the
coverage-gated, dense-dominant `code_lookup` design (see DESIGN.md §10.3–10.4).
Committed so `eval_kw.py`, `eval_hybrid.py`, and `eval_rigor.py` are standalone
and their published numbers reproducible.

| file | shape | role |
|---|---|---|
| `kws.json` | `project::fqname → [keyword phrase…]` (cribsheet + mcp-companion) | frozen synth keywords — the expanded-BM25 field under test |
| `kws_music-llm.json`, `kws_svg-mcp.json` | same | cross-domain generalization corpora |
| `gold_large.json` | `project::fqname → [query…]` | larger, vocabulary-shifted gold set (eval_rigor/eval_hybrid) |
| `queries_music-llm.json`, `queries_svg-mcp.json` | per-corpus gold queries | cross-domain gold sets |

These are a FROZEN measurement substrate, not live data: the production index now
generates `keywords` natively (one LLM pass with the describe), so regenerating
this data would measure a different (current) keyword generator. Keep it frozen
to compare fusion variants apples-to-apples; recapture deliberately if the
keyword prompt itself is what you're evaluating.

The scripts also need the named projects indexed locally (`crib project index`)
— the gold keys resolve against your resident symbol index.
