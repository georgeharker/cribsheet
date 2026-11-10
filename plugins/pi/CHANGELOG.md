# Changelog

All notable changes to cribsheet are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Dev releases
(`X.Y.Z-devN`) are cut from the `dev` branch.

This file ships in the Pi package, where Pi shows the entries newer than the
version you last ran. `scripts/bump-version.sh` refuses a release whose
version has no heading here.

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
  DuckDB), and leftover chroma-era code paths.

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
