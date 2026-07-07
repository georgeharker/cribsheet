# crib surface — CLI & MCP reference

The complete surface: every capability, its CLI form, its MCP tool, and a one-line
description, grouped by facet. (For an intro and quickstart, start at the
[README](../README.md).)

Noun-verb is canonical on the CLI (`crib code lookup`); the hyphenated form
(`crib code-lookup`) still parses. `-p/--project` (CLI) and `project`/`project_path`
(MCP) select the project — code tools act on ONE *current* project (set via
`use_project` or inferred from `project_path` on first use); name a different one with
`project=`/`project_path=`. **Writes** (`store`/`append`/`edit`/`forget`/`move`)
require an explicit `project=`/`project_path=` — they never inherit the current one,
so a fact can't land in the wrong project. `--json` before a CLI verb gives machine
output.

## Memory — notes

Two note classes share one index (both surface via `lookup`/`apropos`):
**crib-owned** notes (`store`/`append`/`edit`, code learnings, explicit `import`
copies) live under the crib tree, are editable + git-synced, and are watched for
external edits; **source-owned** docs (a repo's `.crib`-declared docs) are indexed
**in-situ** — the source tree is master, crib holds only the index, `read`/`locate`
return the repo path, and a source watcher reindexes them on save. Every note —
including code learnings — exposes its on-disk `path`.

| CLI | MCP | Description |
|---|---|---|
| `crib lookup` / `search` | `lookup` | Semantic search over notes; returns ranked locator lines (hybrid dense ⊕ BM25). |
| `crib apropos` / `search -a` | `apropos` | Like lookup, but each hit carries the full matching section's markdown, not a snippet. |
| `crib read <rel>` | `read` | Print a note's full raw markdown (frontmatter + body). |
| `crib locate <rel>` | `locate` | Print a note's on-disk path (to edit with your own tools). |
| `crib store <text>` | `store` | Persist a durable fact as a new note (assigns id, indexes it). |
| `crib append <rel> <text>` | `append` | Append content to an existing note (optional heading). |
| `crib edit <rel>` | `edit` | Replace a note's content wholesale (frontmatter preserved). |
| `crib forget <rel>` | `forget` | Delete a note; recoverable via the version ring. |
| `crib move <rel>` | `move` | Move/rename a note across projects, preserving its id + history. |
| `crib reindex <rel>` | `reindex` | Re-index a note (or the whole project) after external edits. |
| `crib reconcile` | `reconcile` | Sweep all projects for offline changes (add/change/delete). |
| `crib versions <rel>` | `versions` | List a note's recoverable prior versions (the write ring). |
| `crib restore <rel> <v>` | `restore` | Restore a prior version of a note. |
| `crib history [rel]` | `history` | Git history for a note or the whole data tree. |
| `crib snapshot [msg]` | `snapshot` | Git checkpoint of the data tree. |
| `crib distill` | `distill` | Re-digest a note via MCP sampling (knowledge capture). |
| `crib elaborate` | `elaborate` | Generate per-section *keyword search terms* (synonyms + phrases a searcher would type, esp. words not in the text) → BM25 `keyword_index`. Not prose expansion. |
| `crib summarize` | `summarize` | Generate per-section *rephrasings* embedded as dense alias vectors → `summary_index` (so differently-worded queries still match). |
| `crib import <path>…` | `import` | Copy NAMED files into memory as crib-owned notes (a snapshot you own: git-synced, editable, versioned). Manual only. Distinct from in-situ docs. |
| `crib import-memory` | — | Live-mirror Claude Code's harness `memory/*.md` into a crib project (host-namespaced; bind-once, daemon keeps synced). |
| `crib projects` | `projects` | List projects. |
| — | `use_project` | Set the session's current project (sticky). |
| — | `current_project` | Report the session's current project. |
| `crib info` | — | Resolved paths, backends, daemon/chunk/retrieve config. |

## Code index — query (reach for these before grep/Read)

| CLI | MCP | Description |
|---|---|---|
| `crib code lookup <query>` | `code_lookup` | Find a symbol by CONCEPT or name — hybrid dense (LLM descriptions) ⊕ name/subtoken. The entry point; self-diagnoses an unindexed project. |
| `crib code dossier <sym>` | `code_dossier` | Everything about ONE symbol in a call: signature, description, callers/callees/references (each neighbour annotated), + any learning. |
| `crib code xref <sym>` | `code_xref` | A symbol's callers (←), callees (→), references (⇐), and any pinned learning. |
| `crib code graph <sym>` | `code_graph` | Call-graph TREE — `callees` / `callers` / `references`, recursive; pstree-rendered; learning-bearing nodes flagged. |
| `crib code index <file>` | `code_index` | (Re)index ONE source file: symbols + call graph + references + descriptions. Usually you want `project` (whole repo). |

## Code learnings — durable human notes attached to a symbol

| CLI | MCP | Description |
|---|---|---|
| `crib code append <sym> <text>` | `code_append` | Pin a durable learning (the "now I get it") to a symbol; survives re-indexing, resurfaces via lookup/xref/dossier. |
| `crib code edit <sym> <text>` | `code_edit` | Rewrite a symbol's learning body wholesale. |
| `crib code forget <sym>` | `code_forget` | Remove a symbol's learning (recoverable via the ring). |
| `crib code read <sym>` | `code_read` | Print a symbol's attached learning. |
| `crib code reaffirm <sym>` | `code_reaffirm` | Clear a learning's ⚠ stale flag without a rewrite (you re-checked; it still holds). |
| `crib code learnings` | `code_learnings` | Health report: each learning `ok` / `moved` / `orphan`. |
| `crib code rehome <old> [new]` | `code_rehome` | Re-point an orphaned learning (no target → ranked candidates; target → move it). |

## Project lifecycle — onboard a whole repo (superset of code + notes)

| CLI | MCP | Description |
|---|---|---|
| `crib project setup` | `project_setup` | Onboard: ensure `.crib` (auto-created), index docs IN-SITU + all source. The one-call "get me going." |
| `crib project index` | `project_index` | (Re)index the repo's code AND in-situ docs from `.crib` (cheap re-run via the content-hash gate). |
| `crib project status` | `project_status` | Indexed? symbol/file counts, kind breakdown, `.crib` paths, doc sources/chunks. |
| `crib project forget` | `project_forget` | Clear the code index (keeps learnings/notes/`.crib`; `--with-learnings` to drop those too). |
| `crib code setup` / `code status` | — | The code facet only (no doc import) — sugar over `project index` / `project status`. |

## Git sync — share notes across machines (CLI-only; pushing is outward-facing)

| CLI | Description |
|---|---|
| `crib setup --remote <url>` | Join a shared notes repo on a new machine (init + merge driver + pull). |
| `crib sync` | Commit + pull + push notes via git. |
| `crib push` / `crib pull` | The halves of sync. |
| `crib serve` / `crib --mcp` | Run the MCP server (stdio or `--http`). |
| `crib merge-driver` | The `merge=cribnote` git driver (invoked by git during a merge). |

---

### Notes on the shape (open questions for review)

- **`code` vs `project` overlap.** `code setup`/`code index` are the code-only facet;
  `project setup`/`index` are the superset (+ docs). Is the `code` facet worth its own
  verbs, or should `project` + a `--no-docs` flag cover it?
- **`code forget <sym>` (a learning) vs `project forget` (the index).** Two different
  "forgets" at two levels — is that clear, or should one be renamed?
- **Notes verbs are top-level; code verbs are namespaced.** `crib store`/`lookup` vs
  `crib code lookup`. A `crib notes <verb>` namespace would make it symmetric — worth it?
- **MCP exposure.** Lifecycle setup/index/status/forget are all agent-callable (so it
  can self-onboard). `import`/`import-memory`/git-sync stay CLI-only (interactive/
  outward-facing). Right split?
