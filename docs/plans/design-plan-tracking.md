# Plan: design-decision & plan tracking (dependency graphs)

Status: **IN TREE** — implemented in `crib/designs.py` (both facets), registered
on both surfaces (`crib/server.py` tools + `crib/cli.py` `design`/`plan` noun
subparsers, parity-checked), tested in `tests/test_designs.py`, documented in
DESIGN.md §5 and docs/surface.md. This doc is kept as the rationale record: the
Decisions section below is what the code implements.

Two implementation choices worth knowing, both inside the spec's decisions:
`design_supersede` appends the supersession to the decision's BODY, so dependents
taint through the ordinary body-hash path (decision 3's "no write fan-out" holds);
and `design_tree` takes `direction=deps|dependents` — the spec called for both
directions but named no parameter.

Written for execution by an agent with no prior
context — read DESIGN.md §3 (note model), §5 (tool surface / noun-verb
convention), §15.1 (relpath slugs, `created` semantics) first, and skim
`crib/learnings.py` — it is the **structural template** for adding a noun
facet (a small class over `NoteStore`, registered in `server.py`, subcommands
in `cli.py`).

## Goal

Two new note-backed facets:

- **`design`** — durable design decisions with typed dependencies on other
  decisions. Editing or deleting a decision *taints* its dependents so they can
  be re-checked. Goal: reduce unexpected consequences of design drift.
- **`plan`** — persistent, resumable plan items with status, dependencies, and
  a maintainable ordering.

Both are **markdown notes with structured frontmatter** — they inherit search
(semantic + BM25), versioning ring, git sync, and the merge driver for free.
The only genuinely new machinery is the graph layer and its invariants.

## Decisions (settled — do not relitigate)

1. **Notes are the substrate; no new store.** Design notes live at
   `notes/design/<slug>.md`, plan items at `notes/plans/<slug>.md`, with
   frontmatter `type: design` / `type: plan`. They index like any note (the
   existing chunker/watcher path). Identity is the existing ULID `id`.
   > **Revised by the pillar split** (design graph: "Four pillar stores…"):
   > the shared store *implementation* remains the substrate, but design/plans
   > became sibling pillar stores (`design/<slug>.md`, `plans/<slug>.md` under
   > the data root) with their own retrieval scope — facet content no longer
   > indexes "like any note", precisely so it cannot pollute note search or
   > ranking. Everything else in this doc (ids, deps, taint) still holds.
2. **Deps are id-lists in frontmatter.** `deps: [<ulid>, …]`. May reference
   design ids, plan ids, or plain note ids (one namespace). Dangling ids are a
   validation *warning*, not a crash.
3. **Staleness is computed on read, recorded on verify.** Each note carries
   `checked: {<dep_id>: <sha1 of dep body at last verify>, …}`. A dependent is
   **tainted** when a dep's current body hash ≠ its recorded hash (or a dep is
   missing). No background propagation, no write fan-out on edit — `_check`
   verbs compute it live; `_verify` re-records hashes. Transitive taint =
   reachability over tainted edges.
4. **Delete blocks on dependents.** `design_forget` errors listing dependents;
   `--force` deletes and leaves dependents tainted (their `checked` entry now
   points at a missing id).
5. **Ordering by rank string, not sequence numbers.** Plan items carry
   `rank: <lexorank string>`; insert-between never renumbers neighbors.
   Rendered order = **topological sort by deps, rank as tie-breaker among
   ready/independent items**. Deps guarantee correctness; rank expresses
   preference only.
6. **Status enum:** `todo | in-progress | done | verified` stored; `blocked`
   is **derived** (any dep not `done`/`verified`) and never stored.

## Frontmatter schemas

```yaml
# notes/design/<slug>.md
---
id: 01J…
title: CLI and MCP surfaces stay paired
type: design
status: active            # active | superseded
deps: [01H…, 01G…]        # decisions/notes this one builds on
links: [docs/surface.md]  # freeform pointers (paths, URLs)
checked: {01H…: "ab12…", 01G…: "cd34…"}
created: 2026-08-05
updated: 2026-08-05
---
Body: the decision, rationale, alternatives rejected.
```

```yaml
# notes/plans/<slug>.md
---
id: 01J…
title: Sweep notes_dir call sites
type: plan
status: todo              # todo | in-progress | done | verified
deps: [01J…]              # must-precede items (plan or design ids)
rank: "hzzt"              # lexorank; see below
links: []
created: 2026-08-05
updated: 2026-08-05
---
Body: what to do, acceptance criteria.
```

## New module: `crib/designs.py` (one module, both facets)

Follow `crib/learnings.py` shape: a class taking `(paths, notestore, …)`,
async write verbs funneling through the existing locked `index_file` write
path (whatever `NoteStore` write helper `learnings.py` uses — reuse it).

Core helpers:

