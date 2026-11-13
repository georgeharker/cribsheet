# Changelog

All notable changes to cribsheet are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Dev releases
(`X.Y.Z-devN`) are cut from the `dev` branch.

This file ships in the Pi package, where Pi shows the entries newer than the
version you last ran. `scripts/bump-version.sh` refuses a release whose
version has no heading here.

## [0.23.0-dev1] - 2026-11-12

The DuckDB cache now has one uniform shape: an item per note, decision, plan,
learning or code symbol; one table per kind for that kind's own fields; one
table of links between items; and the lexical postings. The files stay the
truth. The store is kept exact by every read and write, and answers the
queries.

### Added

- Dependency kinds. A dependency can say what it means: `follows` (blocked
  until the target is done), `rests_on` (blocked while the target is stale)
  or `informed_by` (checked for change, never blocks). Without one, a dep
  gets the target's default: a plan is `follows`, a decision `rests_on`, a
  note `informed_by`. Use `--follows`, `--rests-on`, `--informed-by` or
  `dep-add --as`; reads report each dep's kind.
- A plain note can be a dependency target: checked for change, never
  blocking. It used to read as a deleted dependency.

### Changed

- Code search runs from the store, so crib no longer keeps every project's
  symbols, keyword index and description vectors in memory. On a 10,000
  symbol project a warm read of all records takes 0.2 s.
- Design and plan graph reads parse only files changed since the last read:
  0.16 s to 0.03 s on a 455-item project.
- The startup and post-pull reconcile reads only notes changed since they
  were last indexed: 4.2 s to 0.5 s across 21 projects. An explicit
  `reindex` still re-reads everything.
- The watcher refreshes the store as record files, decisions and plans
  change; reads still catch anything it misses.
- Decisions written by crib no longer carry an empty `links: []` line.

### Fixed

- Projects that vendor the same file (the same symbol in several projects)
  no longer overwrite each other's symbols in the store.

### Upgrading

- The store's schema changes, so the first daemon on this version rebuilds
  its DuckDB cache once from the files. Nothing in your notes, decisions,
  plans or symbol records changes.

## [0.22.1] - 2026-11-10

### Fixed

- `crib design add` from the CLI failed on every call since 0.18.0 with
  "Unexpected keyword argument" for `path`, `scope` and `lang`: it sent the
  code commands' narrowing options, which the `design_add` tool doesn't take.
  The MCP tool was unaffected. The CLI/MCP parity test now checks that the
  CLI sends only parameters the tool declares.

## [0.22.0] - 2026-11-09

### Fixed

- **Shared store recovery.** One failed reconnect no longer leaves the
  shared (Quack) client unusable until the daemon restarts; threads that
  lose the same session relaunch the server once between them; and with
  `[duckdb] launch = false` a reconnect waits for the supervised server.
- **`tags=` lookups return k results.** The tag filter now runs inside the
  store query instead of after the top results were picked.
- **The embedding cache can't be poisoned** by the hash-embedder fallback:
  it keys on the embedder actually built and never caches hash vectors.
- A zero vector scores 0 instead of NaN, which sorted above every match.
- Hybrid retrieval scores keyword-only candidates on their stored vectors
  (heading breadcrumb plus body, as the dense search ranks them) instead of
  re-embedding the bare body.

### Changed

- `code_lookup` and summary-alias scoring use numpy (about 140 ms to 0.1 ms
  per code query at 16k symbols). numpy is now a direct dependency.
- Shared mode sends each vector as one literal: writes about 20x faster.
- A note write is one transaction; metadata refreshes are one statement
  per batch.
- A `crib_meta` table records the schema version, record-cache format and
  embedder; a change drops or rebuilds what depended on it, and switching
  embedders at the same vector size now recreates the store.
- The generation ledger forgets files that no longer need a description;
  the embedding cache drops other embedders' vectors.
- `crib status` shows `store_stats` (rows, versions, sizes, reconnects) and
  embedding-cache hits; the store checkpoints after a startup reconcile.

### Added

- `crib store install-extensions`: fetch the shared-mode DuckDB extensions
  ahead of time, for containers and offline machines.

### Removed

- The `json` store backend (`[store] backend = "json"` now warns and uses
  DuckDB; the old `~/.cache/crib/store.json` can be deleted), and leftover
  chroma-era code paths.

