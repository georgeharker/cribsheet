# Code symbol index — concept-search ⊕ call graph (the code↔note build)

Status: design — **deliberately parked behind adoption.** The concrete build of the
**code↔note index** sketched in [retrieval-and-adoption.md §3](retrieval-and-adoption.md)
("Topic index into code — two directions; the reverse one is the more powerful"). Two
composed subsystems over a shared *symbol identity*: a **structural** facet (a warm LSP
session → call graph) and a **semantic** facet (LLM "what does this do" → embedded,
concept-searched).

> **Sequencing caveat.** This is a capability, not the gate. Per
> [retrieval-and-adoption.md §1/§4](retrieval-and-adoption.md) the binding constraint is
> *invocation* (does the agent consult crib) and *capture* (does it save) — not more
> findability. A better index the agent never queries is an index nobody reads. So this
> is specced now to bank the thinking, but it is **downstream of the ergonomics work**
> (measure save/consult rates → `UserPromptSubmit` auto-RAG → capture hook). Build it
> only once the store is actually consulted — at which point it becomes the strongest
> *reason* to consult (the grep-can't-do value boundary), reinforcing adoption rather
> than front-running it.
Realizes/extends DESIGN [§10.1 store](../DESIGN.md) and [§10.3 retrieval]; depends on
the generation layer ([knowledge-capture.md](knowledge-capture.md)) and reuses the
whole-doc **bulk generation** + content-addressing built for `keyword_index`.

> **Thesis.** grep finds a *name*; nothing finds a function by *concept* or answers
> "what calls this." A symbol index gives crib a job the source tree structurally
> cannot do — which is exactly the value boundary that makes an agent consult crib
> instead of grep ([retrieval-and-adoption.md §4](retrieval-and-adoption.md), the
> invocation gate). The semantic half is also where LLM generation *finally pays off*
> for retrieval — see §1.

## 1. Why this works where `summary_index` didn't

The [§5.5 verdict](retrieval-and-adoption.md) shelved note-summaries: on prose, the
body already embeds well and recall is saturated, so an LLM summary is a redundant
alias with nothing to add. **Code inverts every term of that:**

- **The raw body embeds terribly for concept.** An NL embedder does not map
  `_refresh_oauth` / `token.expires_at < now()` to "handle auth expiry." So the LLM
  description is not a *competing* alias — it is the **primary searchable surface**,
  because no good body vector for code-by-concept exists.
- **Recall is genuinely unsaturated.** This is the regime the doc predicted the dense
  / doc2query techniques would earn their keep in and never got to test on notes.
  Here `doc2query` ("what questions does this function answer") adds real recall
  instead of crowding — **retest it (§6); the note-verdict does not transfer.**

> The distinction to hold: summaries of prose are redundant; descriptions of code are
> **net-new information**. Same machinery, opposite payoff, because the baseline
> differs.

## 2. Two facets, one identity

The unit is a **symbol** — functions/methods/classes **and data declarations**:
module-level globals/constants and class members (class-body attributes *and* the
`self.x` instance attributes pyright hoists to class scope). Callables get calls/
called_by (call hierarchy) + references; data gets references only (it isn't called).
Indexing is **scope-aware**: a Variable/Constant is kept only at module or class scope —
one nested under a function is a *local* and dropped, because documentSymbol reports
locals too (pyright lists 52 in one file, ~all locals) and indexing them would be pure
noise. The guard is "no ancestor is a function/method" (`_walk` tracks container kinds).
Structural and semantic facets share one identity so the two subsystems compose rather
than diverge:

```
symbol → {
  # identity
  symbol_hash   # content hash of the symbol BODY (§2.1) — the cache/gate key
  fqname        # module.Class.method — stable, human-legible
  file, line    # current location, resolved on read (rots — see §2.1)
  kind          # function | method | class

  # structural facet — LSP, served LIVE (§3)
  signature, calls[], called_by[]     # call hierarchy (precise; not all servers)
  references[]                         # textDocument/references (broader — reads +
                                       # mentions, not just calls); FIRST-CLASS, kept
                                       # separate from called_by — call-vs-ref is the
                                       # consumer's/LLM's call to make

  # semantic facet — LLM, cached + embedded (§4)
  description, doc2query[], embedding_ref
}
```

### 2.1 Hashing & the location-rot rule

- **`symbol_hash` = hash of the normalized symbol body** (dedent, strip comments/
  docstring optional), *not* file offsets. This mirrors `section_hash` for notes
  (window-invariant, survives reformatting around it) — so a description regenerates
  **iff the symbol's own code changed**, and a file edit above it doesn't churn every
  symbol below.
- **Never store line numbers as identity** ([retrieval-and-adoption.md §3](retrieval-and-adoption.md):
  "not line numbers — those rot"). `fqname` is the durable pointer; `file:line` is
  resolved against current disk at read time (like a note section's `line_start/end`),
  and re-derived from the LSP `documentSymbol` ranges on query.

## 3. Structural facet — the warm LSP session subsystem

An LSP server is architecturally **Chroma's twin**: a warm, stateful, expensive-to-
cold-start semantic index that must live as long as the daemon. Everything crib built
to keep Chroma + the embedder warm maps directly.

| crib (text) | this (code) |
|---|---|
| Chroma warm index | language server, warm per (root, lang) |
| daemon owns it; CLI is a client (DESIGN §10.2) | daemon owns the LSP sessions; CLI queries through it |
| `Watcher` → reindex on edit (`crib/watch.py`) | `Watcher` → `didChangeWatchedFiles` to refresh the server |
| content-hash gate (idempotent) | `symbol_hash` gate for descriptions |
| sharedserver refcount + grace | session registry with grace + eviction |

**Divergence: stdio, not a port.** An LSP server talks over pipes to *one* parent, so
it cannot be sharedserver-shared across processes the way `crib-chroma` (HTTP) is.
The sessions therefore live **in-process in the daemon** — which is consistent: the
daemon is already the long-lived owner, and the CLI already routes through it.

### 3.1 Session registry & lifecycle

- A registry keyed by **(workspace_root, language)** → one server process, initialized
  once (init pays the full project-index cost — seconds on pyright, minutes on
  rust-analyzer for a big repo; *this* is why warm is mandatory).
- **Lazy start** on first query for a (root, lang); **grace-evict** idle sessions
  (mirrors Chroma's grace) — the daemon may otherwise juggle basedpyright +
  rust-analyzer + gopls + clangd at once, real memory weight.
- **Crash/restart supervision.** Servers hang or OOM; a restart re-pays init. Queries
  during (re)index must wait/degrade gracefully — crib already has "momentarily stale
  index" semantics for exactly this.

### 3.2 Watched-files sync — the crux, done right

Not "re-dump on change." It's a negotiated, incremental protocol, and the part people
skip and then wonder why results are stale:

1. Let the server **index from disk** off `root_dir` at init — do **not** `didOpen`
   the whole tree (holds every doc in client memory).
2. The server sends `client/registerCapability` for `workspace/didChangeWatchedFiles`
   **with glob patterns** — it tells *you* what to watch. A correct client **honors
   that registration**.
3. crib's `Watcher`, **rooted at the code workspace** (a second watch scope, distinct
   from the notes tree) and filtered to those globs, pumps `didChangeWatchedFiles` on
   create/change/delete → the server re-reads just those files and invalidates its
   index. The same fs event also invalidates the affected `symbol_hash` entries (§4).
4. `didOpen` / `didChange` **only** for a file being actively queried at cursor
   precision; `didClose` after. Everything else stays disk-synced via step 3.

### 3.3 Server specs — the `.lsp.json` schema, from `~/.config/crib`

Launch/settings specs are the tedious long tail. Rather than invent a format or vendor
solidlsp's, crib **mirrors Claude Code's `.lsp.json` schema** — a map of label →
`{command, args, extensionToLanguage, transport?, env?, initializationOptions?,
settings?}` (confirmed against the harness's zod schema and George's `georgeharker/pylsp`
plugin). Selection is by **file extension** via `extensionToLanguage`; its value is the
`languageId` sent in `didOpen`. Because it's the *same* schema, existing `.lsp.json` /
plugin `lspServers` files drop in unchanged.

**Loading (`crib/codeindex.py`):** `~/.config/crib/lsp.json` (user, iterated first so it
wins selection) ⊕ **shipped documented defaults** (`DEFAULT_LSP_SPECS`;
`docs/lsp.json.example` is the copyable set). `command` is resolved **per-machine** —
`${ENV}` expansion (incl. `${CLAUDE_PLUGIN_ROOT}`) then `which` — so the spec is
**portable**: the machine-specific binary path is never baked in, and a server whose
binary is absent is simply skipped (that language degrades, not an error). This is the
"mirror out for others" property: the shared spec carries the *config knowledge*; each
host resolves its own binaries.

For the **call graph** a server must implement `textDocument/callHierarchy` —
basedpyright, pyright, rust-analyzer, gopls, clangd do; **pylsp+jedi does NOT** (callers
only). So the defaults ship `basedpyright` first with `pyright` as the `.py` fallback,
and `server_for()` picks the first spec claiming the extension whose command resolves.
crib isn't bound by the harness's one-server-per-extension limit, so it can select
basedpyright for xref while the editor keeps pylsp.

**nvim is an optional *export* source, not a live dependency.** A `dump_lsp.lua` over
`vim.lsp.config[name]` can normalize George's tuned nvim specs *into* this `.lsp.json`
schema (path → `which`-name) and write them to the shared asset — regenerated on config
change, consumed with no editor live. (The headless dump needs the lazy-plugin env
loaded; that incantation is the remaining nvim-side tidy-up.)

**Two things the client owns** (the schema can't supply them):

1. **Advertise your own capabilities** — `textDocument.callHierarchy`, `references`,
   `documentSymbol`, `definition`. A server only exposes `prepareCallHierarchy` /
   `outgoingCalls` if you advertise support. **This is what unlocks callees.**
2. **Answer the `settings` pull** — servers read `settings` via the
   `workspace/configuration` *pull* (not `initialize`); the client holds the spec's
   `settings` and returns the requested dotted `section` (`LspClient._section`).
   `initializationOptions` go in `initialize`.

### 3.4 Call-graph queries

- **callers** → `textDocument/references` (or `callHierarchy/incomingCalls`). Well
  supported everywhere.
- **callees** → `callHierarchy/prepareCallHierarchy` + `outgoingCalls`. Support is
  **uneven**: basedpyright, rust-analyzer, gopls, clangd yes; many others no.
- **references** → `textDocument/references`, resolved back to the *enclosing* symbol
  (`_enclosing_symbol`: innermost documentSymbol range containing the ref line). A
  **first-class relation** populated whenever the server has `referencesProvider` —
  which is nearly everywhere, incl. symbols-only servers like **shuck** (zsh). It is
  deliberately NOT folded into `called_by`: a reference is *broader* than a call (it
  includes reads, assignments, mentions), and collapsing the two would launder a
  guess as a fact. Both are surfaced separately (`←` callers, `⇐` references) and the
  call-vs-reference distinction is left to the consumer/LLM. Capability is read from
  the `initialize` result (`callHierarchyProvider` / `referencesProvider`) so a server
  that lacks a facet contributes empty edges for it rather than hanging.

**Language routing.** Selection is by extension (§3.3); for **extension-less scripts**
the `#!` shebang is read and mapped to a language (`_shebang_lang`: `env` and version
suffixes handled — `#!/usr/bin/env zsh`→zsh, `#!/usr/bin/python3`→python), then the
first installed spec serving that language is used. Extension always wins over shebang;
a file with neither a known extension nor a recognized shebang is silently skipped.

## 4. Semantic facet — the description index

### 4.1 Generation — structure feeds semantics

- **Bulk-per-file** authoring (the whole-doc bulk path already built in
  `_generate_index`): one structured call per file emits `{fqname, description,
  doc2query[]}` for every symbol, with a per-symbol mop-up backstop. Whole-file
  context lets the model see siblings and call relationships.
- **Feed the structural facet into the prompt.** Put each symbol's `calls[]` /
  `called_by[]` in context so the model writes accurate intent ("orchestrates the
  update by calling `_detect_topology` then `_pull`") rather than guessing from the
  body. This is why doing both halves together beats either alone.
- **Content-addressed by `symbol_hash`** under the git-tracked data tree
  (`<project>/symbol_index/<label>/<symbol_hash>.toml`, exactly the `keyword_index`
  shape) — a watcher edit regenerates *only* the changed symbols; the map travels with
  the notes via git sync, generated once per machine.

### 4.2 doc2query — retest here

Generate the *queries a developer would type* to find the symbol ("where do we
validate the payload", "what retries on 429"), embedded alongside the description.
On notes this washed (saturated recall); **on symbols it should lift** (§1). It is a
first-class A/B on the eval harness (§6), not an assumption.

### 4.3 Embedding — embed the description, not the code

The searchable vector is the **LLM's NL prose**, so **reuse the existing NL embedder**
(`EmbedConfig`, bge) — no code-specific model (CodeBERT etc.) needed. This also means
the per-machine embedder-profile mechanism and the dim-switch reindex apply unchanged.

## 5. Storage & retrieval

- **A parallel corpus.** Symbols are a distinct entity from note sections. Store them
  as a `crib_symbols` collection (or a `kind:symbol` partition of the existing store),
  metadata carrying `fqname`, `file`, `kind`, `symbol_hash`, and the structural refs.
- **Retrieval shape:** concept query → cosine over symbol descriptions/doc2query →
  ranked symbols → return `signature` + `file:line` + `callers/callees`. **Concept
  search finds the entry point; the call graph navigates from it** — "where is auth
  expiry handled?" → the symbol; "what breaks if I change it?" → `called_by`.
- **Surface, two ways:** a unified `lookup` that returns notes *and* symbols (with a
  `kind`), or a dedicated `lookup_code` verb/tool. Also the **reverse** direction from
  [retrieval-and-adoption.md §3](retrieval-and-adoption.md): when the agent opens
  `retrieve.py`, surface "here's what these symbols do + the design note that discusses
  them" — meet it where it greps.

## 6. Eval — same discipline, new corpus

- **Concept→symbol cases** (`scripts/eval_retrieval.*` extended, or a sibling set):
  developer-phrased query → expected `fqname`, MRR / recall@k, multi-phrasing for
  paraphrase generality — exactly the honest benchmark of
  [retrieval-and-adoption.md §5.3](retrieval-and-adoption.md).
- **Re-run the doc2query / alias A/B here.** The hypothesis (§1) is that it lands
  positive where it washed on notes; prove or kill it on this corpus.
- **Structural correctness** is separately checkable and deterministic: assert known
  `callers`/`callees` for a handful of symbols against ground truth (no LLM in the
  loop) — a regression gate on the LSP sync.

## 7. Build order (each gated by a proof)

1. **Config bridge + thin read-only `lsprotocol` client** — `dump_lsp.lua`, launch
   basedpyright from it, one-shot `documentSymbol` + `references` + `callHierarchy` for
   a symbol in *this* repo. Derisks the fiddly capability/settings dance before any
   warmth.
2. **Session manager** (§3.1–3.2) — one warm session, watched-files refresh, grace
   evict. Prove a mid-session edit refreshes callers without re-init.
3. **Symbol identity + structural store** — `symbol_hash`, `fqname`, the parallel
   corpus; structural-correctness eval (§6) green.
4. **Semantic layer** — bulk-per-file description + doc2query (structure-fed),
   content-addressed; embed; concept→symbol eval + the doc2query A/B.
5. **Surface** — `lookup_code` / unified `kind`, and the code→note reverse index.

Each of 3–5 lands behind a harness proof, same as the retrieval work.

## 8. Learnings — durable human understanding attached to a symbol

The LLM `description` is a **regenerable cache** — it's recomputed whenever the
symbol body changes (`content_hash` gate, §2.1). A hard-won *"oh, that's why"* — a
subtlety you finally understood, a gotcha, a "I misread this as X for a while" — is
the opposite: irreplaceable, human, source-of-truth. Putting it in the symbol file
would marry the durable thing to the disposable one (regen churn, mixed provenance,
sync riding on a cache). So it lives elsewhere.

**A learning is a first-class cribsheet note**, keyed to a symbol's `fqname`, under a
dedicated `<project>/code-learnings/` subtree. The symbol index gains a *join*, never
a storage responsibility — and the learning inherits the entire note machinery for
free: the version ring, git sync, the frontmatter merge driver, semantic search
(`lookup`/`apropos` find it as a note *and* it surfaces via the symbol). Same move as
`import-memory`: one source of truth, two searchable surfaces.

```
code-learnings/crib.retrieve.LexicalCache.get.md
---
kind: code-learning
symbol: crib.retrieve.LexicalCache.get   # fqn — the foreign key (authoritative)
lang, file, signature                    # snapshot at authoring (for orphan legibility)
content_hash: 1f89…                      # body hash when written → staleness signal
---
### 2026-07-03
The BM25 cache is keyed by project+corpus-hash; I misread it as global for a while.
```

- **Filename = the fqn, munged only as the filesystem forces.** Whitelist
  `[A-Za-z0-9._-]`; everything else (`::` `/` `<>` `*` `&` spaces `~` operators)
  collapses to `-`, and a *lossy* munge appends a short fqn hash so distinct symbols
  can't collide (`core::cache::Store::get` → `core-cache-Store-get-132ab1a5`). Clean
  dotted fqns pass through verbatim. The `symbol:` frontmatter is authoritative, so
  the filename never has to round-trip. (`crib/codeindex.py: learning_slug`.)
- **Same primitives as notes, under the `learning` noun** — `learning_add` (attach a dated entry,
  creating the running note on first use), `learning_edit` (rewrite the body), `learning_forget`
  (remove, recoverable via the ring — works on orphans too), `learning_read`, plus
  `learning_reaffirm` (clear a ⚠ stale flag without a rewrite) and the maintenance pair
  `learning_report` (health report) / `learning_rehome` (re-point an orphan). Each resolves
  the symbol against the index (exact fqn wins; a bare name only if unique — never
  silently pick, so a learning can't land on the wrong symbol) and reuse the note write/delete path (`NoteStore`). MCP (`learning_add`, …)
  + CLI (`crib learning add <symbol> "…"`).
- **Attach to code you can't edit.** A learning is external, so it pins understanding to
  vendored deps and read-only explorations — where a comment structurally can't go. The
  comment-vs-learning line: a comment is for the next reader and ships in the repo; a
  learning is cross-session memory for the explorer (the meta stuff that doesn't earn a
  comment but shouldn't be re-derived).

**Identity drift.** `content_hash` already immunizes against body churn — the same
`fqname` still points true after a body edit. The failure mode is *rename/move*, which
orphans the fqn key. The rule: **never auto-attach a durable learning to a symbol it
wasn't authored against** — a wrong attachment is worse than a dangling one. So orphans
are *surfaced, not solved* (report-only, never gates indexing). The report (§ step 4)
distinguishes two cases: a **true orphan** (the `symbol:` fqn no longer resolves at all)
and a **moved** learning (the fqn still resolves but its snapshot `file:` no longer
matches the symbol's current file — a same-name relocation worth flagging, cheaply
auto-updatable). Re-homing (§ step 5) is a confirmed action, suggestion-assisted by
signals we already have — strongest is **git history** (`git log --follow` / rename
detection across the code repo) plus **call-graph neighborhood** (the callers/callees
set survives a rename) and signature match; usage pointers become the evidence a human
(or the LLM, on request) weighs. LLM-assisted, manually invoked or agent-proposed —
never silent. The authoring-time `signature` snapshot keeps an orphan legible even if
the symbol is gone.

**Staleness for free.** The learning snapshots `content_hash` at authoring; when
surfaced (via `code_lookup`/`code_xref`), if the symbol's current `content_hash` differs
it's marked `⚠ written against an older body` — not auto-invalidated (the subtlety often
still holds), just honestly flagged. When you've re-checked a flagged note and it still
holds, **`learning_reaffirm` clears the flag without a rewrite** — it re-snapshots
`content_hash`/`file`/`signature` and stamps `reaffirmed`, so the body stays untouched.

Build order:
1. `learning_add`/`edit`/`forget`/`read` + the `code-learnings/` subtree ✓
2. Query-time join — 📌 block in `code_lookup`/`code_xref` + staleness ⚠ ✓ (keyed
   O(1) by `learning_slug(fqn)`; only symbols that carry a note pay a read)
3. `code_graph` glyph (📌) marking nodes that carry a learning ✓ — the call tree
   becomes a treasure map. fqn→slug membership against the subtree (never filename→fqn,
   since the munge is lossy)
4. `learning_report` report ✓ — true orphans (fqn unresolved) *and* moved learnings (fqn
   resolves, snapshot `file:` drifted); report-only, never gates indexing
5. `learning_rehome` ✓ — suggestion-ranked (no target → candidates by name/signature/file;
   confirm with a target → move, id/history preserved), human/LLM-confirmed; `learning_forget`
   removes a dead orphan without needing it to resolve

**git-history rehoming is a prompt pattern, not more code.** `learning_rehome`'s built-in
ranking is structural (name / signature / file). Richer rename evidence — `git log
--follow`, usage pointers — is something the *agent* consults at the prompt (read the
rename from history, then call `crib learning rehome <old> <new>`), not a ranker we hardwire. The
tool stays a confirmed move; the LLM brings the git context.

One thing stays genuinely parked, to keep the human layer clean of the machine layer:
feeding pinned learnings *into* the describe prompt — it would leak human truth into the
regenerable description cache.

## 9. Project lifecycle — onboarding a whole repo

Per-file `code_index` is the primitive; the **lifecycle** commands wrap it so an agent
(or a human) onboards a repo in one call and the "cleared index → re-index → look up"
loop closes without manual per-file work. Three parallel facets share one engine, all
deferring to `_ensure_crib`:

| CLI (noun-verb) | MCP | does |
|---|---|---|
| `crib project setup`  | `project_setup`  | ensure `.crib` + import docs + index all source (**superset**) |
| `crib project index`  | `project_index`  | (re)index the source from `.crib` (cheap re-run via the content-hash gate) |
| `crib project status` | `project_status` | indexed? symbol/file counts, kind breakdown, `.crib` paths |
| `crib project forget` | `project_forget` | clear the `symbol_index` (KEEPS learnings/notes/`.crib` by default) |
| `crib code setup` / `code status` | — | the **code facet** (code-only, no doc import) |

- **`_ensure_crib` — sensible defaults, one primitive.** Finds the repo's `.crib`, or
  writes one: `project` = repo dir name; `paths:` = the LSP-supported extensions that
  actually occur under the root (junk dirs pruned); `docs:` = `README.md` +
  `docs/**/*.md` (indexed IN-SITU — source is master, never copied; legacy `import:`
  is still honoured as a fallback). Globs are YAML-quoted (a bare `- **/*.py` reads
  `*` as an alias anchor). It anchors at the nearest repo marker (`.git`/`pyproject.toml`) **or the cwd
  itself** — never `find_root`'s `base.parent` fallback, which would write `.crib` in the
  wrong dir and index the parent tree.
- **The autonomous loop.** `code_lookup` on an unindexed project self-diagnoses toward
  `project_setup`; the agent runs it (auto-`.crib`, index), then looks up — no grep
  fallback. That's the "clear svg-mcp's index and it re-indexes itself, then queries"
  behaviour.
- **Forget keeps the durable layer.** `project forget` wipes only the regenerable
  `symbol_index`; attached learnings (human source-of-truth), notes and `.crib` survive
  unless you pass `with_learnings`.

The `notes` facet (`notes setup` = docs-only) is the obvious symmetric follow-on.

## 10. Open questions

- **Granularity & hierarchy.** Symbols vs also module- and class-level descriptions
  (a breadcrumb hierarchy, like heading paths). Does file/class context help concept
  search, or just add near-duplicate vectors (the crowding failure mode)?
- **Multi-language reach.** Call hierarchy is uneven (§3.4); is a callers-only graph
  useful enough for the servers that lack callees, or does it read as complete when it
  isn't?
- **Workspace scope & cost.** How many (root, lang) sessions can the daemon hold
  warm before eviction thrash; eager-on-start vs lazy-first-query for init latency.
- **Where the reverse index injects** — a `lookup_code` tool the agent calls, vs a
  `PreToolUse(Grep|Read)` hook that surfaces symbol docs when it opens a source file
  (the injection-vs-pull question from [retrieval-and-adoption.md §7](retrieval-and-adoption.md)).
- **Sharing the LLM output.** `symbol_index/*.toml` is git-tracked and travels with the
  repo — but it describes *code* in another repo, not the notes. Does it live in the
  crib project, or beside the code (a `.crib`-adjacent asset)? Provenance + staleness
  when the code repo moves ahead of the description.
