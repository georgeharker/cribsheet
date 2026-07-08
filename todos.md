# todos — deferred / to-test

## Resolved
- **Sticky session project WAS combiner-global, not per-chat (2026-07-07).**
  Observed: an agent in the cribsheet repo had code queries resolve to `music-llm`.
  ROOT CAUSE (diagnosed by reading the combiner + svg-mcp): crib's per-connection
  `SessionState` is keyed on the MCP `ServerSession` object (copied verbatim from
  svg-mcp's `server.py` per-session document stores). That is correct ONLY when each
  chat gets its own upstream session. The combiner grants a per-chat upstream session
  (a distinct `Mcp-Session-Id` via FastMCP `StatefulProxyClient.new_stateful`) ONLY for
  HTTP/SSE upstreams with `isolate: true`. **svg-mcp sets `isolate: true`; cribsheet
  never did** → all chats collapsed onto one shared upstream session → one
  `SessionState.current_project` → cross-chat leak. The combiner does NOT forward a
  per-chat identity on non-isolated calls, so `isolate: true` (not sharing one session)
  is the only per-chat mechanism. FIX, two parts:
    1. **Config** (the leak): added `"isolate": true` to the cribsheet entry in
       `~/.dotfiles/.config/secrets/mcpservers.json`, matching svg-mcp. (Needs the
       generated `~/.cache/secrets/geohar.mcpservers.json` regenerated + a combiner/crib
       restart to take effect.)
    2. **Crib self-diagnosis** (defense-in-depth): `ProjectResolution` (`crib/session.py`)
       now carries HOW a call resolved (`explicit`/`path`/`session`/`seed`; `implicit` =
       session|seed). The read code tools echo it when resolution was implicit — free on
       dict results (dossier/graph gain a `resolved` key), and as a one-element diagnostic
       on an EMPTY list result (lookup/xref), the case a silently-wrong sticky project
       can't otherwise be told from "no matches". `current_project` also returns
       `resolved_via`. The CLI always sends `project_path` (via=`path`), so the echo is
       agent-only. Stickiness is KEPT — with isolation it's per-chat-correct; the echo is
       the safety net. Consolidates the `_project`/`_source_project`/`_write_project`
       helper policies (ledger #7).
- **`store.all()` per file at index time was O(N²).** `_index_project_code` now parses the
  prior index ONCE (`existing = {e["fqname"]: e ...}`, `crib/app.py`) and passes the
  by-fqname snapshot into every `_index_file_sync`/`_index_file_inner` call
  (`existing` param), so the content_hash gate + vanished-symbol drop no longer re-`store.all()`
  per file. Cold onboard is O(N), not O(files × symbols); the standalone single-file path
  (existing=None) still parses once for itself.
- **Warm LSP sessions (was: per-file cold spin-up).** `LspSessionPool` in
  `crib/codeindex.py`: one initialized client per (workspace root, server label),
  lazy-started, reused across `extract_file` calls — spawn + `initialize` (the
  whole-workspace index) is paid once per sweep instead of per file. Dead servers
  are `poll()`-detected and respawned (plus one wedged-server retry per extraction);
  idle sessions grace-reap on acquire; docs are didOpen/didClose'd per call so every
  extraction reads fresh disk and server doc-memory stays bounded. Warm calls settle
  0.3s instead of 1.5s. `Crib.close()`/atexit shut the pool down. Workspace-index
  freshness (docs §3.2): `_on_code_change` pumps the watcher's batches into every
  warm session for the root as `workspace/didChangeWatchedFiles` (capability
  advertised; `client/registerCapability` null-acked), covering servers that don't
  self-watch the fs.
- **Post-pull eager rebuild of merge-dirtied code files.** `reconcile_all` (already
  fired by the CLI after a changed pull) now runs `_reindex_dirty_code`: files with
  blank-hash symbols rebuild CONCURRENTLY ([generate].concurrency semaphore,
  best-effort per file) instead of serially inside the first code query;
  `_revalidate` stays as the lazy backstop for pulls done outside crib.
- **Docs indexed in-situ (source is master), not copied.** Was: `.crib` docs were
  *copied* into `imported/<repo>/` (a stale snapshot). Now a repo's `.crib` `docs:`
  globs are indexed IN-SITU as source-anchored notes (`sources/<repo>/<rel>`): crib
  holds only the index, `read`/`locate` return the repo path, the source watcher
  reindexes on save, and `project index` reconciles adds/edits/deletes. `import` is
  now manual-only and takes an explicit list of paths to copy INTO memory (a
  crib-owned snapshot you deliberately own). Two note classes, one index; both
  surface via `lookup`. Every note (incl. code learnings) exposes its on-disk `path`.
  (`SourceRoots` registry = `doc-sources.json`; `CribLink.doc_patterns` honours legacy
  `import:` as a fallback.)
- **Submodule/vendored code rooting.** Was: vendored sub-repos got mis-rooted (per-file
  `find_root` escaped into the submodule → `source_root` flip-flopped → `_revalidate`
  evicted the real symbols, collapsing the index). Fixed by `find_root` resolving to the
  top-level repo (a submodule's `.git` is a FILE, a real repo's is a DIRECTORY). Decision:
  vendored submodule code (e.g. `vendor/llmkit/…`) SHOULD be indexed as part of the
  parent — it's real code the parent uses — just correctly rooted. So NOT excluding
  vendor. (Note: the watcher only catches edits made *after* it starts; down-time edits
  fall to the lazy mtime gate on the next query.)

## Live-index staleness — investigate AFTER the store restructure
- **The live daemon index has drifted stale for some projects (surfaced 2026-07-07 by
  `scripts/snapshot_harness.py`).** The harness indexes a CLEAN checkout at a pinned SHA;
  comparing those goldens (`~/.cache/crib-goldens/`) to the LIVE daemon index shows golden
  (clean source) < live for two projects, with clean working trees at HEAD:
    - zsh-ai:        golden 241 vs live 387  (−146, big)
    - mcp-companion: golden 1216 vs live 1222 (−6, mild)
    - cribsheet, llmkit, svg-mcp, sharedserver, dotfiler, zdot: golden == live.
  The goldens are REPRODUCIBLE (idempotency floor is ∅; zsh-ai indexes to 241 twice), so this
  is LIVE STALENESS, not capture nondeterminism: the daemon holds symbols for files that
  changed/were removed but were never pruned. Suspected: a watcher/revalidation gap on
  deletions + an index left half-reconciled across a daemon reboot (the combiner-global
  session era + the `isolate:true` bounce). Investigations:
    - Reconcile/reindex the live daemon, re-compare vs goldens → should collapse to 0. If a
      project STILL drifts after a clean reconcile, that's a real PRUNE bug, not staleness.
    - Root-cause zsh-ai's +146: bucket the live-vs-golden symbol diff by file → the un-pruned
      files. Prime suspect: extensionless zsh autoloads — a DELETED one can't be content-sniffed
      (watcher §6), so it falls to the lazy mtime gate, which only fires on a query touching the
      project; a project rarely queried stays stale.
    - Confirm whether the reboot/grace-period path can leave an index un-reconciled (built before
      a bounce, never swept). If so, add a startup reconcile or a staleness self-check.
    - Keep a periodic "live vs SHA golden" drift check as a health signal — the harness already
      does exactly this (`compare` against `~/.cache/crib-goldens`).

## To test
- **ty as the Python indexer, end-to-end.** Just added (first-choice `.py` LSP; verified
  documentSymbol + callHierarchy + references + speed in isolation). Do a clean-index
  do-over of the cribsheet clean-room test with ty driving, and confirm the call graph +
  references match pyright's and it's faster. (Watch the daemon's PATH includes
  `~/.local/bin` so `ty` resolves — else it falls through to basedpyright.)
- **shuck call hierarchy.** Upstream PRs are out to add `callHierarchy` to shuck. When
  they land, zsh gets a real call graph automatically — crib gates on the
  `callHierarchyProvider` capability, so no code change needed; just re-index zdot and
  confirm `calls`/`called_by` populate (today references-only).

## LSP workspace knowledge (what the server considers "live")
- **`$/progress` readiness barrier.** The settle (1.5s fresh / 0.3s warm) is a GUESS
  at when the server has finished discovering/indexing its workspace; a cold server
  answering early yields under-resolved cross-file edges (and the edge patch then
  propagates the loss). Servers report indexing via `$/progress` (workDoneProgress:
  pyright, rust-analyzer, gopls) — track begin/end in `LspClient._reader` and wait
  for quiescence (with timeout) on FRESH sessions before the first edge query.
  Symbol listings are already safe (didOpen'd doc + empty/partial guards); this is
  about EDGES.
- **Workspace membership — didOpen pinning SHIPPED (`pinWorkspace` spec flag).**
  The server discovers sources by ITS config (pyright include/exclude, cargo
  targets, clangd compile db, shuck's scan); a file it never discovered is
  invisible to cross-file references. `didOpen` is the protocol's membership
  signal, so a sweep now PINS the full enumerated doc set open on servers whose
  spec sets `pinWorkspace` (shipped on shuck — zdot's extensionless autoloads are
  the live case), released at sweep end. Remaining: document per-language
  membership settings in `docs/lsp.json.example`; consider pinning on single-file
  watcher reindexes too (currently sweep-only).
- **Multi-root workspaceFolders for refs.** Advertising a ref's local root as a
  second workspace folder (servers that support it) would make references INTO ref
  projects visible — today the server only searches its own root, so cross-project
  `references`/`called_by` edges are one-directional (outgoing calls resolve;
  inbound references don't). Phase-3-adjacent.

## Live-update (mtime gate shipped; these are the follow-ons)
- **Source watcher — eager revalidation (Phase 2).** Query-time revalidation is in; a
  watcher over `.crib` `paths:` would reindex on save proactively so the first
  post-edit query isn't the one that pays the reindex.

## Resolved (recent)
- **Cross-project refs (phases 1+2) — SHIPPED.** `.crib refs: [llmkit]`: query-time
  fan-out (lookup merge, dossier/xref/graph fall-through + cross-edge traversal) and
  index-time attribution (qualified `name [proj:rel]` edges via local ref root,
  in-tree nested-`.crib` checkout, or site-packages suffix match — keyed by
  name+file, sidestepping the path-derived fqname wrinkle). Nested `.crib`s bound
  the parent's enumeration (vendored code belongs to its project). Remaining from
  the design below: phase 3 (inbound reverse xref via a who-refs-whom registry) and
  the refs-you-don't-own extensions (version-tagged dep indexes, shareable indexes).
- **Watcher spurious-delete wipe (199 cribsheet + 255 mcp-companion tomls,
  2026-07-07).** FSEvents coalesces rename-style saves into flag bundles; watchdog
  re-expands them in arbitrary order and the code watcher's last-event-wins batch
  could land `deleted=True` for a LIVE file → `_drop_file` evicted the whole file's
  symbols (invisible to `_revalidate` afterwards — no tomls, no baseline entry; only
  an enumeration sweep recovers). Fixed: existence re-verified at decode AND at
  dispatch (post-debounce, authoritative); recovery = `crib project index`.

## Parked features
- **Cross-project refs — `.crib refs: [llmkit]`, xref against a named external project.**
  Motivation: de-vendor llmkit (packaging refers to its git) without losing the unified
  crib↔llmkit call graph — which today exists only because the editable install points
  into `vendor/`, keeping LSP resolutions under the one root (`_index_project_code` is
  single-root: `relative_to(root)`, out-of-root edges are dropped). A `refs:` list of
  crib project names would decouple xref from vendoring, in three independent phases:
  1. *Query-time fan-out* (cheap, read-only): `code_lookup`/`dossier`/`xref`/`graph`
     also resolve names against ref'd projects' symbol indexes, results project-tagged;
     an unindexed ref self-diagnoses ("run project setup in <its root>").
  2. *Index-time edge attribution*: when the LSP resolves a def/ref to a file OUTSIDE
     the root, attribute it to a ref'd project (by that project's recorded root, or by
     module-name match for site-packages installs) and store a project-qualified edge
     (`llmkit:<fqname>`) instead of dropping it. Wrinkle: fqnames are path-derived, so
     the same symbol is `vendor.llmkit.src.llmkit.*` under the parent but `src.llmkit.*`
     in its own project — needs import-name normalization (or a root-relative mapping)
     before qualified edges can resolve.
  3. *Inbound reverse xref*: llmkit's `code_xref` showing crib callers, WITHOUT
     cross-project writes — record each project's `refs` at index time (who-refs-whom
     registry), answer inbound queries by scanning declared referrers' outbound edges.
  Portability: resolve ref targets by project NAME (their own .crib/source-roots), never
  absolute paths. Supersedes the "vendored submodule code SHOULD be indexed as part of
  the parent" decision above once shipped — vendoring becomes purely a packaging choice.
  *Also covers refs you DON'T own*: index a dependency's source (its site-packages copy,
  or a pinned upstream clone) as its own project — `refs: [chromadb, fastmcp]` — and get
  dossier/lookup into third-party internals, inbound xref as dependency-impact analysis
  ("who in MY code calls chromadb.PersistentClient" before an upgrade), and learnings
  pinned to upstream symbols (where gotcha-notes are most valuable; `code_rehome` already
  handles symbols moving between versions). Read-only fits naturally — refs never write
  into the target. Follow-ons this opens: tag a dep's index with its version/commit and
  self-diagnose installed-vs-indexed mismatch; the portable symbol_index format means
  pre-built indexes for popular libs could be shared/shipped rather than rebuilt.
- **zsh cross-file references + autoload indexing.** shuck can't statically resolve
  dynamically-sourced zsh, and function definers live in extension-less autoload files.
  A name-based cross-file reference layer (+ index the autoload files via shebang) would
  make zsh refs complete. Deferred behind the "pause zsh" call. (2026-07-07: autoload
  files now didOpen-PINNED during sweeps — 566→604 zdot symbols — but zdot→dotfiler
  cross-project EDGES still don't materialize: shuck neither resolves dynamically-
  sourced calls nor searches the extra multi-root workspaceFolder. Query-time refs
  fan-out is the working zsh cross-project path; edges need this upstream work.)
- **git-history-driven `code_rehome`.** Ranking rehome candidates via `git log --follow`
  + usage pointers — a prompt pattern (the agent reads history, calls `code_rehome`),
  not hardwired ranking.
- **Feed learnings into the describe prompt.** Would let regenerated descriptions respect
  human corrections — but leaks human truth into the regenerable cache. Kept parked.

## Surface shape (open questions, see docs/surface.md)
- `code` vs `project` facet overlap (own verbs vs `project --no-docs`).
- Two `forget`s: `code forget <sym>` (a learning) vs `project forget` (the index).
- Notes verbs top-level vs code verbs namespaced — a `crib notes <verb>` for symmetry?
