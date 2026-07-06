# todos — deferred / to-test

## To discuss / fix
- **Vendored sub-repos pollute the parent's index (mis-rooted paths).** `project_index`
  enumerates `vendor/**/*.py` (`vendor` isn't in the code-ignore set), and per-file
  `find_root` then escapes into the vendored sub-repo (its own `.git`/`pyproject`), so the
  entries get stored under the *parent* project with paths relative to the *sub-repo*
  (e.g. `src/llmkit/…` inside the `cribsheet` project). Against the parent's `source_root`
  they read as "source GONE". Observed: 161 phantom llmkit symbols in the cribsheet index.
  Options to weigh: (a) add `vendor` + nested-`.git` dirs to the enumeration ignore set;
  (b) the deeper fix — in the project-index path, pin the root to the project's `.crib`
  root instead of per-file `find_root`, so a project only ever holds files under its own
  root. `_revalidate` self-heals existing phantoms on the next query (stat fails →
  `_drop_file`), but the enumeration bug re-pollutes on the next `project_index`.
  (Also note: watcher only catches edits made *after* it starts — edits while the daemon
  is down are caught by the lazy mtime gate on next query, not retroactively by the watcher.)

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
