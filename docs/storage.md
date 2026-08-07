# Where things live — store types, ownership, and how to use each

Cribsheet stores several *kinds* of content with different owners, write
rules, and lifecycles. This page is the map. (Design rationale: DESIGN §2,
§6, §8, §13, §14; the in-repo option in §6.)

## The three roots (lifecycles)

| Root | Default | Holds | Lifecycle |
|---|---|---|---|
| **config** | `~/.config/crib` | `config.toml` | hand-edited |
| **data** | `~/.local/share/crib` | everything authored or curated (below) | **precious** — the truth |
| **index** | `~/.cache/crib` | Chroma embeddings, chunk-schema marker | disposable — `rm -rf` + reconcile is a supported recovery |

Env overrides: `CRIB_CONFIG_DIR` / `CRIB_DATA_DIR` / `CRIB_INDEX_DIR`.

## Content classes inside a project

Everything below lives under `projects/<name>/` in the data root (unless the
project is adopted in-repo — next section). **Owner** = who may write the
bytes; **write via** = the sanctioned path.

Notes, design decisions, plan items and symbol learnings are FOUR SIBLING
PILLAR STORES — one shared store implementation, four instances, four
directories. Each pillar indexes and ranks over its own corpus only:
`note_lookup` can never surface (or be re-weighted by) facet content, and
`design_lookup`/`plan_lookup` search pure facet pools. Relpaths are
store-relative (`base.md`, not `design/base.md`); a citation of a facet note
from another store spells it qualified (`design:base.md`).

| Class | Path | Owner | Write via | Notes |
|---|---|---|---|---|
| **Notes** | `notes/**.md` | crib (you/agents) | `note_store/append/edit`, or raw edit + watcher | the general memory surface. The note verbs refuse paths in the other pillars (`design/…`, `plans/…`, `learnings/…`) |
| **Design decisions** | `design/*.md` | the design facet | `design_add/edit/append` — **not** `note_*` | the store impl is shared backend; only facet verbs know the edges. Raw file edits (editor, git pull) are caught by hash-taint but lose the edge-aware feedback |
| **Plan items** | `plans/*.md` | the plan facet | `plan_add/status/move` | same rule as design |
| **Symbol learnings** | `learnings/*.md` | the learning facet | `learning_add/edit/forget` | pinned to code symbols; `rehome` when symbols move |
| **In-situ docs** | keyed `sources/<repo>/…` — the FILES stay in the repo | **the repo** | edit in the repo checkout; never through note verbs (writes are refused) | declared by `.crib` `docs:` globs; crib indexes in place, owns nothing |
| **Imported copies** | `imported/<repo>/…` | crib, but source-wins | `note_import` (re-pull overwrites) | one-way snapshot; provenance in frontmatter |
| **Claude-memory mirror** | `notes/claude-memory/<host>/…` | **the harness** | never write here; crib mirrors one-way | host-namespaced so machines merge |
| **Version ring** | `.versions/<note-id>/` (data root; or `<store>/.versions/` in-repo) | crib, automatic | every write stashes prior bytes; `note_versions`/`note_restore` | never indexed, never searched |
| **Symbol index** | `symbol_index/*.toml` | crib (LLM-generated + pins) | `project_index` / the describe backlog | semi-derived: rebuildable but LLM-expensive; always in the project dir |
| **Section facets** | keyword/summary index storage | crib (LLM-generated) | the elaborate/keyword backlog | same semi-derived tier |

Rules of thumb:

- **If the repo owns it (`sources/…`, claude-memory), crib never writes it** —
  writes through note verbs are refused by design, not just discouraged.
- **If a facet owns it (design/plans/learnings), use the facet's verbs** — the
  pillar stores share one implementation, but the facet verbs carry the
  semantics (edges, taint, status) that raw edits silently bypass. Editing the
  file directly still works (the watcher reindexes, hash-taint catches drift);
  addressing facet content through `note_*` does not.
- **Attribution (`sources:` in design/plan frontmatter) always cites a
  section**, never a whole document (unless the document has no headings) —
  section-hash checking is what keeps references live without whole-file churn.

## Global vs in-repo project storage

**Default — and recommended: global.** Every project's data lives under
`projects/<name>/` in the data root. Sync across machines via the data-root
git remote (`crib memory setup/sync`).

**Opt-in — in-repo (data tier only).** A repo may carry its project's
*authored* tier in-tree:

```yaml
# .crib at the repo root
project: myproj
store: .crib-store        # repo-relative; NOT ".crib/…" (.crib is a file)
```

then `crib project adopt`. What moves: every pillar dir (`notes/`, `design/`,
`plans/`, `learnings/`) and that project's version ring
(`<store>/.versions/`, gitignored). What never moves: the Chroma index (embeddings are rebuildable
and must not be committable), the symbol index and section facets, the
machine-local registries, and the stub `projects/<name>/.cribproject` — the
stub stays behind holding a portable `store_root:` token so the daemon can
find the store from anywhere.

Consequences to know:

- **Exclusive, not overlay** — a project is global or in-repo, never both;
  `crib project release` moves it back.
- **The repo's git owns in-repo notes** — commit them with the repo;
  `crib memory sync/push/pull/setup` refuse for such projects.
- **`.crib` globs never match the store** — a `docs: ["**/*.md"]` will not
  double-index your notes as in-situ docs; adopt warns if globs overlap.
- **On a machine without the checkout** the project lists as `unavailable`
  with its token; reads/writes error with the fix (clone it, map the
  `$LOCATION`, or `release`).
- Chunk ids are path-independent, so adopt/release never reindexes.

## Legacy layout migration

Pre-split layouts kept facet notes inside the notes tree (`notes/design/`,
`notes/plans/`, `notes/code-learnings/`). Every full reindex self-heals that
layout automatically — files move to the sibling pillars, `sources:` refs
using the old spelling are requalified (`design/x.md` → `design:x.md`,
frontmatter only, so nothing gains taint), and the same sweep re-keys the
chunks. `crib project migrate` runs it on demand with a report. Name
collisions between the two layouts are skipped and reported, never merged.
Upgrade crib on every synced machine before relying on the new layout — old
code cannot see the sibling dirs (no data loss; it heals on upgrade).

## What to use when (quick chooser)

- durable fact, gotcha, convention → `note_store` (name the project it's
  *about*)
- a decision with consequences → `design_add` with deps; changing one →
  `design_check` first
- work that outlives the session → `plan_add`; resume with `plan_next`
- a hard-won insight about a symbol → `learning_add <symbol>`
- repo docs searchable without copying → `.crib` `docs:` globs +
  `project_index`
- a doc you want to own and edit in crib → `note_import` (accepts re-pull
  overwrite)
- notes traveling with a specific repo → `.crib store:` + `project adopt`
- notes shared across machines (global projects) → `crib memory sync`
