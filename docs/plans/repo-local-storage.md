# Plan: optional in-repo project storage

Status: **EXECUTED 2026-08-05** — landed on dev; kept as the spec-of-record.
(One correction from execution: the example `store: .crib/store` was
impossible — `.crib` is a file; use e.g. `store: .crib-store`, and the
implementation errors explicitly on that case.)
Written for execution by an agent with no prior
context — read DESIGN.md §2 (three roots), §6 (`.crib`/`.cribproject`), §14
(git sync, `$LOCATION` portable paths) first.

## Goal

Let a code repo carry its crib project's **data tier** (notes, learnings,
design/plan notes) in a subdirectory of the repo, declared in `.crib`,
root-relative. Default remains the global store. The derived index (Chroma,
symbol index) **always stays in the global cache dir** regardless.

## Decisions (settled — do not relitigate)

1. **Data tier only.** Only `notes/` (+ its `.versions/` ring) may live
   in-repo. Chroma/caches stay under `$CRIB_INDEX_DIR` keyed by project name,
   so `rm -rf $CRIB_INDEX_DIR && crib reindex --all` remains the universal
   recovery path and embeddings can never be committed to the repo.
2. **Exclusive, not overlay.** A project's data root is *either* global *or*
   in-repo — never both. Migration is an explicit verb pair, not a merge.
3. **`.crib` declares it, a global stub records it.** The repo's `.crib` gains
   `store: <relpath>` (repo-root-relative). On adoption, the global
   `projects/<name>/` dir is reduced to a **stub** holding only `.cribproject`
   with a `store_root` pointer written as a `$LOCATION` portable token
   (`config.portable_path`). The stub is how the daemon finds the store
   *without* a cwd near the repo (project listing, reconcile-at-startup,
   watcher roots all scan `projects_dir` as today).
4. **Repo's git owns in-repo notes.** `crib snapshot/sync/push/pull/setup`
   refuse (clear error, exit 1) for an in-repo project — the user commits notes
   with their repo. `.versions/` moves alongside (`<store>/.versions/`) and is
   gitignored by a `.gitignore` crib writes inside the store dir.
5. **Chunk identity is unaffected.** `chunk_id = sha1(project + relpath + …)`
   uses project-relative relpaths, so migration moves files without changing
   chunk ids or content hashes — **no reindex required on migrate**, only a
   path rebinding. (Run a hash-gated reconcile afterwards anyway; it should
   no-op and is the verification.)

## Config surface

`.crib` (committed, portable — names and relative paths only):

```yaml
project: myproj
store: .crib/store        # NEW — repo-root-relative; validated: relative, no '..'
```

Stub `projects/<name>/.cribproject` (machine-local pointer):

```yaml
name: myproj
store_root: $DEV/myrepo/.crib/store   # portable token, expanded via [locations]
```

## Implementation steps

### 1. `crib/config.py`

- `CribLink`: add `store: str | None` field; parse `store` key in `find()`.
  Validate in a property/helper: must be relative, must not escape the repo
  root (`(root / store).resolve().is_relative_to(root.resolve())`), else
  raise `ValueError` with the offending value.
- `ProjectConfig`: add `store_root: str | None`; parse in `load()`. Add a
  `save()`/write helper (there may not be one yet — `.cribproject` is
  currently read-only; add minimal YAML dump preserving known keys).

### 2. `crib/paths.py` — the central seam

Today `Paths.notes_dir(project)` / `project_dir(project)` are pure global
functions of the name. Introduce resolution:

- New `ProjectPaths` dataclass: `{project_dir, notes_dir, versions_dir}`.
- New `resolve_project_paths(paths: Paths, cfg: Config, project: str) ->
  ProjectPaths`: load the stub `.cribproject`; if `store_root` set, expand via
  `expand_location(token, cfg.locations)` → in-repo layout
  (`notes_dir = <store>/notes`, `versions_dir = <store>/.versions`); else the
  current global layout (note: global ring is the *shared* `data_dir/.versions`,
  keyed by note id — keep that behavior for global projects).
- **Sweep every call site** of `paths.notes_dir(...)` / `paths.project_dir(...)`
  / `paths.versions_dir` (grep across `crib/`) and route through
  `resolve_project_paths`. This is the bulk of the change; do it mechanically
  and keep a per-call-site checklist in the PR description. Cache the
  resolution per (project) in the app/daemon object — it's hit on every call.

### 3. Availability handling

If `store_root` expands to a missing directory (repo not cloned on this
machine, `[locations]` name unknown):

