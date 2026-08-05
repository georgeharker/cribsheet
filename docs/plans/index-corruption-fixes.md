# Plan: index-corruption fixes — exact specification

Status: **EXECUTED 2026-08-05** — all four work packages ①–④ and 2.9 landed
on dev (`fix: hardening sweep` commit); kept as the spec-of-record.

Expands Tier 2 of `robustness-fixes.md` into execution-ready specs. Four work
packages ①–④ plus 2.9. Line numbers from the 2026-08-05 tree — re-grep first;
Tier 0 / stall-fix agents have been editing concurrently.

Governing principle for every change here: **a record must be either correct
or visibly dirty — never plausibly wrong.** Every failure path degrades to a
dirty/pending marker that an existing backlog sweep picks up, not to silent
absence or silent misattribution.

---

## ① Deletion safety (`codeindex.py`, `codeindexer.py`) — one agent, sequential

### 2.2 Non-UTF8 file must not kill the session
- `codeindex.py:1121` (`_extract`): `path.read_text(encoding="utf-8",
  errors="replace")`. The LSP `didOpen` is sent the same replaced text, so
  positions stay self-consistent.
- Introduce typed errors (module-level in codeindex.py):
  `ExtractError` base; `FileReadError(ExtractError)` wrapping
  OSError/UnicodeDecodeError; `SessionError(ExtractError)` for
  timeouts/protocol/dead-session. The retry handler at `codeindex.py:875-878`
  discards the session **only** on `SessionError`; `FileReadError` skips the
  file (recorded in sweep results as `skipped: [{file, error}]`, one warning
  line) and never touches the pool.
- Test: repo with a latin-1 file → sweep completes, file reported skipped,
  session object identity unchanged across the encounter.

### 2.3 Honor settle; hash-gated deletion rule
- `extract_file(settle=…)` (`codeindex.py:1150-1152`): change the wait from
  `min(settle, _REUSE_SETTLE)` to: `settle: float | None = None` — `None`
  means policy default (`_REUSE_SETTLE` warm / full initial cold), an explicit
  float is honored exactly. The confirm call at `codeindexer.py:153` passes
  `settle=3.0` and now actually waits 3s.
