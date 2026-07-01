# Retrieval quality & tool adoption (the prerequisite layer)

> ## Build state & how to resume (2026-07-01)
>
> **Shipped & tested (88 unit tests pass):**
> - **Generation bridge** (`crib/generate.py` over llmkit; providers/profiles TOML
>   like `models.toml`, default `~/.config/crib/models.toml` → zen-qwen). Powers
>   `distill` and the two index generators.
> - **keyword_index** (BM25 side): `crib elaborate <label>` → section-addressed
>   `keyword_index/<label>/<section_hash>.toml`, tokens folded into BM25 at
>   `keyword_weight`. **Default: `keyword_labels=["keywords"], keyword_weight=0.3`**
>   (measured best). Concurrent + timeout + progress + error-isolated generation.
> - **summary_index** (dense side): `crib summarize <label>` → LLM rephrasings
>   embedded as **alias vectors**, fused as a weighted third RRF list
>   (`summary_weight`). **Default OFF (`summary_labels=[]`)** — see finding below.
> - **section identity**: `section_hash` (window-invariant) keys both indexes;
>   metadata self-heals on reindex (`set_meta`, no re-embed); code-fence `#`
>   comments no longer parsed as headings.
> - **Eval harness**: `scripts/eval_retrieval.py --lift <kw> / --lift-summaries
>   <sum> [--elab-weight/--summary-weight]`, cases in `eval_retrieval.cases.json`.
>
> **Findings (cribsheet corpus, 71 sections, n=31 phrasings, baseline MRR 0.844 /
> recall@3 0.968):**
> - keyword_index `keywords@0.3`: the net-positive config (recall→1.0 in earlier
>   runs; MRR neutral-to-slightly-up). **Shipped default.**
> - summary_index: **net-negative at every weight** (best w=0.1: still −0.03
>   recall). Better *doc2query* summaries were *worse* (compete harder). Root
>   cause: **dense recall is already saturated** on this small clustered corpus —
>   aliases can only displace, not rescue. Needs a **larger/diverse corpus** to
>   show value → that's why `.crib` was added to zsh-ai, zdot, dotfiler,
>   sharedserver, svg-mcp, mcp-companion (import for volume). **Confirmed
>   net-positive on the volume corpus (§5.5, 2026-07-01): +0.024 MRR @ w=0.15–0.2,
>   recall held.** Also there: `keywords@0.3` (the shipped default) *hurts* diverse
>   corpora — retune to w=0.1; best combo `sum@0.2 + kw@0.1` = +0.034 MRR.
>
> **To resume on a new machine:**
> 1. Clone this repo + `git submodule update --init` (vendor/llmkit at 67e8465).
> 2. `uv sync` / `pip install -e '.[full,generate]'` then `pip install -e
>    './vendor/llmkit[anthropic]'` (zen adapter). Optional: `[embed]` for bge.
> 3. Config in dotfiles: `~/.config/crib/config.toml` (`[generate]` + `[retrieve]`
>    blocks) and `~/.config/crib/models.toml` (providers); `export OPENCODE_API_KEY`.
> 4. `crib pull` (notes from `georgeharker/.crib`), then `crib reindex --all`.
>    **Indexes are gitignored/regenerable** — rebuild with `crib elaborate keywords`
>    (+ `crib summarize summary` when testing dense) per project.
> 5. `crib import` inside each `.crib`-tagged repo to load the volume corpora, then
>    generate indexes + add eval cases spanning them to retest the summary hypothesis.
>
> **Open / next:** ✅ summary_index measured on the volume corpora — net-positive
> (§5.5). Next: **whole-doc-context generation** (author a doc's sections together,
> validate coverage, mop-up misses) to fix generic per-section keywords — pairs with
> the llmkit `chat`→TextIO **streaming sink** (still temp-file) and a new
> **structured-output** path on `ChatRequest` (tool/`response_format`) for conformant
> bulk output. Per-corpus `keyword_weight` (0.3 hurts diverse corpora, §5.5). Promote
> keyword_index to git-tracked once the default is stable.

