# crib surface — CLI & MCP reference

The complete surface: every capability, its CLI form, its MCP tool, and a one-line
description, grouped by facet. (For an intro and quickstart, start at the
[README](../README.md); for a walkthrough, see the [guide](guide.md).)

The CLI is **noun-verb**: `crib <noun> <verb>` — `crib note lookup`, `crib code xref`,
`crib learning add`, `crib project setup`. That is the only form; there is no
hyphenated fallback (`crib code-lookup` is rejected). The nouns are `note`, `code`,
`learning`, and `project`, plus a few top-level system verbs.

**Selecting a project.** `-p/--project` (by name) or `-P/--project-path` (by a path
inside the repo) on the CLI — `project`/`project_path` on MCP — pick which project a
command acts on. Code and learning commands act on ONE *current* project (set with
`crib project use`, or inferred from a path on first use); name a different one to
override. **Writes** (`store`/`append`/`edit`/`forget`/`move`, and learnings) require
an explicit project — they never silently inherit the current one, so a fact can't
land in the wrong place.

**Global flags** go before the noun: `--json` for machine-readable output, `--no-daemon`
to run in-process instead of attaching to the warm daemon (see [Server & daemon](#server--daemon)).
Content-taking verbs (`note store`/`append`/`edit`, `learning add`/`edit`) accept `-`
to read the content from stdin.

## Memory — notes

Two note classes share one index (both surface via `lookup`/`apropos`): **crib-owned**
notes (`store`/`append`/`edit`, imported copies, and code learnings) live in the crib
tree, are editable + git-synced, and are watched for external edits; **source-owned**
docs (a repo's `.crib`-declared docs) are indexed **in-situ** — the source tree stays
master, crib holds only the index, and `read`/`locate` return the repo path. Every
note exposes its on-disk `path`.

| CLI | MCP | Description |
|---|---|---|
| `crib note lookup <query>` (alias `search`) | `note_lookup` | Semantic search over notes; returns ranked locator lines (hybrid dense ⊕ BM25). `-a/--render` renders full sections. |
| `crib note apropos <query>` (alias `a`) | `note_apropos` | Like lookup, but each hit carries the full matching section's markdown, not a snippet. |
| `crib note read <rel>` | `note_read` | Print a note's full raw markdown (frontmatter + body). |
| `crib note locate <rel>` | `note_locate` | Print a note's on-disk path (to edit with your own tools). |
| `crib note store <text>` | `note_store` | Persist a durable fact as a new note (assigns an id, indexes it). |
| `crib note append <rel> <text>` | `note_append` | Append content to an existing note (optional heading). |
| `crib note edit <rel>` | `note_edit` | Replace a note's content wholesale (frontmatter preserved). |
| `crib note forget <rel>` | `note_forget` | Delete a note; recoverable via the version ring. |
| `crib note move <rel> --to-project/--to-relpath` | `note_move` | Move/rename a note across projects, preserving its id + history. |
| `crib note reindex [rel]` | `note_reindex` | Re-index a note (or the whole project) after external edits. |
| `crib note versions <rel>` | `note_versions` | List a note's recoverable prior versions (the write ring). |
| `crib note restore <rel> <v>` | `note_restore` | Restore a prior version of a note. |
| `crib note history [rel]` | `note_history` | Git history for a note or the whole data tree. |
| `crib note snapshot [-m msg]` | `note_snapshot` | Git checkpoint of the data tree. |
| `crib note distill <rel>` | `note_distill` | LLM-revise a note in place (compress/dedupe/normalize). |
| `crib note elaborate <label> [rel]` | `note_elaborate` | Generate per-section *keyword search terms* (synonyms + phrases a searcher would type) to strengthen BM25 matching. Not prose expansion. |
| `crib note summarize <label> [rel]` | `note_summarize` | Generate per-section *rephrasings* embedded as dense aliases, so differently-worded queries still match. |
| `crib note import <path>…` | `note_import` | Copy NAMED files into memory as crib-owned notes (a snapshot you own: git-synced, editable, versioned). |
| `crib note import-memory` | `note_import_memory` | Mirror an AI harness's `memory/*.md` into a crib project (host-namespaced). One-way, idempotent, and live-synced thereafter. |

`note lookup` also takes retrieval-tuning overrides — `-k`, `--tag`, and
`--keywords`/`--keyword-weight`/`--summaries`/`--summary-weight` (MCP:
`keyword_labels`/`keyword_weight`/`summary_labels`/`summary_weight`) — to override which
`elaborate`/`summarize` index sets feed retrieval, mainly for eval sweeps.

## Code index — search & navigate (reach for these before grep)

A repo's `.crib` may name other projects under `refs:`, and queries then fan out: a
symbol missing locally is resolved from the refs, `code lookup` merges the ranked
hits (each hit carries its `project`), and `dossier`/`graph` follow edges across
projects.

| CLI | MCP | Description |
|---|---|---|
| `crib code lookup <query>` | `code_lookup` | Find a symbol by CONCEPT or name — hybrid dense (LLM descriptions) ⊕ name/subtoken. The entry point; self-diagnoses an unindexed project. |
| `crib code dossier <sym>` | `code_dossier` | Everything about ONE symbol in one call: signature, description, callers/callees/references (each neighbour annotated), plus any attached learning. |
| `crib code xref <sym>` | `code_xref` | A symbol's callers (←), callees (→), references (⇐), and any pinned learning. |
| `crib code graph <sym>` | `code_graph` | Call-graph TREE — callees / callers / references, recursive, pstree-rendered; learning-bearing nodes flagged. |
| `crib code index <file>` | `code_index` | (Re)index ONE source file. Usually you want `crib project index` (whole repo) instead. |

## Learnings — durable notes attached to a code symbol

| CLI | MCP | Description |
|---|---|---|
| `crib learning add <sym> <text>` | `learning_add` | Pin a durable learning (the "now I get it") to a symbol; survives re-indexing, resurfaces via lookup/xref/dossier. |
| `crib learning edit <sym> <text>` | `learning_edit` | Rewrite a symbol's learning body wholesale. |
| `crib learning forget <sym>` | `learning_forget` | Remove a symbol's learning (recoverable via the ring; works on orphans). |
| `crib learning read <sym>` | `learning_read` | Print a symbol's attached learning. |
| `crib learning reaffirm <sym>` | `learning_reaffirm` | Clear a learning's ⚠ stale flag without a rewrite (you re-checked; it still holds). |
| `crib learning report` | `learning_report` | Health report: each learning `ok` / `moved` / `orphan` (`--orphans` to filter). |
| `crib learning rehome <old> [new]` | `learning_rehome` | Re-point an orphaned learning (no target → ranked candidates; target → move it). |

## Project lifecycle — onboard & manage a whole repo

| CLI | MCP | Description |
|---|---|---|
| `crib project setup` | `project_setup` | Onboard a repo: ensure `.crib` (auto-created), import its docs in-situ, and index all source. The one-call "get me going." |
| `crib project index` | `project_index` | (Re)index the repo's code AND in-situ docs from `.crib` (cheap re-run via the content-hash gate). The code-only onboard. |
| `crib project status` | `project_status` | Is it indexed? symbol/file counts, kind breakdown, `.crib` paths, doc sources. |
| `crib project forget` | `project_forget` | Clear the code index (keeps learnings/notes/`.crib`; `--with-learnings` to drop those too). |
| `crib project reconcile` | `project_reconcile` | Sweep ALL projects for offline changes (add/change/delete). Idempotent. |
| `crib project list` | `project_list` | List projects (separate memory namespaces). |
| `crib project use <name>` | `project_use` | Set this session's current project (sticky; creates the namespace). |
| `crib project current` | `project_current` | Show this session's current project (+ available projects). |

## Server & daemon

One warm process serves both the CLI and MCP.

| CLI | MCP | Description |
|---|---|---|
| `crib status` | `status` | One-call health summary: per-project inventory (notes/docs/symbols/learnings), git-sync state, attached language-server sessions, in-flight indexing. |
| `crib serve` / `crib --mcp` | — | Run the MCP server: stdio by default, `--http --host --port` for HTTP. |
| `crib info` | — | Resolved paths, backends, and daemon/chunk/retrieve config. |
| `--no-daemon` (global) | — | Run the verb in-process instead of attaching to the warm daemon — e.g. to exercise freshly edited code without a daemon restart. |
| `--json` (global) | — | Machine-readable output for any verb. |

## Git sync — share notes across machines (CLI-only)

Pushing publishes to a remote, so these stay CLI-only (not agent-callable).

| CLI | Description |
|---|---|
| `crib note setup --remote <url>` | Join a shared notes repo on a new machine (init + frontmatter merge driver + pull). |
| `crib note sync` | Commit + pull + push notes via git, then reindex. |
| `crib note push` / `crib note pull` | The halves of sync (`pull` reindexes after). |
| `crib merge-driver` | The frontmatter-aware git merge driver (invoked by git during a merge; hidden from `--help`). |
