# Changelog

All notable changes to cribsheet are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Dev releases
(`X.Y.Z-devN`) are cut from the `dev` branch.

This file ships in the Pi package, where Pi shows the entries newer than the
version you last ran. `scripts/bump-version.sh` refuses a release whose
version has no heading here.

## [Unreleased]

### Fixed

- Bundles: `bundle create` says when `--expires` was lowered to the
  `--max-retention` cap (`expires_capped`); a registry entry from 0.28.0-dev1
  (no cap recorded) is capped at its creation plus 30 days; the sweep reports
  a bundle it can't delete once, not on every pass.
- CLI file links resolve context projects' items (the context dir applied);
  the "no project named" error names the context dir too.

## [0.28.0-dev2] - 2026-11-23

Security review of the 0.28.0-dev1 sandbox features (bundles, confined mode,
the writable allowlist). Read docs/bundles.md for the guarantees and what the
sandbox itself must provide.

### Fixed

- **Confined mode wrote into, and read, the checkout under review.**
  `project_create --store`, `adopt`, `release` and `relocate` are refused
  under `--root`; a checkout's `.crib` files are never read (a planted nested
  `.crib` could hide files from the index, and `refs:`/`store:` were
  honoured); files whose real path escapes the checkout are skipped; every
  writing verb checks both the allowlist and the root.
- **Bundles carried the trusted repo path**, which a sandbox revalidated
  against (dropping a context project's records, or reading trusted code).
  It is no longer copied, and read-only projects are never revalidated,
  re-described or watched.
- **A hostile bundle could hang or redirect the trusted side.** Manifests are
  read as small regular files only (no symlink, no FIFO); `bundle expire`
  takes the overlay from the registry and never writes through a symlink; the
  bundle sweep runs off the daemon's loop and one bad bundle never blocks the
  rest; a failed `bundle create` cleans up.
- **The transient sweep trusted a synced stub.** Only a transient project this
  machine created (its birth `.gitignore` present, its host this host) is
  deleted on schedule or without trash.
- **Environment leaks in confined mode:** the store moves only by `--store`
  (`$CRIB_STORE`; an inherited `$CRIB_INDEX_DIR` is ignored),
  `$CRIB_FACETS_FILE` is ignored, `$ROOT` can't be overridden, and
  `python -m crib` applies `--root` before loading. `--writable` is refused
  when a verb would run in the daemon (which ignores it).
- **The phased re-index** now updates only the edges touching re-extracted
  files, from the language server's data (same-named symbols no longer gain
  each other's callers; callers only the server knew survive);
  `--budget 0` means no time, not unbounded.
- Expiry parsing gives a clear error for out-of-range values;
  `transient: "false"` no longer reads as true.
- **Second review round:** trusted-side `bundle expire` reads and rewrites
  the overlay's stub as it does the manifest (a small regular file, replaced
  atomically, so a planted symlink at the stub or a temp name is never
  written through); a failed `bundle create` removes only what it made (a
  `--context-to` given without `--context` is never touched); a bundle's life
  is capped by `--max-retention` (default 30d), held in the trusted registry;
  global flags can't be abbreviated (`--ro` would have run unconfined), and
  `cli.main` refuses env flags it can't apply; `lsp.json`/`grammar.json` come
  from the root in confined mode; `--store` is refused when a verb would run
  in the daemon; the phased re-index treats a file emptied of symbols as
  changed.