### Upgrading from a 0.22.0 dev release

- Chunks whose tags were stored as a comma-joined string are converted to the
  new list form when the store opens, so `tags=` lookups work at once.
- Embedding-cache rows keyed by the old configured model name are dropped
  once; the vectors in the store itself are untouched.

## [0.22.0-dev2] - 2026-11-09

### Added

- `docs/vector-store.md`: the DuckDB setup (embedded vs shared mode, every
  `[duckdb]` key, file layout, the per-machine caches, recovery).
- `docs/migrating-to-duckdb.md`: upgrading a 0.21.x machine from chroma.
- This changelog. The Pi package ships it and points `pi.changelogPath` at it,
  so Pi lists what changed after an update.

### Changed

- `scripts/bump-version.sh` refuses a version with no `CHANGELOG.md` heading,
  and copies the changelog into `plugins/pi/` with each release.
- Dev-release tags are published as GitHub prereleases, so they never take
  "Latest" from the last stable release.
- Docs no longer describe chroma as the current store.

## [0.22.0-dev1] - 2026-11-09

### Removed

- **chroma.** DuckDB is the only vector store. Gone with it: `ChromaStore`,
  the shared chroma server, the `[chroma]` config table (now ignored with a
  one-time warning), the chroma CVE guard, and the `chromadb` dependency.
  Upgrading: [docs/migrating-to-duckdb.md](https://github.com/georgeharker/cribsheet/blob/main/docs/migrating-to-duckdb.md).

### Added

- **DuckDB store**, selected by `[store] backend = "duckdb"` (the default),
  with exact cosine search. `[duckdb] mode = "embedded"` opens the file in
  process. `mode = "shared"` runs a loopback-only, token-protected DuckDB
  server (`crib-duckdb`, port 7734) through sharedserver.
  `[duckdb] launch = false` attaches to a server something else supervises.
- **Embedding cache.** Embedded vectors persist, so a warm start embeds only
  new or changed text.
- **Record cache.** Symbol-index reads skip unchanged TOML files; on un-bien,
  cold start went from 3.0 s to 87 ms.
- **Generation ledger.** Background generation backs off from inputs that
  already failed: 1 h, 4 h, 16 h, about 2.7 days, then every 7 days.
  `[generate] backlog_budget` (default 50) caps describes per daemon start.

### Changed

- `[generate] timeout` is passed to the llmkit provider, so a hung endpoint
  is abandoned.
- A structured generation call that returns no data counts as a failure.
- crib never forks: subprocesses go through `posix_spawn` (`crib.proc`).
  Forking a process that has loaded macOS's Network.framework can crash the
  child.
- crib's DuckDB connections keep extensions and secrets under crib's own
  directory, never `~/.duckdb`.
- Requires `polyllmkit>=0.2.2`.

### Fixed

- Malformed `lsp.json` or `grammar.json` files print a warning instead of
  being silently ignored.

## [0.21.6] - 2026-11-08

### Changed

- Shared chroma mode is refused only for an off-box bind; loopback is allowed.

## [0.21.5] - 2026-11-08

### Added

- Per-set read-only tool exposure, for example `--tools notes:ro`: the
  mutating verbs are never registered.

### Security

- Shared chroma mode is refused until `[chroma].allow_unpatched_remote` is
  set, guarding the unpatched server CVE-2026-45829.

## [0.21.4] - 2026-11-04

### Fixed

- A finished `project_index` sweep keeps its final counts in `project_status`.

## [0.21.3] - 2026-11-04

### Added

- Tool-set exposure: `crib serve --tools` and `[tools] expose` choose the
  MCP surface per deployment.
- `project_status` carries a `jobs` section.

### Changed

- `project_index` returns at once and its sweep runs in the daemon; poll
  `project_status` instead of blocking.
- `project_relocate` runs its heavy half in the daemon, so a client
  disconnect can't cancel it. It also completes a move-and-rename: mount
  rename plus citation rewrite.

## [0.21.1] - 2026-11-03

### Added

- `project_relocate`: re-home a project at its moved repo, keeping its
  identity.

## [0.21.0] - 2026-11-03

### Changed

- The directive names `project=` on every read.
- One funnel anchors every session (the era gate); sync reads ask when a
  session has no anchor.
- 2026-era clients are served directly; a stateless refusal says what to do.