Status: design. The substrate that must work **before** automatic capture
([knowledge-capture.md](knowledge-capture.md)) delivers any value: if `lookup`
doesn't surface the right note, and if the connected agent doesn't *consult the
tool* in the first place, capturing more knowledge just grows an index nobody
reads. Realizes/extends DESIGN [§10.3 retrieval](../DESIGN.md) and [§10.4
reranking].

## 1. The frame — two different problems, usually conflated

- **Findability** — given a query, does the right note rank at the top?
- **Invocation** — does a query get *issued at all*?

The design so far has invested almost entirely in findability — hybrid dense⊕BM25
fused by RRF (§10.3), an optional reranker (§10.4) — and it largely works:
**recall@3 = 100%** on the n=8 eval set. Invocation has essentially no design, and
it is the real gate: perfect retrieval delivers exactly zero value on every turn
where the agent greps the source tree instead of calling `lookup`.

> **Thesis:** the leverage is mostly on *invocation*, plus one specific findability
> gap (vocabulary mismatch). The rest of the retrieval machinery is already good
> enough. Capture sits behind both.

## 2. Why the agent greps instead of looking up

Honest account of the failure mode (the connected model — e.g. Claude Code —
defaulting to `grep`/`glob` over the codebase rather than crib):

- **Reliability asymmetry.** Grep is local and never fails. `lookup` is a network
  MCP call that can be down, slow, or empty. One bad experience trains the fallback
  permanently — the harness won't retry a dead MCP connection mid-session.
- **No trigger in context.** Nothing fires at the moment the agent is about to
  search. The global "consult first" directive is a weak, vague prior competing
  against the strong, concrete habit of searching code.
- **Cold-start emptiness.** Early on the store returns nothing, which *negatively
  reinforces* — the agent learns to stop trying.
- **Unclear value boundary.** The agent doesn't know which questions crib answers
  better than the code itself, so under ambiguity it picks the general tool (grep).

> **The crucial observation:** the harness's own `MEMORY.md` (§13) gets consulted
> reliably and crib does not — *not because it is better, but because it is
> injected into context, never looked up.* crib is strictly more capable and loses
> on delivery. The whole strategy below follows from inverting that.

## 3. Part A — Search-term efficacy (findability)

The one named weakness in §10.4 is the **vocabulary gap**: query "credentials" vs
note "tokens" — stays rank-2 under any *small* reranker. Attack it on the
**document side** (amortized, paid once at index time, zero query-time latency —
and the generation layer in [knowledge-capture.md](knowledge-capture.md) is the
natural home for the work):

> **Shipped first (2026-06-30): heading-breadcrumb injection** — the free, no-LLM
> precursor to item 1. A section's heading path (`"…/10.4 Reranking — options…"`) is
> already an authored topic phrase, but was indexed only as metadata, invisible to
> retrieval. `Chunk.index_text` now prepends it to the **embedding** input, and the
> BM25 corpus prepends it from metadata (`_lexical_text`, no re-embed needed); the
> stored `document` stays the clean body for display. `content_hash` folds it in so
> existing chunks re-embed on the next reindex. **Proven** in §5.2. This buys most of
> the cheap lift before any LLM is involved; items 1–4 layer on top once the bridge
> ([knowledge-capture.md](knowledge-capture.md) §2) exists.

1. **Canonical topic phrase per chunk** (do first — cheapest). The digestion pass
   emits a one-line "what is this about" headline, embedded with weight *and*
   reused as the snippet the agent sees. Doubles as a retrieval surface and a
   better read/skip signal.
2. **Doc2query / "what questions does this answer."** Generate the *search phrases a
   user would actually type* to find a chunk, in the **querier's** vocabulary
   ("how do I log in", "credentials"), not the author's ("tokens"). Closes the gap
   from the document side (docTTTTTquery). Free incremental work on the
   distill/capture pass.
