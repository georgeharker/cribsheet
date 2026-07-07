# todos — deferred / to-test

## To fix (perf)
- **`store.all()` per file at index time is O(N²).** `_index_file_sync` re-parses the
  ENTIRE symbol_index (`existing = store.all()`) on every file, for the content_hash
  gate + vanished-symbol drop. As the index grows (cribsheet+llmkit ≈ 1000+ symbols)
  each file re-parses the whole thing, so a cold `project_index` scales quadratically and
  the parallel-describe win gets eaten. Fix: snapshot `store.all()` ONCE before the sweep
  (or reuse the resident cache) and pass the by-fqname map to each file. Correct today,
  just wasteful — concurrent full-parses also contend.

## Resolved
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

## Live-update (mtime gate shipped; these are the follow-ons)
- **Source watcher — eager revalidation (Phase 2).** Query-time revalidation is in; a
  watcher over `.crib` `paths:` would reindex on save proactively so the first
  post-edit query isn't the one that pays the reindex.

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
  make zsh refs complete. Deferred behind the "pause zsh" call.
- **git-history-driven `code_rehome`.** Ranking rehome candidates via `git log --follow`
  + usage pointers — a prompt pattern (the agent reads history, calls `code_rehome`),
  not hardwired ranking.
- **Feed learnings into the describe prompt.** Would let regenerated descriptions respect
  human corrections — but leaks human truth into the regenerable cache. Kept parked.

## Surface shape (open questions, see docs/surface.md)
- `code` vs `project` facet overlap (own verbs vs `project --no-docs`).
- Two `forget`s: `code forget <sym>` (a learning) vs `project forget` (the index).
- Notes verbs top-level vs code verbs namespaced — a `crib notes <verb>` for symmetry?
