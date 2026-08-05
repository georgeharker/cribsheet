# Plan: robustness & correctness fixes

Status: **EXECUTED 2026-08-05** — Tiers 0–4 all landed on dev (see the
`fix: hardening sweep` and `fix: robustness Tiers 3-4` commits); this doc is
now the record of what was found and why each fix took the shape it did.
Reviewed 2026-08-05 (three parallel deep reviews: storage/notes,
code-index, server/session). Companion to
`surface-parity-fixes.md` (behavioral/naming drift lives there; `project_use`/
`project_current` duplication is there, item P2.5). Line numbers from the
2026-08-05 working tree — re-grep if drifted.

Each item: defect → fix → test. Work tiers in order; items within a tier are
independent unless noted.

---

## Tier 0 — security / data loss (do first)

### 0.1 Path traversal via `relpath`, `project`, and version names
Found independently by both reviewers. No containment check anywhere:

- `notestore.py:45-52` `abspath` = `dir(project) / relpath`: `relpath="../../x"`
  escapes; an **absolute** relpath makes pathlib discard the base entirely. So
  `note_read` reads and `note_forget` **unlinks arbitrary files** the daemon
  can touch — and every relpath is an LLM-supplied MCP argument.
- `paths.py:55-59` `project_dir` = `projects_dir / project`: `project="../x"`
  escapes; `project_use("../x")` eagerly mkdirs outside the tree
  (server.py:640).
- `versions.py:55-56` `_note_dir(note_id) / name` with caller-supplied
  `note_id`/`name` (`note_restore(..., version="../../../x")`).

**Fix:** one helper, used at all three seams:
`_confine(base: Path, *parts) -> Path` that joins, resolves, and raises
`ValueError("path escapes <base>")` unless `result.is_relative_to(base.resolve())`;
additionally reject absolute parts and any `..` segment up front (clearer
error, no resolve() surprises with symlinked stores). Validate `project`
names against `^[A-Za-z0-9._-]+$` (no separators) in `resolve_project` /
`project_use`. **Tests:** each verb with `../`, absolute path, and a
separator-bearing project name → clean error, filesystem untouched.

### 0.2 `sources/…` relpaths are writable through note verbs
`notestore.write`/`delete` have no `SRC_PREFIX` guard (notestore.py:56-85), so
`note_edit` stamps crib frontmatter into a *source repo's* README, and
`note_forget` **deletes the file from the user's checkout**; `note_restore`
rewrites it (app.py:1809-1813). The read-only discipline exists only inside
`_index_locked` (indexer.py:104-110).