- `project_list`: still lists the project, flagged `unavailable: true` with the
  unexpanded token shown.
- Any read/write verb: raise a tool error naming the token and the fix
  ("clone the repo / add `[locations]` entry / `crib project release <name>`").
- Startup reconcile + watcher: skip unavailable projects with one warning log
  line, never crash the daemon.

### 4. Watcher (`crib/watch.py`) + reconcile

- Watch roots = global `projects_dir` **plus** each available in-repo store's
  `notes/`. Recompute on daemon start; a mid-life adoption takes effect on
  next daemon start (same accepted limitation as memory-bindings, DESIGN §13).
- Ignore rules unchanged (`.versions/`, dotfiles, temp patterns) — they apply
  per-root.

### 5. Migration verbs (new, both CLI and MCP — pairing rule below)

- `project_adopt(project?, cwd?)` / `crib project adopt`: requires a `.crib`
  with `store:` at/above cwd. Moves `projects/<name>/notes/**` +
  the project's ring entries into the repo store, writes the stub
  `.cribproject` `store_root` (portable token), writes `<store>/.gitignore`
  (`.versions/`), then hash-gated reconcile (expect no-op). Refuse if store dir
  already has notes (no merge — decision 2).
- `project_release(project?)` / `crib project release`: inverse — move files
  back to global, clear `store_root`, reconcile.
- Both print a summary (files moved, destination) and are idempotent no-ops
  when already in the requested state.

### 6. Git-sync guards (`crib/gitbacking.py` / CLI sync verbs)

At the top of `setup/snapshot/sync/push/pull`: if the target project resolves
in-repo, error: "notes for <name> live in <repo>; commit them with that repo's
git." (`snapshot` with no project arg operates on the global tree only — in-repo
stores are simply not part of it.)

### 7. Tests

- Unit: `CribLink.store` validation (absolute path, `..` escape, missing).
- Unit: `resolve_project_paths` global vs in-repo vs unavailable.
- Integration: adopt → verbs work against repo store → chunk ids unchanged
  (capture ids before/after, assert equal) → release → same.
- Watcher: edit a file in an in-repo store, assert reindex fires.
- Sync guard: adopt then `crib sync` errors.

## CLI⇄MCP pairing rule (applies to every verb here)

Every new verb ships on **both** surfaces in the same PR: MCP tool
`project_adopt` ⇄ CLI `crib project adopt`, same parameter names, same
defaults, both calling the *same* function in `project_services.py` /
`app.py` — no logic in `server.py` or `cli.py` beyond arg plumbing and
formatting. Update `docs/surface.md`.

## Follow-ups (approved 2026-08-05, not yet executed)

### F1 — store exclusion from `.crib` globs (latent-bug fix; do promptly)

A `.crib` with `docs: [**/*.md]` (or any glob reaching the store) matches the
in-repo store's notes and indexes them a SECOND time as in-situ docs —
different chunk identities (`<relpath>` as note vs `sources/<repo>/…` as
doc), so real duplicates and retrieval decoys, not hash-gated no-ops. Fix as
one rule at every enumeration point: **paths under the declared store dir
never match `.crib` globs** — `index_docs_insitu` expansion, code sweep glob
expansion, and the code/doc watcher decode (same treatment `.git`/
`.versions` get). `project adopt` additionally warns when existing globs
would overlap the store. Tests: adopted project + `**/*.md` docs glob →
store notes indexed once (as notes only); watcher edit in store fires the
notes path only.

### F2 — optional annotation-tier flip (`store_index: true`) — parity, not default

For mechanism parity, allow (never default) moving the annotation tier —
`symbol_index/*.toml` (LLM descriptions, keywords, learning pins) + section
facets — into the store: `.crib` gains `store_index: true`, adopt/release
honor it. The deliberately-unrouted call sites are enumerated in this doc's
execution report (codeindexer/codestore/refs/learnings/app SymbolIndex+
SectionIndex construction) — route them through ProjectPaths. Repo-noise
guidance: mark the dir `linguist-generated` via .gitattributes; merge safety
already exists (tomllib-or-merge-dirty parsing, content-addressed facets).
Chroma, machine-local registries and the stub NEVER move. Maintainer stance:
"probably could be, not convinced it's optimal — possible for parity";
global stays default and recommended.

## Out of scope

- Overlay/split storage (some notes global, some in-repo).
- Moving the config tier or Chroma in-repo (F2 covers only annotations).
- Live rebinding of watcher roots without daemon restart.