- **New deletion gate** (replaces trust-the-confirm at
  `codeindexer.py:227-228`): vanished symbols are deleted **only if the
  file's content hash changed** since the last index. If the hash is
  unchanged and previously-indexed symbols are missing from the extract, that
  is by definition an extraction anomaly (the code didn't change — the
  symbols can't have) → keep all symbols, mark the file dirty, requeue via
  the describe/backlog machinery. This makes wrongful deletion structurally
  impossible rather than timing-dependent; the settle fix then only affects
  latency of legitimate updates.
- Test: simulate a partial documentSymbol response for an unchanged file →
  zero deletions, file marked dirty; change the file and remove a symbol →
  deletion happens.

### 2.4 Reader-thread death fails fast
- `_reader` (`codeindex.py:355-383`): wrap the frame loop in
  `try/except Exception as e` → set `self._dead = repr(e)`, complete every
  pending future in `self._resp` with `SessionError`, exit.
- `request` (`codeindex.py:405-416`): if `self._dead`, raise `SessionError`
  immediately (no 30s wait). On timeout, pop the request id from
  `self._resp` (fixes the response leak noted in review).
- The existing discard/retry path then replaces the session once.
- Test: feed one garbage frame → next request raises immediately; session is
  discarded and replaced exactly once.

---

## ③ Serialization hardening (`codeindex.py`, `section_index.py`) — same agent as ①, after it

### 2.5 Escaping + parse failure mode
- Extract ONE shared escape helper (new small module or `util.py`): escape
  order `\\` → `\"` → `\n` → `\r` → `\t`; `_unesc` exact reverse. Replace
  both copies (`codeindex.py:1277`, `section_index.py:91`). Property test:
  round-trip random strings containing newlines/quotes/backslashes/unicode.
- Parse hardening (`codeindex.py:1429` `_parse`): the files are valid TOML —
  parse with stdlib `tomllib.loads` as the primary path. On
  `TOMLDecodeError`: do NOT partially parse; treat the record as
  **merge-dirty** (structure kept from a re-extract, description/keywords
  dropped → the symbol re-enters the describe backlog and self-heals). Keep
  the custom renderer (schema is tiny); only the reader changes.
- Atomic writes: `SymbolIndex.write` (`codeindex.py:1363-1369`) via the
  `save_atomic` tmp+`os.replace` pattern; same for the section_index writer
  if it shares the bare-write pattern.
- Test: a description containing `\n` round-trips; a hand-truncated TOML file
  → symbol reported dirty, re-described on next backlog run, file rewritten
  valid.

---

## ② Attribution keys (`codeindex.py` describe/meta paths, `codestore.py`) — after ①③ (same files)

### 2.6 Describe results keyed by fqname
- The describe request rows carry the fqname; the prompt requires it echoed
  per row. `_rows_to_meta` (`codeindex.py:1206-1214`) keys by fqname.
- `match_meta` (`codeindex.py:1237-1267`): primary = exact fqname; fallback =
  tail match splitting on `.` **and** `::` (Rust), applied only when the
  match is unique within the file; ambiguous or missing → drop the row
  (symbol stays undescribed → backlog re-describes; never guess).
- Test: `A.run` + `B.run` in one file → two distinct descriptions on the
  right symbols; a Rust `mod::fn` fqname tail-matches.

### 2.7 Patch `references` symmetrically; fqname keys
- `patch_called_by` (`codestore.py:259-289`) → `patch_edges`: on an
  incremental file reindex, recompute outgoing `calls` AND `references`;
  update reverse edges (`called_by`, `references`) on every affected target
  under the project lock. Replace the `(name, file)` key (`:268`) with
  fqname.
- Test: watcher-style edit adds a reference A→B → B's dossier shows it
  without B reindexing; removing it removes the reverse edge; two same-named
  symbols in one file patch independently.

---

## ④ Decay + recovery (`describe_queue.py`, `notestore.py`, `app.py`) — AFTER the stall fix and Tier 0 land (shares `notestore.py`/`app.py`)

### 2.1 Backoff actually backs off
- `_fire` (`describe_queue.py:69-78`): thread the popped entry's `level` into
  `_run`; on failure `_arm(key, level=entry.level + 1)` (capped); on success
  the entry is gone (level resets naturally on next dirty).
- Task refs (`describe_queue.py:85`): hold spawned tasks in a set with a
  done-callback that discards and logs exceptions — or accept the app's
  `_spawn_bg` as a constructor arg. Same treatment for `watch.py:121` and
  `:271` and `memmirror.py:105-107` (robustness 1.5) if the stall agent
  hasn't already done them — check first.
- Test: describer stub that always raises → observed delays double up to the
  cap; a succeeding run clears the entry.

### 2.10 Dim-change triggers full recovery, visibly
- `notestore.py:124-136`: after `recreate()`, schedule a **full** re-embed —
  every project plus in-situ `sources/…` docs — via the stall fix's
  `Crib.reconcile_in_background()` with `reason="embedder profile change"`.
- `status` surfaces `reindexing: {reason, remaining, total}` until done.
- While a project has not yet been re-swept, `lookup`/`apropos` results for
  it carry `index_rebuilding: true` (echoed by the CLI renderer and visible
  to MCP callers) so thin results are explained, not silent.
- Test: two projects, flip embed dim, write to one → both projects' lookups
  return results after the background sweep; the marker appears during and
  disappears after.

### Live-store recovery (operational, after ④ lands)
Restart the daemon (loads all fixes), then run the full reindex sweep — the
live `crib_chunks` holds only ~305 chunks (2.10 already fired in production);
until the sweep completes, other projects' lookups are silently thin.

---

## 2.9 Chunk-id occurrence counter (`chunk.py`) — independent small agent, any time

- In `_split_sections`, count occurrences of each `heading_path` key in
  document order; the `chunk_id` hash input appends `#<n>` for n ≥ 2.
  `section_line_map` (`chunk.py:187`) uses the same counter so spans align
  with chunks instead of keeping first-span-only.
- Migration: introduce `INDEX_SCHEMA_VERSION` (collection/store metadata; add
  if absent). On open with an older version: trigger a background reconcile —
  the normal `index_file` path re-embeds under the new ids and deletes the
  stale ones (hash gate misses on new ids by construction), so no special
  migration code; bump the stored version after the sweep.
- Test: a note with two identical `### Notes` sections → both sections
  retrievable, distinct ids, line map covers both.

---

## Sequencing summary

- **Agent A** (serial, owns `codeindex.py`): ① → ③ → ② .
- **Agent B** (parallel with A): 2.9 in `chunk.py`.
- **Agent C** (only after stall-fix + Tier 0 agents land): ④.
- All: match existing style; tests per item as specified; run the full suite;
  do not commit.