**Fix:** reject writes/deletes/restores to `SRC_PREFIX` relpaths in
`NoteStore.write/delete` (single guard at the choke point, not per-tool) with
an error pointing at the in-situ contract ("source files are indexed in place;
edit them in their repo"). **Test:** `note_edit`/`note_forget`/`note_restore`
on a `sources/` relpath → error, source file byte-identical.

### 0.3 One malformed note aborts whole-project reconcile
`notes.parse` (notes.py:47) lets `yaml.YAMLError` fly; `NoteStore.reindex`'s
loop (notestore.py:137-140) has no per-file try, so one bad frontmatter (hand
edit, conflict markers) kills the **daemon startup reconcile** for the whole
project, and `NoteStore.write:58` can't even overwrite-to-repair.

**Fix:** wrap the per-file body in reconcile/reindex loops: collect
`{relpath: error}` skips, report them in the result (`skipped: [...]`), never
abort siblings. In `notes.parse`, catch `yaml.YAMLError` and raise a
`NoteParseError(relpath, cause)` so callers can distinguish. Let
`NoteStore.write` overwrite a corrupt existing note (stash raw bytes to the
ring first — recovery still possible). **Test:** project with one bad + two
good notes → reconcile indexes 2, reports 1 skip; `note_edit` repairs the bad
one.

---

## Tier 1 — daemon correctness under load

### 1.1 Sync embedding on the event loop stalls every client
`indexer.py:146` runs `embedder.embed(...)` inside `_index_locked`, called
directly on the loop (`index_file`, indexer.py:74-76); reached from every
write verb, `note_reindex`, `project_reconcile`, and the watcher
(app.py:204-205). A post-pull reconcile embedding hundreds of chunks freezes
all MCP clients; `DaemonClient._wait_ready` (client.py:89-105) then declares
the daemon dead. `store_note`'s dedupe probe also embeds inline
(app.py:459-470).

**Fix:** push the embed (and chunk hashing / store upsert I/O) into
`asyncio.to_thread` from within `_index_locked`, keeping the per-path asyncio
lock at the async layer (the comment at app.py:234 objects to threading the
*locks*, not the compute). Requires 1.2 (thread-safe stores) first.
**Test:** start an N-note reconcile, assert a concurrent `status` call
returns within ~100ms.

### 1.2 Store/caches mutated on loop thread, read from FastMCP's threadpool
`JsonStore` (store.py:113-150) is a plain dict rewritten wholesale on every
mutation; `LexicalCache`/`SummaryVectorCache` are invalidated by writers
(indexer.py:156) while `note_lookup` reads them from worker threads →
`dictionary changed size during iteration` / half-applied reads.

**Fix:** a `threading.Lock` (or RLock) inside JsonStore around `_recs`
mutation+save and around snapshot-for-query (query iterates over a shallow
copy); same for the two caches (invalidate/build under lock, hand out
immutable snapshots). **Test:** hammer upserts in a thread while querying;
no exceptions, queries see either pre or post state.

### 1.3 Startup reconcile blocks the transport
`server.py:669` awaits `reconcile_all()` **before** `mcp.run_async` (:680) —
a cold daemon with an offline backlog holds the port closed past the client's
30s ready timeout.

**Fix:** move reconcile to a post-startup background task via the existing
`_spawn_bg` (app.py:198-202); expose progress via `status` (`reconciling:
true/remaining`). The hash gate makes serving-during-reconcile safe by design
(DESIGN §9). **Test:** daemon accepts `ping` immediately even with a large
backlog.

### 1.4 Stale Chroma collection handle after `recreate()` — the live
"Collection does not exist" bug
`ChromaStore.__init__` caches `self._col` once (store.py:162-166);
`recreate()` (:173-184) refreshes only its own handle. Any *other* attached
process (shared mode, daemon+CLI mix) keeps a handle bound to the deleted
collection UUID and every op errors until restart. Trigger is automatic:
`NoteStore.reindex` calls `recreate()` on an embed-dim change
(notestore.py:124-128). This is presently reproducible against the live
daemon (note_lookup errors "Collection [uuid] does not exist").

**Fix:** wrap ChromaStore ops in a retry-once-on-NotFound that re-resolves
`get_or_create_collection` and replaces `self._col`. Also stop
`recreate()` swallowing all exceptions (store.py:179-181) — only pass on
"not found". **Test:** two ChromaStore instances on one dir; recreate via A;
op via B succeeds after transparent refresh.

### 1.5 Fire-and-forget tasks with no strong reference (3 sites)
asyncio holds tasks weakly; these can be GC'd mid-flight, silently dropping
work — the codebase's own `_spawn_bg` (app.py:198-202) exists for exactly
this:
- `watch.py:121` `_fire` → dispatch task (a save's reindex silently lost);
- `watch.py:271` `CodeWatcher._flush`;
- `describe_queue.py:85` describe task (multi-second LLM call);
- `memmirror.py:105-107` `_fire` live-sync task, which *also* has no
  exception callback.

**Fix:** route all four through `_spawn_bg` (or a local task-set + done
callback logging exceptions). **Test:** weakref/gc stress is flaky — assert
by inspection that tasks land in the holder set; add exception-logging
callback tests instead.

### 1.6 Multi-process access has no guard at all
Both reviewers: daemon + `--no-daemon` CLI (or the *silent* in-process
fallback in `_reconcile`, cli.py:969-996, or a second stdio `crib --mcp`)
open the same embedded Chroma / JsonStore / `symbol_index/*.toml` /
`doc-sources.json`. JsonStore is whole-file last-writer-wins — one process
silently discards the other's writes. `SourceRoots._save` isn't even atomic
(sources.py:68-70) and `__init__` swallows parse errors to `{}`
(sources.py:36-39): a crash mid-write **empties the registry**.

**Fix (scoped):** (a) an advisory lockfile (`fcntl.flock`) on the data dir
taken by any in-process `Crib.open()` in write mode; when held elsewhere,
error with "daemon is running — drop --no-daemon or stop it" instead of
corrupting; (b) make `SourceRoots._save` and `SymbolIndex.write`
(codeindex.py:1363-1369) and `VersionRing.stash` (versions.py:41) use the
existing `save_atomic` tmp+rename pattern; (c) `MemoryBindings.upsert` RMW
under the same flock. Full multi-writer support stays out of scope — the
design answer is the daemon (DESIGN §10.2); the lock just makes violations
loud. **Test:** hold the lock, run `--no-daemon` write → clean error.

---

## Tier 2 — indexing correctness

### 2.1 Describe-queue backoff is broken (hammers a down endpoint)
`_fire` pops the `_Entry` (dropping `level`) so a failed describe re-arms at
level 0 forever (describe_queue.py:69-78, 91-92) — one failing call per file
per ~2s, exactly what the backoff exists to prevent.

**Fix:** on failure re-arm with `level + 1` (thread the popped entry's level
into `_run`'s failure path). **Test:** stub a failing describer; assert
delays grow geometrically.

### 2.2 Non-UTF8 source file kills the warm LSP session
`path.read_text()` in `_extract` (codeindex.py:1121) raises
`UnicodeDecodeError` *before* the session is used; the retry handler treats
it as a wedged server and `pool.discard`s the warm session (codeindex.py:875-878)
— every encounter of one latin-1 file cold-starts the language server
(minutes on rust-analyzer).

**Fix:** `read_text(encoding="utf-8", errors="replace")` (positions stay
consistent since the LSP doc is opened with the same text); or catch
`UnicodeDecodeError` and skip the file with a warning. **Test:** index a repo
containing a latin-1 file; session object identity unchanged after.

### 2.3 "Slow confirm" guard is a no-op → real symbols get dropped
`extract_file(settle=3.0)` on a warm session waits
`min(settle, _REUSE_SETTLE)` = 0.3s (codeindex.py:1150-1152), so the
partial-result re-check (codeindexer.py:150-155) re-reads the same partial
listing and the vanished-symbol pass (codeindexer.py:227-228) deletes live
symbols.

**Fix:** honor the caller's settle: `max(settle, …)` on the confirm path (or
a dedicated `confirm=True` flag that forces the full wait). **Test:** unit
test the wait computation; integration: simulate a slow documentSymbol,
assert no deletions on the confirm pass.

### 2.4 LSP reader-thread death poisons the session silently
One malformed frame kills `_reader` (codeindex.py:355-383); every subsequent
request burns its full 30s timeout (codeindex.py:405-416) — a sweep
serializes 30s stalls with no diagnosis.

**Fix:** catch-all in `_reader` that marks the session dead
(`self._dead = True`, wake all waiters); `request` fails fast on a dead
session so the existing discard/retry path replaces it. **Test:** feed a
garbage frame; next request raises immediately, session gets discarded once.

### 2.5 Newlines in LLM output corrupt the symbol-index TOML
`_esc` (codeindex.py:1277) doesn't escape `\n`; `_render` (:1303) emits
one-line scalars; the line-oriented `_parse` (:1429) then truncates the value
and misreads following lines — persistent corruption of that symbol's record.

**Fix:** escape `\n` → `\\n` in `_esc`, reverse in `_unesc` (mind existing
ordering); sanitize on parse too (a lone quoted-but-unterminated value →
treat as merge-dirty, don't propagate). Same `_esc` exists in
section_index.py:91 — fix both or extract one helper. **Test:** describe
returning an embedded newline round-trips.

### 2.6 Same-named symbols clobber each other's descriptions
Describe results keyed by bare local name; `_rows_to_meta` clobbers dups
(codeindex.py:1206-1214), so `A.run` and `B.run` in one file get one
description assigned to both (codeindexer.py:270-272). Also `match_meta`'s
tail-match splits on `.` only — Rust `::` fqnames never match
(codeindex.py:1263).

**Fix:** key by fqname when the describe prompt can echo it (preferred:
include fqname in the request rows and demand it back); fallback: keep a
list per name and match by signature/line. Add `::` to the tail-split.
**Test:** two same-named methods → distinct descriptions land correctly.

### 2.7 Incremental reindex never patches `references`
`patch_called_by` updates `called_by` only (codestore.py:259-289); editing A
to add/remove a mention of B leaves B's `references` stale until B's own file
reindexes — dossier/graph lie under watcher operation. Also the
`(name, file)` key (:268) collides on same-named symbols in one file.

**Fix:** extend the patch pass to `references` symmetrically; key by fqname.
**Test:** watcher-driven edit adding a call A→B; B's dossier shows the new
reference without reindexing B.

### 2.8 Dense-score sentinel buries undescribed symbols
`-1.0` sentinel for description-less symbols (codequery.py:165) means an
exact-name query (`gnorm=1.0`) can never outrank any described symbol —
freshly indexed files are unfindable by name until describes complete.

**Fix:** sentinel 0.0 (name/keyword signal stands on its own). Re-run
`scripts/eval_retrieval.py` code slices to confirm no regression — the
fusion is measured, don't tweak blind (DESIGN §10.3). **Test:** undescribed
symbol, exact-name query → rank 1.

### 2.9 Chunk-id collision on duplicate heading paths
`chunk_id = sha1(project, relpath, heading_path, window_idx)` (chunk.py:39-43)
collides for two sections with the same effective heading stack; later
section overwrites, orphan windows attach to wrong content;
`section_line_map` (:187) keeps first-span-only.

**Fix:** disambiguate with a per-note occurrence counter in the id input
(`heading_path + "#2"` for the second occurrence — deterministic across
reindexes since it's document order). Bump requires reindex: gate behind a
store-version marker so old chunks reconcile away. **Test:** note with two
identical `### Notes` sections → both retrievable, distinct ids.

### 2.10 Dim-change `recreate()` wipes all projects, reindexes one
notestore.py:124-136: after the wipe only the current project re-embeds;
other projects and this project's `sources/…` docs silently vanish from
search (queries succeed, empty).

**Fix:** on dim change, either (a) iterate *all* projects + in-situ targets
(correct, slow — do it via the background reconcile from 1.3 with progress in
`status`), or minimally (b) record `pending_reindex: [projects]` in status
and warn on every lookup against a wiped-but-unreindexed project. Do (a);
it's the honest one. **Test:** two projects, flip embed profile, reindex one
→ other project's lookup returns results after the sweep completes.

---

## Tier 3 — git/sync + config resilience (smaller, mechanical)

- **3.1** `gitbacking.py:291` — `committed = "nothing to" not in snapshot(...)`
  string-sniff: a *failed* commit reads as committed, then sync pulls onto a
  dirty tree. Fix: make `snapshot` return structured `(ok, committed, msg)`
  (check returncode), not prose. Test: unset `user.name` → sync stops with
  the real error.
- **3.2** `gitbacking.py:305-307` — unborn-HEAD `_branch()` guesses "main";
  joining a `master` remote silently bootstraps a divergent branch. Fix:
  after fetch, prefer the remote's HEAD branch
  (`symbolic-ref refs/remotes/origin/HEAD`, fallback `ls-remote --symref`);
  only then default. Test: join a master-branch remote → lands on master.
- **3.3** `gitbacking.py:65-69` — git subprocesses: add `timeout=` (generous,
  e.g. 120s network / 15s local) + `stdin=DEVNULL` so a credential prompt
  can't wedge the daemon thread (DESIGN §14 says network git is CLI-side —
  enforce it: the daemon path should never run fetch/pull/push at all).
- **3.4** `config.py:244-257` — unknown config key `TypeError` kills every
  command. Fix: filter kwargs to dataclass fields, collect unknowns into a
  one-line stderr warning naming file+table+key; wrap `tomllib` errors with
  the path. Test: config with a typo'd key → command runs, warning printed.
- **3.5** `config.py:318-327` — malformed `.crib` (missing `project`,
  non-dict, bad YAML) raises out of `find()`, breaking every command run
  below it. Fix: catch, warn with the path, continue walking up (treat as
  absent). Test: junk `.crib` in a parent dir → commands still work.
- **3.6** `config.py:339-351` — `portable_path` doesn't `resolve()` symlinks;
  on macOS (`/tmp` → `/private/tmp`, symlinked `~/Development`) paths miss
  their `$LOCATION` root and serialize machine-specific — the exact conflict
  the feature prevents. Fix: match against both raw and resolved forms of
  path *and* roots. Test: symlinked root round-trips to a token.
- **3.7** `versions.py:51,59,63` — a stray non-`NNNNNN-hash.md` file in a ring
  dir raises `ValueError` in `_next_seq` → **every write for that note
  fails**. Fix: skip non-matching names (warn once). Test: drop `foo.md` in a
  ring dir; writes still work.

---

## Tier 4 — worth doing, not urgent

- **4.1** memmirror.py:43-44 — `catch_up` swallows per-binding exceptions
  bare; add a stderr line per failed binding (path + error).
- **4.2** notestore.py:57-61 — id-less notes skip the version ring on
  overwrite/delete while `delete` claims recoverability; stash by
  content-hash key when no id.
- **4.3** notestore.py:87-109 — `move` writes destination before unlinking
  source (crash → duplicate id); unlink-then-write is worse; acceptable fix:
  after-crash reconcile detects duplicate ids and reports (cheap scan in
  reconcile), plus compare resolved paths not strings in the same-target
  guard.
- **4.4** learnings.py:227-237 — `rehome` silently overwrites an existing
  learning at the target; raise like `NoteStore.move` does.
- **4.5** notestore.py:36-39 — `abspath`/`dir` mkdir on *read* paths: a
  typo'd project name in a lookup creates a phantom project dir. Split
  ensure-on-write from resolve-on-read.
- **4.6** codeindex.py:691-706 — edge attribution: anchor site-packages
  suffix match at path boundaries; `_in_workspace` add trailing `/`;
  percent-decode URIs (spaces/non-ASCII workspace paths currently break
  every cross-file edge).
- **4.7** codeindex.py:1337-1361 — fqname→filename is case-sensitive but APFS
  isn't: `Chunk` vs `chunk` collide on the dev platform. Append a short
  case-hash to the slug when it contains uppercase.
- **4.8** store.py:61-62 — `_cosine` zip-truncates on dim mismatch (Json/
  InMemory): assert equal dims, raise loudly.
- **4.9** embed.py:183-188 — ImportError → `HashEmbedder` fallback silently
  queries garbage against real-model vectors; refuse when the store already
  holds vectors from a different profile.
- **4.10** session.py:89-94 — `project_path` with no `.crib` falls back to
  default but is tagged `via="path"` (implicit=False), muting the
  wrong-project echo built for exactly this; tag `via="default"`.
- **4.11** server.py:198-207 vs 112-130 — `note_store`'s `anyOf` wire schema
  makes the promised elicitation unreachable for schema-validating clients;
  drop `note_store` from the anyOf (runtime `_write_project_elicit` already
  enforces/elicits).
- **4.12** app.py:477 — `title or head if cond else "note"` precedence bug
  discards an explicit title for whitespace content; parenthesize.
- **4.13** watch.py:123-127 — `_FSWatcher.stop` doesn't cancel pending
  debounce timers (post-shutdown dispatch against a closing loop); mirror
  `CodeWatcher.stop`. Add an exception guard in `Watcher._dispatch`
  (:148-149) like the code watcher has.
- **4.14** watch.py:213-255 — `CodeWatcher._decode` does file I/O + `.crib`
  YAML parsing on the watchdog event thread; move past the debounce
  boundary.
- **4.15** client.py:40-43 — `{"result": …}` unwrap heuristic misfires on a
  legitimate single-key payload; unwrap only for known scalar-returning
  verbs or tag the envelope.
- **4.16** Dead code sweep: `match_description` (codeindex.py:1270),
  `_lexical_tokens` (retrieve.py:140), dict-branch in `start_watchers`/
  backlogs (app.py:175,273,304), watch.py:33-34 redundant pattern. Dedupe:
  `_esc` (codeindex/section_index), coverage (retrieve/codestore), min-max
  rerank fusion (app.py:618-642 vs 1322-1342), fenced-code scanners
  (chunk.py `_split_sections` vs `section_line_map`).
- **4.17** Boundary validation: `code_graph` direction enum, k/depth bounds,
  empty query/symbol → useful errors (server.py:493-502 etc.). Replace the
  `"unknown symbol" in str(err)` string contract (refs.py:116-117) with a
  typed exception.

## Verified sound (don't "fix")

Call-hierarchy direction mapping (callers/callees), `_unesc`/`_unquote`
inverses, BM25 empty-corpus handling, RRF confined to cross-project fan-out,
the deferred-describe clobber guard (hash re-check under project lock), the
per-path-lock + hash-gate write model itself, and fastmcp error propagation
(self-diagnosing ValueErrors reach the agent usefully).

## Suggested execution order

Tier 0 (one PR: 0.1+0.2 share the confinement helper; 0.3 separate) →
1.4 (unbricks the live daemon) → 1.3 → 1.2+1.1 (one PR, ordered) → 1.5, 1.6
→ Tier 2 items independently (2.1/2.2/2.3 first — they're active data-loss
under normal operation) → Tier 3 → Tier 4 opportunistically. Add the
grep-guard test from `surface-parity-fixes.md` P3 in the same pass as any
touched docstrings.
