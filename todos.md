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
- **Warm LSP session (perf).** Kill the per-reindex init + settle (~1–2s/file) by keeping
  one initialized `LspClient` per (root, lang) warm in the daemon; incremental
  `didChange` re-query instead of cold spin-up. The design doc's step 2.

## Parked features
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