- `_load_graph(project) -> Graph`: scan `notes/design/` + `notes/plans/`
  frontmatter (cheap: YAML header only, don't chunk), build `{id: node}` with
  edges from `deps`. Detect cycles (DFS); a cycle is an error on the write
  that would create it (`*_dep_add` validates before writing).
- `_body_hash(note) -> sha1` of the body (frontmatter excluded — metadata
  churn must not taint dependents).
- `_taint(graph) -> {id: [reasons]}`: per node, compare each dep's current
  body hash against the node's `checked` entry; missing dep or missing
  `checked` entry ⇒ tainted. Then propagate transitively.
- `_resolve_ref(project, ref) -> id`: accept a full ULID, unique ULID prefix,
  relpath, or exact title slug; error listing candidates when ambiguous.
- Lexorank: `_rank_between(a: str | None, b: str | None) -> str` over
  lowercase a–z (midpoint string; append `"m"` on ties/exhaustion). ~20 lines
  + property test (`a < rank_between(a,b) < b`).

## Verb surface (every verb: MCP tool + `crib design|plan <verb>`, same args,
same defaults, shared implementation in `designs.py` — see pairing rule below)

| MCP | behavior |
|---|---|
| `design_add(title, content, deps?, project?)` | create under `notes/design/`, slug per §15.1; validates+resolves deps; `checked` seeded from current dep hashes (a new decision is born verified) |
| `design_dep_add(ref, dep_ref, …)` / `design_dep_remove` | edit `deps`; cycle-check; `dep_add` does **not** seed `checked` (new dep starts unverified ⇒ tainted ⇒ shows up in `check` — the nudge to actually reconsider) |
| `design_forget(ref, force?)` | decision 4 |
| `design_check(project?, ref?)` | list tainted decisions, each with the dep path(s) explaining *why* (`X changed → Y depends on X → …`) |
| `design_verify(ref)` | re-record `checked` hashes for that node (after human review) |
| `design_tree(ref?, depth?)` | render the dep tree (down: what this builds on; up: what depends on this), taint-flagged — mirror `code_graph`'s tree rendering in `cli.py` |
| `design_supersede(ref, by_ref?)` | `status: superseded` + taint dependents (soft delete) |
| `plan_add(title, content, deps?, after?, before?, project?)` | rank from `_rank_between` of neighbors (default: end) |
| `plan_status(ref, status)` | set status; reject unknown enum; warn (not block) when marking `done` with unfinished deps |
| `plan_dep_add` / `plan_dep_remove` / `plan_forget` | mirror design verbs |
| `plan_move(ref, after?/before?)` | re-rank only — deps untouched |
| `plan_list(project?, all?)` | topo-sorted, rank tie-broken; default hides `done`/`verified` unless `all`; shows derived `blocked`; flags cycles if any snuck in |
| `plan_next(project?, k?)` | actionable items: `todo` with all deps satisfied, in rank order |

Search needs **no new verbs**: `note_lookup(tags=…)` already filters; ensure
`type` lands in chunk metadata (check `crib/chunk.py` frontmatter→metadata
plumbing; add `type` if absent) and mention `type:design`/`type:plan`
filtering in the `note_lookup` docstring. Read/edit reuse `note_read`/
`note_edit`/`note_locate` (they're just notes). Editing a design body via
`note_edit` or a raw edit is *automatically* caught: taint is hash-computed,
so no hook is needed — this is why decision 3 is load-bearing.

## Wiring checklist

1. `crib/designs.py` — module above (+ unit tests: cycle detection, taint
   propagation incl. transitive + missing-dep, lexorank property, ref
   resolution ambiguity).
2. `crib/server.py` — register the ~14 tools next to the `learning_*` block;
   docstrings are the LLM's UI: one line of *when to reach for it* each.
   Session/project resolution identical to `note_*` (`_project` helper).
3. `crib/cli.py` — `design` and `plan` noun subparsers; human rendering for
   `tree`/`list`/`check` (reuse the code_graph tree style); `--json`
   passthrough like other verbs.
4. `docs/surface.md` — add both facets to the CLI⇄MCP table.
5. MCP server instructions blurb (wherever the `learning_add` guidance string
   in `server.py` lives, ~line 186): one sentence steering the LLM to record
   design decisions and check deps before changing one.
6. Integration test: add A; add B dep A; `check` clean → edit A's body via
   plain `note_edit` → `check` reports B tainted with path → `verify B` →
   clean. Plan: add 3 items with deps, `plan_next` respects topo+rank,
   `move` reorders without touching deps.

## Dogfood (last step of execution)

Record the first real design notes in the `cribsheet` project itself:
(1) "CLI and MCP surfaces stay paired — every noun-verb on both, same args,
shared impl"; (2) "disk is truth, Chroma is rebuildable cache"; (3) decisions
1–6 from this doc, with deps between them. Then `design_check` after any
future change to §5 of DESIGN.md is exactly the feature's acceptance demo.

## Out of scope

- Automatic taint on *code* changes (linking design ids to symbols — future:
  a `learning`-style attachment could bridge this).
- Cross-project deps.
- Any background/watcher-driven propagation.