3. **Alias vectors, not concatenation.** Stuffing synthetic queries into one
   embedded blob dilutes it. Store N vectors per chunk (body + one per alias),
   all pointing at one chunk id; max-over-vectors at retrieval, dedupe by id at
   fusion. More precise, costs index complexity — weigh vs. the simpler concat.
4. **Keyword/entity sidecar for BM25.** Sparse retrieval is exact-term; feed it the
   extracted symbols, file paths, command names, and synonym sets per chunk. This
   is where "restart server" → `:MCPRestartServer` wins. Cheap, high precision,
   complements dense.

**Related-topic map.** A chunk↔topic graph (edges from shared entities, embedding
proximity, or LLM-asserted "see also"). Threefold value: retrieval **expansion**
(after a hit, pull graph neighbors — catches the vocabulary-gap note sitting one
hop from a note that *did* match), **navigation** (surface "related:" so the agent
can pivot), and it makes [knowledge-capture §5b](knowledge-capture.md)'s
merge-aware write a graph lookup. A real build — stage it *after* per-chunk
enrichment proves out.

**Topic index into code** — two directions; the reverse one is the more powerful:

- *Notes→code*: notes carry resolvable **symbol** pointers (not line numbers —
  those rot), generated via LSP/ctags during digestion. Answers "where is this
  implemented."
- *Code→notes* (the one that fights the grep habit): an index from symbol/file →
  the notes that discuss it. When the agent is reading `crib/retrieve.py`, *meet it
  there* — surface "there's a design note on this" instead of waiting for it to
  think to ask. Turns "about to grep" into "already pointed at the note."

### 3.1 Keyword sidecar — two tiers, and the git-communicable map

The keyword sidecar splits into two tiers by cost — and therefore by storage:

- **Tier 1 — mechanical (shipped 2026-06-30).** Compound-identifier splitting so a
  *spaced* query matches a *solid* identifier the tokenizer keeps whole: "index
  file" → `index_file` (the `_` is a word char), "restart server" →
  `:MCPRestartServer`, "lexical cache" → `LexicalCache`. Computed **on-the-fly** in
  the BM25 corpus build (`_subtokens` / `_lexical_tokens`, `crib/retrieve.py`) — **no
  storage, always current**, BM25-only (kept out of the dense embedding to avoid
  identifier-soup). Proven deterministically: a spaced query plain BM25 scores 0 now
  matches. Targets *exact-term* recall — it does **not** move the semantic-paraphrase
  stragglers (§5.3); those need tier 2.
- **Tier 2 — LLM-distilled (designed; deferred behind the bridge).** Semantic
  keywords / synonyms / "what this answers" — an LLM call per section, too costly to
  recompute per rebuild, so it needs a **persisted** map. Per crib's cross-machine
  ethos that map must be a **git-communicable asset**, not an ephemeral cache.

**The tier-2 map: content-addressed text, in the tracked data tree.** Key each entry
by the chunk's `content_hash` (already computed) → `keywords/<content_hash>.toml`
under the project's **git-tracked** data dir, modeled on the version ring
(`.versions/`, DESIGN §8): one immutable, additive file per hash, **merge-conflict-
free** (same content → same filename → byte-identical across machines). It rides the
existing git sync (DESIGN §14), so the expensive LLM output **travels with the notes**
— generated once on one machine, pulled everywhere, never re-run for content seen.