- **Third review round:** the global `--root`/`--store`/`--writable` no
  longer collide with a subcommand's own option (`project create NAME --store
  REL` works again); the trusted `bundle expire` writes only the manifest and
  the registry, and moves the retention cap with the operator's expiry (the
  overlay's stub, in the sandbox's tree, is never touched from the trusted
  side); bundles copy `grammar.json` too.
- `lsp.json` and `grammar.json` are read from crib's config dir as resolved
  everywhere else, so `$XDG_CONFIG_HOME` now applies to them: with it set,
  move them from `~/.config/crib` to `$XDG_CONFIG_HOME/crib`.
- **Read-only context projects live whole in their own dir:** a bundle's
  context projects (stub, notes, records) go in `context_dir` (config; the
  `--context-mount` / `--context-to` path), a read-only second projects dir;
  nothing of them is left in the writable root. A project there is read-only
  whatever `writable` says and can't be shadowed by the root. Replaces the
  dev1 layout (stubs with `$CONTEXT/<p>` in the root): rebuild dev1 bundles.
  Enrichment (keywords, summaries, their catch-up) and the in-situ docs
  re-sweep skip read-only projects, so a read-only mount is never written.

- **Confined mode in a sandbox.** CLI verbs under `--root` now run
  in-process: they tried to attach to a warm daemon (needing `sharedserver`),
  which a sandbox doesn't have. A bundle's config also says
  `[daemon] enabled = false`.
- **`--root`, `--store` and `--writable` take effect before crib loads.** The
  facet registry is read at import, so `--root` used to leave it read from the
  default config dir (outside the root). The `crib` command now applies these
  global flags first (`crib.entry`).

## [0.28.0-dev1] - 2026-11-22

Dev release: crib for sandboxed review. Bundles (`crib bundle`), confined
mode (`crib --root`), per-project read-only overrides (`--writable`),
transient projects, and a phased re-index.

### Added

- **Per-project read-only overrides.** A crib instance can be told which
  projects it may write: `writable = ["p", ...]` in `config.toml`,
  `$CRIB_WRITABLE` (comma-separated; wins over the file), or the global
  `--writable PROJECT` flag (repeatable; for `serve`/`--mcp` and `--no-daemon`
  verbs). Every other project is read-only for that instance: reads work,
  writes are refused with the reason. Enforced at every write path: notes,
  declared facets, decisions, plans, learnings, code indexing, imports and the
  project lifecycle verbs. Unset, every project is writable, as before. The
  first piece of overlays for sandboxed review (an agent handed several
  projects writes only to its copy).
- **Transient projects.** `crib project create NAME --transient [--expires
  2h]` (MCP `project_create(transient=True, expires=...)`) makes a project for
  one job: never git-synced (its directory carries a `*` .gitignore), no
  version ring, skipped by the startup reconcile, and deleted without `.trash`.
  With `--expires` (an ISO time, or `30m` / `2h` / `1d`) the daemon deletes it
  when due, through the ordinary delete path (a scheduled delete, checked at
  start and every 5 minutes). A transient project can't declare repo residency.
- **Projects homed outside the store, including read-only ones.** A
  project whose data tier lives at any absolute path (its stub's
  `store_root`), such as a container's read-only mount of a context project,
  now indexes and reads without writing there: a reconcile no longer creates
  pillar directories, and a project this instance holds read-only is indexed
  like a read-only location (a note without an `id` is reported as
  `unstamped`, not stamped).
- **Confined mode: `crib --root DIR` (or `$CRIB_ROOT`).** Everything crib
  keeps (config, store, index, notes) lives under DIR, and nothing is written
  outside it. Only the embedded store runs (the shared server is refused), git
  memory sync and history are refused, watchers watch only inside DIR (no code
  watcher on a checkout, no harness-memory mirror), and a checkout's own
  `.crib` is never read or written: projects are named, and a trusted `.crib`
  can be placed in the project's directory under DIR. A reopened root rebuilds
  its store from its files, its transient projects included. Built for
  sandboxed review: crib runs against a disposable root.
  `--store DIR` (or `$CRIB_INDEX_DIR`) puts the store, a disposable cache,
  elsewhere (e.g. pod scratch); in confined mode it is the one thing kept
  outside the root.
- **Bundles: `crib bundle create|expire|list|delete`.** A bundle is a
  self-contained, disposable crib root for a sandbox: `crib bundle create DIR
  --overlay toucan:pr-123 --context llmkit --expires 7d` copies project
  `toucan` as the writable, transient project `toucan@pr-123`, copies
  `llmkit` read-only for context (to `DIR/context`, or `--context-to` for a
  read-only mount), and writes the bundle's config (embedded store, only the
  overlay writable) and a manifest. Open it with `crib --root DIR`. The
  trusted side registers each bundle and deletes it when its expiry passes
  (the daemon's sweep) or on `crib bundle delete`; a session can extend it
  with `crib bundle expire`. Only the expiry is read back from a bundle (it
  is writable inside the sandbox); what a delete removes comes from the
  registry. Project names may now contain one `@` (an overlay of a base).
  `create` also builds the bundle's own store (in `DIR/index`) and writes
  its vectors to `DIR/data/embed-cache.parquet`, embedding through the main
  store's cache so only never-seen text is computed; a sandbox that keeps
  its store elsewhere (`--store`) loads the file and re-embeds nothing.
  `--no-vectors` skips this. The bundle's config records the embedding model.
- **Phased re-index: `crib project index --phased` (MCP `phased=True`), on by
  default with `--root`.** The work runs in phases under the one `--budget`,
  most useful first: changed files' structure; then edges re-resolved (every
  record's callers rebuilt from every record's calls, so a caller in an
  unchanged file links to a changed function), with `dangling` listing calls
  whose target is gone (removed or renamed, still called); then
  descriptions. Records of deleted files are dropped. `phases` reports what
  each phase left unfinished.

### Fixed

- `crib project index --budget` was parsed but never passed on, so the CLI's
  budget was silently ignored.

## [0.27.0] - 2026-11-22

**Breaking for agents:** `project_use` and `project_current` are gone; name the
project (`project=` or `project_path=`) on every call. The updated plugin
instructions say so.

### Removed

- **`project_use` / `project_current` (and `crib project use` / `crib project
  current`) and the per-connection current project.** Every call names its
  project (`project=` or `project_path=`); nothing carries over between calls,
  on every protocol era. A first `project_path` call no longer adopts its repo,
  an answer to "which project?" applies to that call only, and creating a project
  no longer switches anything. A repo-scoped call (`project_index`,
  `project_status` ...) that names no project or repo is refused rather than
  answered from the default project. Design: "Retire use_project and implicit
  anchoring (all eras)".

### Added

- **Per-call asks on the 2026-07-28 protocol era.** A read naming no project,
  a write naming no target, and a destructive confirmation are now asked by
  returning the question (`InputRequiredResult`, answered on the client's
  retry) instead of being refused; the handshake era asks as before. The
  daemon still advertises only the handshake era; lifting that is a separate
  step.

- **List without a query, on every facet.** `note list` and `learning list`
  (MCP `note_list`, `learning_list`) join `design list`, `plan list` and
  declared facets' `list`: every item by title and path, with `-k` and
  `--tag`; a facet with declared fields can still filter by them.
- **`crib project items`** (MCP `project_items`): a project's items across
  every facet at once, each with its kind (and a decision's or plan's
  status); `--kind` and `--tag` narrow it.
- **`crib facets list`** is an alias of `crib facets show`.

### Changed

- **`created` in results is now `project_created`.** It always meant "this
  also created the project", never "this note is new", but agents read
  `created: false` as "the note already existed". In note (and note-like
  facet) add, move, import, import-memory, `project setup` and `project index`
  it appears only when true; `project create` always reports it. A
  learning's `created` (the learning note is new) is unchanged. The add
  tool's description now says that every add writes a new file.
- `<noun> list` output for declared facets is now one readable line per item
  (title, linked path, then tags and fields) instead of a raw key=value dump.

## [0.26.3] - 2026-11-21

### Fixed

- **Describe (and other structured generation) retries an unusable reply
  once, and says why a call failed.** A model that intermittently returns an
  empty reply, or JSON that doesn't fit the schema (`sections[0].keywords:
  expected array, got str`), now gets one more try after a short pause. The
  error reports llmkit's reason ("…: empty reply") instead of "exited 1", so
  `descriptions_error` explains itself. A call that fails before replying
  (auth, endpoint) is not retried. Rate limits reported by an
  openai-compatible provider now back off and retry as intended: llmkit
  returned them as a failed result rather than an exception, which crib's
  backoff never saw. Requires polyllmkit 0.2.5.

## [0.26.2] - 2026-11-21

### Changed

- **The rest of the CLI is styled on a colour terminal too:** write
  confirmations (note, decision and plan writes, moves, plan batches, with
  what they tainted or unblocked), `design tree`, `design check`,
  `design extract`, `code graph` (tree and edge views), `learning rehome`,
  `project list`, the project verbs (`index`, `status`, `doctor`, `related`
  ...), `ops` and `export`. Same roles and links as 0.26.1; piped output is
  unchanged.

## [0.26.1] - 2026-11-21

### Added

- **Styled CLI output on a colour terminal.** Query and list commands
  (lookup and apropos for every facet, `project lookup`, `code lookup`,
  `xref` and `dossier`, `design`/`plan` list and read, learnings, `status`)
  mark each part by what it is: ranks and metadata dim, names and titles
  bold, kinds magenta, statuses by value, warnings yellow, paths cyan. Paths
  are clickable in terminals that support links: code to its source file,
  notes, decisions, plans and learnings to theirs. Piped output and `--json`
  are unchanged byte for byte. `--color auto|always|never` overrides the
  detection, `NO_COLOR` turns it off, and `$CRIB_THEME_FILE`'s `[styles]`
  can restyle any role (`crib.path`, `crib.rank`, ...).

## [0.26.0] - 2026-11-21

### Added

- **Pick the generation profile per machine without editing config.**
  `$CRIB_GENERATE_PROFILE` overrides `[generate] profile` (and clears a
  `[generate] provider`, which would otherwise win silently);
  `$CRIB_GENERATE_PROVIDER` forces a provider. The daemon reads them from
  its own environment, so set them wherever it is launched. `crib generate
  test` and `crib status` say when a value came from the environment.
- **TypeScript 7 support.** `typescript-go` (`tsc --lsp --stdio`) is now
  the first built-in TypeScript/JavaScript language server. TypeScript 7
  ships no `tsserver.js`, so `typescript-language-server` returned no
  symbols wherever a global TypeScript 7 was all it could find. Server
  specs gain `min_version` (and `version_args`): a `tsc` older than 7 has
  no `--lsp`, so it falls through to `typescript-language-server` as
  before.
- **Empty extracts are reported.** A non-empty code file whose language
  server returns no symbols is retried once more slowly, then recorded:
  the index sweep lists it, and `crib project doctor` reports "no symbols
  from the language server". Its existing symbols are kept, and the record
  clears when the file yields symbols again.

### Fixed

- **`code_lookup` finds symbols that have no description yet** by their
  name. A symbol without a description vector used to score the lowest
  possible cosine, so even an exact name match ranked below every described
  symbol. It now ranks on its name match alone until its description
  arrives; described symbols score exactly as before.

### Known issues

- `typescript-go` reports import bindings (`import { X } from ...`) as
  symbols, so an imported name can be ambiguous in `code_dossier`; name the
  file to pick the definition.

## [0.25.0] - 2026-11-17

Everything since 0.23.1, collected from the 0.24.0 and 0.25.0 dev releases
below. **Requires Python 3.14.**

### Highlights

- **Custom facets.** Declare your own kinds of item in a system facets file
  (`~/.config/crib/facets.toml`; `crib facets init` writes a commented
  starter, `docs/facets.md` has the full spec reference). Each gets the verbs
  of its aspects as MCP tools and CLI commands (`routine_add`, `crib routine
  lookup`, ...), its own store, search scope and tool set. Three kinds:
  note-like (optionally enriched with summaries and keywords), anchored to
  code like learnings (shown on `code_dossier`), and graph facets like
  decisions and plans, with their own statuses (each playing a role: open,
  claimed, gated, paused, dropped, done, retired), transitions, link
  defaults and working set, and their own names for its bands
  (`[<noun>.bands]`).
- **Typed fields you can query.** `int`, `str` and `date` fields are
  parameters on add/edit and filters on lookup and `list` (equality, or
  `where` with comparisons and `re`). Declared fields are required unless the
  spec lists them under `optional`; `add` refuses a new item without one.
- **Agents learn your facets.** A facet's `about` and `[<noun>.when]` wording
  join crib's own in its tool descriptions, and each session's instructions
  list this machine's declared facets (the MCP server and the Claude Code,
  OpenCode and Pi plugins add them). Engine hints and the `extract`
  procedure name the facet's own tools, statuses and abilities.
- **Facets mapped from a repo, and storage per pillar.** A repo's `.crib`
  `facets:` maps a facet onto a directory of its own, writable or read-only,
  ids stamped or derived from the path (a read-only, derived mapping never
  writes a byte; `docs:` is now the notes facet mapped that way). A repo can
  keep one pillar, say its decisions, in the repo while the rest stay where
  they are: `crib project adopt` moves the items (merging when nothing would
  be overwritten, refusing before moving anything), `release` moves them back.
  `title_from` names items without a title (heading, filename, any key), and
  `crib project doctor` flags a missing required field, a value that doesn't
  fit its type, and an untitled item.
- **Explicit conformance.** A new or changed facet waits for `crib facets
  sync --apply` (a plan first); a removed one is dropped only with `--drop`;
  sync refuses while the facets file fails to load and, on a shared store,
  without `--shared`.
- **Releases test themselves.** Every release tag runs the full suite once
  on the tagged commit; PyPI and the plugins publish only if it passed.

### Changed

- **Python 3.14 or newer** (was 3.13).
- crib's own notes, decisions, plans and learnings are specs in the same
  registry, their tools and commands generated from the same templates; their
  names, parameters, descriptions and `--help` are unchanged (held to the byte
  by a snapshot test), apart from the subcommand order in `crib note -h`,
  `crib design -h` and `crib plan -h`.
- `read`/`locate` find an item named without its suffix and report a missing
  one with suggestions; `crib project index` reports each facet mapping.

### Fixed

- Declared graph facets work in-process (`--no-daemon`), refuse while waiting
  for `crib facets sync`, and their `review_apply` and `crib project doctor`
  act on their own items.

### Upgrading

- **On Python 3.13**, pip and uv keep you on 0.23.1; `uvx` and the plugins
  fetch a 3.14 for crib on their own.
- **Otherwise nothing to do:** crib's own facets and the store layout are
  unchanged, so the first start rebuilds nothing. A facet you declare waits
  for `crib facets sync --apply`, then a daemon restart.
- **If you tried the dev releases:** remove an `extract = ...` line from a
  facet spec (it is derived now, and a file that sets it is refused, leaving
  only the built-ins), and list in `optional = [...]` any declared field your
  items may leave out.

## [0.25.0-dev3] - 2026-11-16

### Added

- **Band names per facet.** A progress facet's `[<noun>.bands]` names the
  groups its `list` shows (`claimed = "investigating"`); unnamed bands keep
  the plan's names, so plan output is unchanged.
- **Flags are a defined registry** (`crib.facets.FLAGS`: type, needed
  aspects, meaning), checked evenly; `crib facets show` lists each facet's.

### Changed

- **The `extract` procedure is built from what the facet can do**: proposed
  entries only with a gated status, one batch only when ordered (else one
  add per item), and only tools the facet has. The `extract` spec key is
  gone: a facets file that sets it is refused (remove the line).
- A declared facet now gets `similar` on its writes, `settled` verdicts and
  stale markers, read from roles instead of crib's own status names.

### Fixed

- Every CLI verb on a declared graph facet works with `--no-daemon` (it
  raised `AttributeError`), and a graph facet waiting for `crib facets sync`
  refuses with "isn't ready" over MCP too (it listed as empty).
- A progress facet that reports staleness lists correctly (`tainted` was
  passed as `all`).

### Internal

- One `graph_*` operation per graph verb (crib's own `design_*`/`plan_*`
  names are aliases); the built-ins' wording is one file
  (`crib/wording.toml`); CLI verb orders derive from the verb catalogue;
  `facets.parse` is split into helpers.

## [0.25.0-dev2] - 2026-11-15

### Changed

- **A declared facet's fields are required unless its spec lists them under
  `optional`.** `add` refuses a new item without a required field, and
  `crib project doctor` names files missing one; `edit` still works on an
  existing item that lacks one. If you declared fields before, list the ones
  items may leave out: `optional = ["timeout"]`.

### Fixed

- `crib project index` / `wait` say "starting (counting files)" before a
  sweep has counted its files, instead of `None/None`.

## [0.25.0-dev1] - 2026-11-15

The facet registry and everything since 0.23.1, as a new minor line: the
0.24.0 dev releases below describe each change. Requires Python 3.14.

## [0.24.0-dev4] - 2026-11-15

### Fixed

- `crib project index` reports each facet mapping and its indexed item count
  on every answer: while its code sweep was still running daemon-side, it
  printed `indexed 0/0 files` and nothing about the facets. A running sweep
  now prints as "indexing in progress: done/total".

## [0.24.0-dev3] - 2026-11-15

### Added

- **`title_from`** in a facet spec: where an item's title comes from, tried
  in order: its frontmatter `title`, its first `heading`, its `filename`, or
  any frontmatter key. Default: the title alone (unchanged). An item with no
  title is listed by `crib project doctor`.
- **Listing by tag:** a declared facet's `list` shows each item's tags (its
  own plus its `type`) and takes `tags=` / `--tag`.
- `crib project setup` and `index` report each facet mapping: where, how,
  and how many items it has indexed.
- `crib project doctor` names a declared field value that doesn't fit its
  type (`timeout: 30s` on an int field).
- `docs/facets.md`: a complete spec reference, and the frontmatter keys crib
  reads in a mapped file.

### Fixed

- `read` and `locate` find an item named without its suffix
  (`morning-briefing` for `morning-briefing.md`); a missing item is a clean
  error with suggestions instead of a raw "No such file".
- Under `ids: derived` (and for `docs:`), the id is always the path's: a
  file's own `id:` key (another tool's) no longer decides crib's identity,
  where two files sharing one used to merge.

## [0.24.0-dev2] - 2026-11-15

### Fixed

- Session start can no longer wait on a download: the Claude Code hook asks
  for this machine's declared-facets section only when a facets file exists,
  only from a `crib` it can run directly (never the plugin's `uvx`
  fallback), and gives up after 5 seconds. The OpenCode and Pi plugins ask
  at most once per process, and not at all without a facets file (0.24.0-dev1
  ran `crib --version` on every turn).
- `learning migrate`, new learnings and `import` name files to match a
  mapping's glob suffix (`*.lrn.md`, `**/*.rt.md`).
- A mapping repointed in `.crib` is followed only once every item has moved
  to the new home (one moved file used to be enough).
- A move that fails part-way across filesystems leaves no partial copy, and
  says so if anything couldn't be put back.
- While a facet waits for `crib project adopt`, items already committed into
  its mapping (a teammate's) take part in dependencies and gating.
- `crib facets sync` names a moved store's leftover files in repo-adopted
  projects too.
- Release workflows: a plugin release reads only this tag's release run, and
  stops at once if that run ends without reaching its build.

## [0.24.0-dev1] - 2026-11-15

### Highlights

- **Custom facets.** Declare your own kinds of item in a system facets file
  (`~/.config/crib/facets.toml`; `crib facets init` writes a commented
  starter). A facet is a spec: a noun, a plural, the aspects it has, and its
  own typed fields. It gets the verbs of its aspects as MCP tools and CLI
  commands (`routine_add`, `crib routine lookup`, ...), its own store, search
  scope and tool set (`--tools routines:ro`). Its `about` (what an item is,
  when to use the family) and `[<noun>.when]` wording join crib's own in its
  tool descriptions, so agents know when to reach for it, and each session's
  instructions list this machine's declared facets (`crib facets
  instructions`; the MCP server and the Claude Code, OpenCode and Pi plugins
  add it). See `docs/facets.md`.
- **Three kinds of facet.** Note-like (core, optionally enriched with
  summaries and keywords), anchored to code like learnings (for example a
  review agent's findings, shown on `code_dossier` beside learnings), and
  graph facets like decisions and plans, with their own statuses (each
  playing an engine role: open, claimed, gated, paused, dropped, done,
  retired), transitions, link defaults and working set (`next`).
- **Typed fields you can query.** A declared facet's fields (`int`, `str`,
  `date`) are parameters on add/edit, and filters on lookup and the new
  `list`: equality, or `where` with `== != < <= > >=` (int, date) and
  `== != re` (str). `--where 'severity>=3'` on the CLI.
- **Facets mapped from a repo.** A repo's `.crib` `facets:` maps a facet onto
  a directory of its own, writable or read-only, with ids stamped into the
  files or derived from their paths (a read-only, derived mapping never
  writes a byte). `docs:` is now the notes facet mapped read-only.
- **Storage per pillar.** A repo can keep, say, its decisions in the repo
  (`facets: {design: {dir: decisions}}`) while notes stay where they are;
  `crib project adopt` moves the existing items, `release` moves them back.
  Adopt merges into a directory that already holds items (a teammate's, or an
  interrupted adopt's) when no file would be overwritten, refuses before
  moving anything, and never moves a read-only mapping's files.
- **Explicit conformance.** A new or changed facet definition waits for
  `crib facets sync --apply` (which shows what it will do first); nothing
  converts the store behind your back. A facet you remove is dropped only
  with `--drop`; sync refuses while the facets file fails to load, and on a
  shared store needs `--shared`. `crib facets show` lists every facet and its
  state; `--toml` exports the registry.
- **Releases test themselves.** Every release tag runs the full suite once, on
  the tagged commit; the PyPI and plugin releases each publish only if it
  passed.

### Changed

- **Python 3.14 or newer** (was 3.13).
- crib's own pillars (notes, decisions, plans, learnings) are now specs in
  the same registry, and their tools and commands are generated from the
  same templates. Their names, parameters, descriptions and `--help` are
  unchanged (held to the byte by a snapshot test).
- `crib note -h` lists `import-memory` first.
- `crib design -h` and `crib plan -h` list their subcommands in the order the
  registry declares them.

### Upgrading

- **Python 3.14 or newer is now required** (3.13 was the floor). On 3.13,
  pip and uv keep you on 0.23.1; `uvx` and the plugins fetch a 3.14 for crib
  on their own.
- Otherwise nothing to do: built-in facets and the store layout are
  unchanged, so the first start rebuilds nothing. A facet you declare waits for
  `crib facets sync --apply`, then a daemon restart.

## [0.23.1] - 2026-11-13

Everything since 0.22.1, collected from the 0.23 dev releases below.

### Highlights

- **One shape for the cache.** Notes, decisions, plans, learnings, code
  symbols and the keyword/summary indexes are all rows in the DuckDB store,
  kept exactly in line with your files on every read and write. Your files
  stay the truth. Code search no longer holds every project in memory
  (crib's own memory is about 110 MB with every project warm), decision and
  plan reads are about 6x faster, and the startup reconcile reads only the
  notes that changed (about 9x faster).
- **New verbs:** `crib project doctor` (what needs attention, with the fix
  for each), `crib project related <ref>` (an item's links both ways and its
  nearest items of every kind), `crib project lookup` (one search across
  notes, decisions, plans and learnings), and `crib generate setup` /
  `crib generate test` (configure and check LLM generation).
- **Writes warn usefully.** After a note, decision or plan write, `similar`
  says whether a match is a duplicate, a confusingly similar title, already
  settled or just related, and what to do. Reads suggest the decisions an
  item probably builds on; imports name the existing items each section is
  closest to; orphaned learnings name where they probably went.
- **Dependency kinds:** `follows`, `rests_on`, `informed_by`, with defaults
  from the target; `dep_add` with another kind changes it in place.
- **Generation modes:** `[generate] mode = "on" | "explicit" | "off"`.
  Without a models config it is `explicit`: crib never drives the Claude CLI
  in the background on its own.
- **The daemon explains itself.** `crib status` shows uptime, startup steps,
  running background work, queues, file-watching health and the generation
  mode; the daemon writes `~/.cache/crib/daemon.log`.

### Fixed

- Opening the store can no longer spin, a store file DuckDB can't read is set
  aside and rebuilt, and a schema change keeps the embedding cache.
- A hung LLM provider can't wedge the daemon: generation has its own pool and
  deadline, and code queries never wait on it. The Claude CLI provider starts
  with no MCP servers and is killed promptly on a timeout (polyllmkit 0.2.4,
  now required).
- `status` no longer shows a git remote's password or token.
- Projects that vendor the same file no longer overwrite each other's symbols.

### Upgrading

- The first daemon on 0.23.1 rebuilds its DuckDB cache from your files once;
  nothing in your notes, decisions, plans or symbol records changes.
- If you relied on background generation without a models config, run
  `crib generate setup` or set `[generate] mode = "on"`.
- With uv, upgrade with `uv pip install --upgrade --refresh cribsheet`
  (`--upgrade` moves an installed package; `--refresh` skips a stale index).

## [0.23.1-dev2] - 2026-11-13

### Fixed

- **Opening the store can no longer spin.** An open of a store already at
  this version writes nothing, so it can't conflict with other connections.
  A conversion (a new file or a version change) runs once, under a lock
  that every crib process on the machine shares, as one transaction; others
  wait and find it done. If other writes keep conflicting, crib waits up to
  two minutes and then says what to do, instead of retrying silently.
- **A store file DuckDB can't read is set aside and rebuilt**, kept as
  `crib.duckdb.broken-<time>` (it is a cache rebuilt from your files).
- **A schema change keeps the embedding cache**, so a conversion no longer
  re-embeds every note and symbol description.
- **A hung LLM provider can't wedge the daemon.** Generation runs on its own
  small pool with crib's own deadline (165 s by default); past it the call is
  abandoned and the description waits, and tool calls never queue behind it.
  A code query that finds changed files queues their descriptions instead of
  waiting for them. Each generation call shows in `status` while it runs.
- With the Claude CLI as the provider, it starts with no MCP servers, and a
  timed-out call kills it even when it hangs at startup (polyllmkit 0.2.3,
  now required).
- The daemon's log starts before the store opens, so a hang there leaves a
  trace.

### Changed

- `crib status` prints the daemon's background work: uptime and log file,
  each running job and how long it has run, queued work, file watching when
  it isn't healthy, and the generation mode with the descriptions waiting.

## [0.23.1-dev1] - 2026-11-13

No changes since 0.23.0-dev4: the same code with the patch version bumped, so
an installer that won't move between dev releases of one version sees a newer
base version.

## [0.23.0-dev4] - 2026-11-13

No changes since 0.23.0-dev3: the same code under a new version number, to
check that `uv pip install --upgrade --prerelease=allow cribsheet` picks up a
new dev release.

## [0.23.0-dev3] - 2026-11-13

### Added

- `crib generate setup`: finds the LLM providers this machine has (API keys
  in the environment, a local Ollama or llama-swap, the `claude` CLI,
  providers already defined for zsh-ai), asks which to use and which model,
  and writes `~/.config/crib/models.toml` and the `[generate]` section of
  `config.toml` after showing the diff. Both files are backed up first; a
  symlinked config is edited where it lives. Ollama Cloud gets
  `structured = "tool"`, and providers get room for crib's bulk replies.
- `crib generate test`: one small call per purpose, reporting whether the key
  works and structured output comes back.
- The README quick start has a generation step, and
  `docs/generate.toml.example` shows current practice.

## [0.23.0-dev2] - 2026-11-13

### Added

- `crib project doctor` (`project_doctor`): what in a project needs
  attention, report-only, each finding naming the command that fixes it.
  It checks dangling dependencies; dependencies on a superseded decision
  (naming its replacement) or a declined plan; cited sections that changed
  or vanished; moved or orphaned learnings; one id in several files;
  near-duplicate notes, decisions and plans; and pending descriptions.
- `crib project related <ref>` (`project_related`): one item's links both
  ways and its nearest notes, decisions, plans, learnings and symbols.
- `crib project lookup` (`project_lookup`): one search across notes,
  decisions, plans and learnings, with `--kind` and `--status` filters.
- `[generate] mode = "on" | "explicit" | "off"`. Without a models
  config it is `explicit`: crib generates descriptions, keywords and
  summaries only when you run a verb that asks for them, and never drives
  the Claude CLI in the background on its own. Background indexing still
  saves code structure; `status` counts the descriptions waiting.
- `status` reports the daemon: uptime, startup milestones, the background
  work running now, queue depths, and whether file events are arriving.
  The daemon writes a log, by default `~/.cache/crib/daemon.log`.
- When file events are delayed (macOS fseventsd under load), the daemon
  says so and sweeps for changes every two minutes until they recover.

### Changed

- After a note, decision or plan write, `similar` compares the whole item
  with every note, decision and plan in the project, not just its first
  line against one pillar. Each match says whether it is a duplicate, a
  confusingly similar title, already settled (done, declined, parked,
  superseded) or just related, and what to do. Plan adds return it too.
- Decision and plan reads list `suggested_deps`: active decisions the item
  is close to but doesn't depend on, with the command to add the link.
- `design_import` and `plan_import` name, for each section, the existing
  decisions or plans closest to it.
- The learnings report names where an orphaned learning probably went.
- `dep_add` with a different kind changes an existing dependency's kind
  and keeps it verified.
- Summary aliases are embedded once and kept, instead of re-embedded after
  every daemon start; the keyword index is read from the store.

### Fixed

- `status` no longer shows a git remote's password or token.

### Upgrading

- The store's schema changes again, so the first daemon on this version
  rebuilds its cache from your files once.
- If you relied on background generation without a models config, set
  `[generate] mode = "on"`.

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