**Plain text, never a binary store (no SQLite).** Git-communicable means *git can do
its job*: line-level diff, three-way merge, and a legible commit/PR where one
machine's generated keywords are reviewable. A binary index (SQLite/LMDB) is opaque to
diff/merge and churns as a blob — it would be a *cache*, not a shared asset. So each
entry is small **TOML** (matching crib's own config format) — line-oriented with one
keyword per array line, comments allowed, stable key order — one file per
`content_hash`, written deterministically so re-serialization never yields a spurious
diff. Shape:

```toml
# keywords/<content_hash>.toml
content_hash = "…"          # = filename; self-describing
relpath  = "DESIGN.md"      # provenance hint (not authoritative — content_hash is)
heading  = "10.3 Retrieval — hybrid dense ⊕ BM25"
kw_scheme = 1               # bump to force regeneration
keywords = [
  "reciprocal rank fusion",
  "hybrid dense sparse retrieval",
  "exact term vs semantic match",
]
```

Liveness falls out of content-addressing: a section edit changes `content_hash` →
cache miss → regenerate *that section only*; a prompt/extractor change bumps a
`kw_scheme` field → a deliberate, on-demand global refresh (never automatic — it's
expensive). Generation runs **off the write path** (an explicit `crib keywords` pass,
or folded into `distill`, sharing the bridge), so a save never blocks on an LLM; BM25
consumes whatever's cached and a miss degrades gracefully to tier-1 + body.

**Record vs. serving — the text is the record; the indexes load from it.** The text
files are the durable, shared store of record; the *serving* layers are derived and
rebuildable, so loading/attaching them at serve time is fine (Chroma already attaches
to serve — same idea). At `LexicalCache` build, look up each chunk's
`keywords/<content_hash>.toml` (the `content_hash` is in chunk metadata) and append
its terms to the BM25 token list — **exactly how heading and tier-1 subtokens already
feed BM25** (`_lexical_tokens`). The same text can also be fed into Chroma (as added
document text, for the dense side) if it earns lift. What we avoid is the *inverse*:
making a binary index the **store of record**. Chroma is gitignored and rebuilt from
notes + this text asset, so it must never be the only home for the expensive LLM
output — but Chroma (or BM25) *serving* it, loaded from the text, is the intended
shape. Derived-but-**expensive** data → tracked text asset, fed into the indexes;
derived-and-**cheap** data (tier 1) → recomputed into the index, stored nowhere.

## 4. Part B — Invocation (the actual gate) — make crib *push*, not only pull

Keep the pull tools for deep dives, but borrow `MEMORY.md`'s trick — inject
automatically. In rough order of leverage:

1. **`UserPromptSubmit` hook = automatic RAG.** On each turn, lookup the prompt and
   inject top-k as a context block ("crib may have: …"). The tool fires without the
   agent deciding. Gate on a relevance threshold (inject only if top score > τ) so
   it is signal, not noise. Likely the single highest-leverage item here.
2. **`SessionStart` hook = inject the project digest** (the topic-index / map from
   §3). Literally generalizing the `MEMORY.md` mechanism — the reason harness
   memory wins. Curated knowledge in context *before* any tool decision.
3. **`PreToolUse(Grep|Glob)` hook.** Intercept the moment the agent is about to
   grep, run the same query against crib, inject "before grepping, crib has …".
   Advisory, not blocking. Triggers on the exact competing action.
4. **Sharpen the tool description + the global directive** with concrete triggers
   (the `claude-api` skill's explicit "consult when X / skip when Y" lists beat
   vague "consult first"). Promise `lookup` is cheap and safe to call
   speculatively.
5. **Fix the reliability asymmetry.** Always-on daemon, graceful degraded mode,
   health surfaced. Reliability *is* an adoption feature — a tool that is sometimes
   down trains permanent fallback.

> **Observed live (2026-06-30), three distinct failure modes in one session**, each
> a textbook trainer of the grep habit: (a) the combiner did not surface crib's
> tools to the agent until a refresh was forced — *reachable but undiscoverable*;
> (b) the tools were under an unexpected namespace prefix, so keyword search missed
> them; (c) the combiner dropped all proxied tools **mid-call**. The crib server
> itself stayed healthy on `:7732` throughout. The save each time was the **`crib`
> CLI**, which talks straight to the daemon and bypasses the combiner — which is why
> the eval harness (§5) drives the CLI, not the MCP path: the measurement substrate
> must not share the adoption layer's fragility.

## 5. The eval harness — two metrics, because push amplifies both signal and noise

"Solid understanding of search-term efficacy" means measurement, not vibes — and
auto-injection (§4) cannot ship blind, since it amplifies bad retrieval as readily
as good. Grow the n=8 set from §10.4 into:

- **Findability metric** — query→expected-note, MRR / recall@k, toggled per
  enrichment strategy (topic-phrase on/off, doc2query on/off, multi-vector) — so a
  win is *proven* (did doc2query actually close credentials≠tokens?) not hoped.
- **Invocation metric** — from logs: rate of lookups that returned nothing
  (enrichment gap) vs. returned-good-but-the-agent-grepped-anyway
  (description/trust gap). That log *is* the dataset for tuning τ and the doc2query
  prompts.

Two metrics, two problems. Every strategy below lands behind a quality bar measured
on this harness before it is trusted.

### 5.1 First proof — cold-start seed (2026-06-30)

The harness exists (`scripts/eval_retrieval.py`, cases in
`scripts/eval_retrieval.cases.json`, driven via `crib --json lookup`). Its founding
data point is the cold-start fix itself. Before this repo had a crib project, the
design docs were **not indexed**, so crib could not answer questions about its own
design — and grepping `DESIGN.md` was the *correct* behavior. After `.crib` →
`cribsheet` project + `import` of `DESIGN.md` and `docs/*.md`, the identical queries
invert:

| query | before (`default`, unindexed) | after (`cribsheet`, seeded) |
|---|---|---|
| "…consult the memory tool instead of grep" | rank-1 = generic project blurb (0.66); 0/3 on-topic | rank-1 = `retrieval-and-adoption.md §3` (0.79); 8/8 on-topic |
| "vocabulary gap credentials tokens reranking" | rank-1 wrong (0.58) | rank-1 = the exact §3 section (**0.83**), then DESIGN §10.4, §10.3 |

The lesson is the thesis (§1): **adoption cannot precede content + findability.** The
seed is build-order step 0 — without it every downstream metric measures an empty
store. (Displayed cosines run non-monotonically down the ranked list — the RRF
fusion fingerprint of §10.3, not a harness bug.)

### 5.2 Enrichment lift — heading-breadcrumb injection (2026-06-30)

First doc-side enrichment (§3, "shipped first"), measured on the 9-case set. Clean
A/B: `rerank=False, hybrid=True`, so the warm daemon and `--no-daemon` rank
identically — the only variable is the enrichment.

| | MRR | recall@3 | rank-2 cases |
|---|---|---|---|
| baseline (body only) | 0.889 | 1.000 | "Capture source" (§5c), "version ring" (§8) |
| + heading breadcrumb | 0.926 | 1.000 | — both lifted to rank-1 |

The two cases that improved are exactly the ones whose *subject* lives in the heading,
not the prose — the predicted win. Recall was already saturated, so MRR (rank
quality) is the metric that moved.

**Finding — labels and near-ties.** One case *appeared* to regress (rank-1 → rank-3)
until inspected: enrichment had promoted `eval-organic-memory.md` (headings: "how
well does the LLM save notes unprompted", "isolate cribsheet from the other memory
system") — a **legitimately relevant** doc the narrow label hadn't anticipated, in a
near-tie cluster (top-5 within 0.025). Two harness changes followed: `expect` may now
be a **list** (genuinely multi-answer queries), and that query was sharpened to target
§4 unambiguously. Post-fix: **MRR = 1.000, recall@3 = 1.000 (n=9)**. The near-tie
itself is the standing signal that the LLM topic-phrase (§3.1) and rerank are what
separate a bunched cluster — the next lift to measure here.

### 5.3 Multi-phrasing coverage — the honest benchmark (2026-06-30)

One phrasing per need overfits to the note's own wording and measures nothing about
**embed/paraphrase generality**. The cases were restructured into 9 information-needs
× 3 phrasings (the 2nd/3rd deliberately vocabulary-shifted), and the harness now
reports **per-need robustness** (phrasings-hit / total, worst rank), not just an
average. This is the canonical set; single-phrasing was saturated noise.

| set | MRR | recall@3 | needs all-rank-1 |
|---|---|---|---|
| single phrasing (n=9) | 1.000 | 1.000 | 9/9 (saturated) |
| **3 phrasings (n=27)** | **0.809** | **0.963** | **3/9** |

The drop is the point — the oblique phrasings expose where bge-small's generality
runs out (the vocabulary gap, live). Weak spots, i.e. the next enrichment's targets:
- **1 miss** — distill / *"clean up and condense … with an LLM pass"*: right file,
  **wrong section** (a vocabulary-shifted phrasing pulls a sibling section).
- **rank-3 stragglers** — invocation *"stop the assistant reaching for grep"*,
  hybrid-fusion *"combine semantic search with exact term matching"*, quarantine
  *"low-trust staging area"*, version-ring *"keep recent revisions"*.
- **robust across all 3:** only vocab-gap, rerank-fuse, capture-source.

Bars track this baseline (MRR ≥ 0.75, recall@3 ≥ 0.90) — floors with margin, to be
tightened as the keyword sidecar / LLM topic-phrase (§3) lift the stragglers.

**Timing (Raspberry Pi).** The lookup path has **no LLM** (rerank off); cost is
embedding. Cold `--no-daemon` reloads the embedder *per call* — minutes for 27
queries. The warm daemon amortizes the model load (→ 2:40 for 27), and the residual
is **27× CLI process startup + connect**, not embedding. A batch/in-process harness
(one connection, all queries) is the fix if the set grows.

### 5.4 Tier-1 keyword sidecar lift (2026-06-30)

Compound-identifier splitting (§3.1), plus 2 identifier-style needs added for
coverage (n=31, 11 needs):

| | MRR | recall@3 | needs all-rank-1 |
|---|---|---|---|
| heading enrichment only | 0.809 | 0.963 | 3/9 |
| + tier-1 keyword sidecar | **0.839** | 0.968 | **5/11** |

Predicted profile, confirmed: an **exact-term** lift with **no regression** on the
semantic set. `version-ring` became fully robust (its "keep recent revisions"
phrasing rose rank-3 → rank-1 as the section out-scored competitors), and the new
`id-index-file` / `id-lexical-cache` needs are found. The semantic-paraphrase
stragglers (invocation p3, hybrid-fusion, quarantine "low-trust staging area", the
distill miss) are **unchanged** — correctly, since BM25 can't bridge a synonym gap.
Those are tier-2's job (§3.1).

### 5.5 Volume-corpus lift — LLM elaboration + summary_index (2026-07-01)

The unsaturated companion to §5.1–5.4: a **12-need / 36-phrasing** set spanning five
imported repos (dotfiler, mcp-companion, svg-mcp, zdot, sharedserver;
`scripts/eval_retrieval.volume.cases.json`), each need one direct + two
**vocabulary-shifted** phrasings — the query≠note gap the enrichments target. Indexes
were **GLM-authored** (`opencode-glm`; the qwen zen endpoint was offline that day — a
provenance variable, since alias/keyword quality tracks the generating model).
Measured **in-process** (`Crib.lookup`, one warm embedder) against a *true* no-LLM-index
baseline.

| config | MRR | recall@3 | needs all-rank-1 |
|---|---|---|---|
| baseline (none) | 0.841 | 0.917 | 5/12 |
| kw@0.1 | 0.856 | 0.917 | 6/12 |
| kw@0.3 *(shipped default)* | 0.848 | **0.889** | 5/12 |
| sum@0.15 | 0.866 | 0.917 | 7/12 |
| sum@0.2 | 0.861 | 0.917 | 7/12 |
| **sum@0.2 + kw@0.1** | **0.875** | 0.917 | **7/12** |

- **summary_index is net-positive here — the §5 "needs a larger/diverse corpus"
  hypothesis is confirmed.** +0.024 MRR at w=0.15 (recall already 0.917, held),
  promoting stragglers (`dotfiler-deployment 2→1`, `svg-defs 3→1`,
  `requires-optional 3→2`). This **overturns the cribsheet-only net-negative**: where
  dense recall isn't saturated the aliases *rescue* rather than *displace*. Sweet spot
  **w=0.15–0.2**; w≥0.3 starts costing recall.
- **keyword_weight is corpus-dependent.** The shipped `keywords@0.3` (measured best on
  cribsheet, §5.4) **hurts here** — recall −0.028 (demotes `svg-defs` out of top-3) for
  a trivial MRR gain, because the per-section GLM terms are too generic. Dropping to
  **w=0.1** flips it net-positive (+0.015 MRR, no recall loss). The 0.3 default should
  **not** be assumed for imported/diverse projects.
- **They stack.** `sum@0.2 + kw@0.1` → MRR 0.841→**0.875 (+0.034)**, recall held,
  all-rank-1 5→7/12 — best config, beating either alone.
- **Why keywords come out generic → the next lever.** Sections are authored **blind to
  their siblings** (`_generate_index`, one LLM call per section), so the model can't
  choose *distinctive* terms. **Whole-doc-context generation** — author a doc's sections
  together, validate heading→`section_hash` coverage, mop-up the misses (content-
  addressing makes the mop-up idempotent + convergent, so strict model conformance is an
  *efficiency* not a *correctness* property) — is the fix, re-measurable on this set.

**Measurement integrity — two bugs fixed to get valid numbers, both masking the lift:**
`DaemonClient._data` returned empty hits (FastMCP typed `.data` reconstructs a
list-of-objects return as empty models → `[{}]`/`Root()`; read `structured_content`
instead) — the eval/gate drive the daemon by default, so every hit scored as a miss;
`_split_labels("")` couldn't *disable* a default-on index (returned None → config
default), so `--lift keywords`'s baseline silently ran **with** keywords on → a Δ0 false
null. Regression tests: `tests/test_client.py`, `tests/test_cli_labels.py`.

## 6. Recommended build order (each gated by a proof on §5)

1. **Doc-side enrichment** — ✅ heading-breadcrumb injection (§5.2, MRR 0.889→0.926),
   ✅ tier-1 keyword sidecar (§3.1, compound-identifier splitting), and ✅ tier-2 LLM
   **elaborations** (the generation bridge + `crib elaborate <label>` + content-
   addressed TOML store + BM25 consumption, §3.1) are built. The generation bridge
   (`crib/generate.py` over llmkit, providers/profiles config à la `models.toml`)
   also powers `distill`. Remaining: run the LLM elaboration pass and **measure the
   lift** on the eval harness (`eval_retrieval.py --lift keywords`) — the semantic
   stragglers (§5.3) are the target; and (fast-follow) the `chat`→TextIO sink in
   llmkit to drop the temp-file capture.
2. **`SessionStart` digest injection + `UserPromptSubmit` auto-RAG** — the
   invocation unlock, the part that is actually missing.
3. **Eval harness (both metrics)** — in parallel; without it 1 and 2 are unproven.
4. **doc2query / alias vectors** — once the eval can score them.
5. **Topic graph + code↔note index** — the bigger build, after the basics pay off.

Capture ([knowledge-capture.md](knowledge-capture.md)) sits behind all of this: it
only adds value once 1–3 make the store findable and consulted.

## 7. Genuinely open architectural questions (worth scrutiny before committing)

- **Auto-RAG injection budget & threshold (§4.1).** How much context per turn, what
  τ, how to dedupe against what is already in context, and how to avoid a feedback
  loop where injected text gets re-captured. Lean: strict τ, small top-k, mark
  injected blocks so capture ignores them.
- **Topic graph + code↔note index (§3).** Storage model (edges in Chroma metadata
  vs. a sidecar), how edges are derived and kept fresh under the single write path,
  and whether code→note is a tool (`lookup_code`) or another injection hook.
