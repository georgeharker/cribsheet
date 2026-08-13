# Symbol identity: one name, one key, one conversion

The design of record for finishing the `symbol_ref` change. Written against the
code and the fourteen live stores, not against the docs — which are ahead of the
code in places and behind it in others.

Two questions started it:

1. Are all symbol references `path#tail`, and are on-disk filenames derived from
   that? **No, on both counts.**
2. Can an old record be converted in place — with what markers, what resumability?
   **Yes, and the same mechanism serves both stores.**

---

## 1. Evidence

### The stores

```
project                entries   .schema   with symbol_ref   entries stamped   learnings
cribsheet                 2300         4              2300                 0           1
svg-mcp                   2667         4              2667                 0           4
zdot                       567         4               567                 0           0
sharedserver               164         4               164                 0           0
mcp-companion             1853         2                 0                 0           1
music-llm                 3798         2                 0                 0           0
dotfiles                    49         2                 0                 0           0
ai2-scholarqa-lib          633         –                 0                 0           0
asta-bench                1220         –                 0                 0           0
asta-plugins               622         –                 0                 0           0
dotfiler                   576         –                 0                 0           0
llmkit                     163         –                 0                 0           0
opencode-mcp-combiner       36         –                 0                 0           0
zsh-ai                     387         –                 0                 0           0
```

Three populations — four converted, three stamped-but-not, seven never stamped —
and **not one of 14,035 entries carries a `schema =` line.**

### A converted entry, and where it lives

```
$ head -3 …/cribsheet/symbol_index/crib.__version__.toml
fqname     = "crib.__version__"
symbol_ref = "crib/__init__.py#__version__"
fqn        = "crib.__version__"
```

The filename is the **legacy fqname**. Every entry in the most-converted store on
the machine is filed under the old spelling.

### The migrated learning notes

| project | bound to | index calls it | file on disk |
|---|---|---|---|
| cribsheet | `crib/retrieve.py#reciprocal_rank_fusion` | `crib.retrieve.reciprocal_rank_fusion` | `crib-retrieve.py-reciprocal_rank_fusion-f979eab8.md` |
| svg-mcp ×4 | `src/svg_mcp/…#_apply_fx` … | `svg_mcp.ops.resources._apply_fx` … | `src-svg_mcp-…-_apply_fx-eb893073.md` |
| mcp-companion | `combiner.…create_encrypted_store` | *(same)* | *(same)* |

The one that still works end to end is the one that was **never migrated**. The
filenames of the migrated ones are already correct — `ref_slug(symbol_ref)`
reproduces them exactly. Only the readers are wrong.

---

## 2. The three spellings

### Ground truth is four fields

The extractor *observes* `file`, `lang`, `container`, `name`. Everything else is a
rendering. Worked through on a deeply nested real symbol:

```
file       crib/watch.py
lang       python
container  ['_FSWatcher', '_schedule_dir', '_Handler']    ← the ancestor CHAIN,
name       on_moved                                          bare names, outermost first
```

`container` is a list, one element per nesting level, **each a bare single name**.
It is not a reference to anything; `container + [name]` is the declared chain
within the file.

| field | formula | value |
|---|---|---|
| `module` | `module_of(file, lang)` | `crib.watch` |
| `scope` | `_path_scope(lang,file) + clean(container)` | `['crib','watch','_FSWatcher','_schedule_dir','_Handler']` |
| *declared_tail* | `sep.join(clean(container) + [name])` | `_FSWatcher._schedule_dir._Handler.on_moved` |
| `fqn` | `sep.join(scope + [name])` | `crib.watch._FSWatcher._schedule_dir._Handler.on_moved` |
| `symbol_ref` | `file + "#" + declared_tail` | `crib/watch.py#_FSWatcher…on_moved` |
| `fqname` | `qualify(lang, module, container, name)` | `crib.watch._FSWatcher…on_moved` |
| `parent` | `qualify(lang, module, container[:-1], container[-1])` | `crib.watch._FSWatcher._schedule_dir._Handler` |

### `symbol_ref` ≡ `(file, fqn)`

Verified both directions on every converted entry:

```
symbol_ref + file + lang  →  fqn          5698/5698 exact
fqn        + file + lang  →  symbol_ref   5698/5698 exact
```

`symbol_ref` is not a third concept. It is the pair `(file, fqn)` serialized —
`file + "#" + (fqn minus the path-derived prefix)`, where the prefix is a pure
function of `(lang, file)`.

### `fqname` is not a name

`fqname = qualify(lang, module_of(file, lang), container, name)`, and `module_of` —
whose *only* consumers are `fqname` and `parent` — dots the path
**unconditionally, for every language**:

| lang | file | `module_of` → fqname prefix | `_path_scope` → fqn prefix | |
|---|---|---|---|---|
| python | `crib/app.py` | `crib.app` | `['crib','app']` | agree |
| lua | `lua/sharedserver/health.lua` | `sharedserver.health` | `['sharedserver','health']` | agree |
| rust | `rust/src/cli/commands/check.rs` | `rust::src::cli::commands::check` | `['cli','commands','check']` | **disagree** |
| go | `internal/store/store.go` | `internal.store.store` | `['store']` | **disagree** |
| c | `bin/sharedserver-watcher.c` | `bin.sharedserver-watcher` | `[]` | file-scoped |
| zsh | `core/cache.zsh` | `core.cache` | `[]` | file-scoped |
| typescript | `src/api/client.ts` | `api.client` | `[]` | **no path namespace** |
| ruby | `app/models/user.rb` | `app.models.user` | `[]` | **no path namespace** |
| cpp | `src/engine/render.cpp` | `engine.render` | `[]` | **no path namespace** |

`_path_scope` gates on `_PATH_NAMESPACE` (python/lua = module, go = package,
rust = crate) and finds the *last* `src` for crate-relative paths. `module_of`
gates on nothing and strips `src`/`lua`/`lib` only at position 0.

So `fqname` is correct for **python and lua only**. Elsewhere it asserts a
namespace the language does not have: `rust::src::` is not a Rust path,
`app.models.user.User` is not a Ruby name, `bin.sharedserver-watcher.main` is not
anything.

### Which is which

| | what it actually is | disposition |
|---|---|---|
| `fqn` | the language's fully-qualified name, gated per language | **the name** |
| `symbol_ref` | `(file, fqn)` serialized | **the key** |
| `fqname` | **the v0.7.0 key** — a synthetic uniquifier that pathified the module to force uniqueness, and got mistaken for a name because it was called one | **the legacy binding**, kept as `symbol_was[0]` |

`fqname` was never trying to be a name; it was trying to be *unique*. That is the
job `symbol_ref` now does properly — and it is why `fqn` cannot be the key (Go's
`store.Store` collides across two `store` packages; every C `main` collides) and
why the file has to be in it.

Stop calling it a name and the confusion goes with it: **one name, one key, one
history list.**

---

## 3. Where each spelling is authoritative today

`symbol_ref` is a decoration. It is written into entries and note frontmatter, and
**nothing reads it as a key.**

| surface | keyed by |
|---|---|
| `SymbolIndex._relname` → the .toml filename | `learning_slug(fqname)` |
| `read` / `delete` / `_path` / `write` | `fqname` |
| `all()` drop filter | `e["fqname"]` |
| `_ResidentCode.by_fq` | `fqname` |
| `existing` snapshot + content_hash gate | `fqname` |
| `drop_file` → `store.delete(…)` | `fqname` |
| `patch_edges` target resolution | `fqname`, then `(name, file)` |
| edge refs in `calls`/`called_by`/`references` | `name [file]` — a third format |
| `code_graph` node ids | `proj:fqname` — a fourth format |
| `describe_queue`, `match_meta` | `fqname` |
| `resolution()`, every error message | `fqname` |
| resolver `match()`'s `id` tier | `entry.get("id")` — **no entry has that field** |
| learning note frontmatter | `symbol_ref` *or* `symbol` — mixed |
| learning note filename | `ref_slug(symbol_ref)` *or* `…(fqname)` — mixed |
| `learnings.fqns()` | whatever the frontmatter said — mixed |

---

## 4. What is broken

### 4.1 A `symbol_ref` cannot be used as input to any verb

`symbols.match()` has an `id` tier that would match a reference exactly. All four
call sites (`codeindex:1588`, `refs:63`, `refs:162`, `codestore:55`) feed it
`entry.get("id", "")`, and **no entry has ever carried an `id` field**. The tier is
unreachable:

```
match("crib.retrieve.reciprocal_rank_fusion", "reciprocal_rank_fusion",
      "crib/retrieve.py#reciprocal_rank_fusion", "python")   →  None
```

### 4.2 The migration broke the join it was moving

Verified on all five migrated notes:

- **`learning_forget` cannot delete one.** `add`/`edit`/`read`/`reaffirm` resolve
  through `relpath(proj, entry)`, which prefers `symbol_ref`. `forget` alone uses
  `rel_for_fqn(proj, entry["fqname"])`. It looks for
  `crib.retrieve.reciprocal_rank_fusion.md`; the file is
  `crib-retrieve.py-reciprocal_rank_fusion-f979eab8.md`.
- **The `※` glyph is dead on every migrated project.** `codequery:488` and `:625`
  test `n["fqname"] in learnings.fqns(p)`; `fqns()` now returns references.
- **`learnings.candidates`** reads `fm.get("symbol")`, which `rebind` pops.

### 4.3 The resumability marker is not on disk

`_render` emits `schema` and `_symbol_entry` sets it, but both landed in `741ae1b`
and no store has been rewritten since. Every entry reads as `schema = 0`, so
`unconverted()` reports cribsheet's 2300 fully-converted entries as 100% pending.

### 4.4 The converter has no caller

`convert_store` / `convert_entry` / `unconverted` are referenced only by
`tests/test_symbol_convert.py`. No CLI verb, no MCP verb, no call from the indexer.

### 4.5 Two schema mechanisms that disagree

Store-level `.schema` gates single-file writes (`schema_stale()`); per-entry
`schema` is what conversion filters on. cribsheet is `.schema = 4` with 2300/2300
converted and every entry stamping 0 — the gate says done, the converter says
nothing is.

### 4.6 The half-converted guard is a wall with no door

`migrate_symbols` refuses if **any** entry lacks `symbol_ref` (`learnings.py:284`).
The instinct is right — the orphan report would otherwise say *"no live symbol for
this binding"* about a symbol that is merely unreached. But given §4.4 the only way
past is the full reindex conversion existed to avoid.

### 4.7 `existing` is a store-wide dict keyed by a spelling

Threaded `codeindexer.py:67 → :75 → :87`, built at `:163-164` and `:402`, consumed
at `:165`, `:197-202`, `:214`, `:229`, `:284`, `:332`; delegator `app.py:1228,1233`.

| site | use | what it needs |
|---|---|---|
| `:165` | `old_in_file` / vanished drop | prior entries **of this file** |
| `:197-202` | the `stale` describe gate | prior entry **for this symbol** |
| `:214`,`:229` | carry description + keywords | same |
| `:332` | `_deletion_allowed`'s prior `file_hash` | **of this file** |

Every one is scoped to `rel`, and two of them do that with a **full O(store) scan
per file** — so the comment at `:399-401` claiming this made cold onboard O(N)
rather than O(files × symbols) is false. It moved the quadratic into RAM.

### 4.8 The authored sweep

- **`rebind` bypasses the store** (`learnings.py:334` uses `notes.save_atomic`
  where `rehome` at `:449-452` uses `store.write` + `store.reindex`). After a
  *complete, successful* migration the learnings chunk index still points at old
  relpaths. Broken on the happy path.
- **`symbol_was` is written (`:332`) and never read.** Zero readers.
- **The manifest is written before the apply** (`:300` vs `:338`), with invariants
  computed pre-apply.
- **`seen_targets` (`symmigrate.py:56,78-85`) is per-run**, so a resumed run cannot
  predict a collision it will then hit.

---

## 5. Root cause

`symbol_ref` was added **beside** `fqname` — *"carried ALONGSIDE the legacy fqname
through the rollout"* (`codeindex.py:1186`) — with no end state and no verb that
performs the substitution.

v0.7.0 (`71ebada`) shows what was lost. The entry was `fqname · name · kind · lang ·
module · container · parent · …` — **one name, one key** — and the read paths had no
branches:

```python
# v0.7.0
def relpath(self, proj, entry):
    return self.rel_for_fqn(proj, entry["fqname"])      # one line

# now (learnings.py:57-72)
def relpath(self, proj, entry):
    for key in ("symbol_ref", "fqname"):                 # fifteen lines
        ...
```

`forget` did not diverge because someone forgot to update it. **At v0.7.0 there was
nothing to update** — `rel_for_fqn(fqname)` and `relpath(entry)` were the same
function. Every bug in §4 is an artifact of adding a second identity without moving
the key: a function that used to *be* an identity grew a fallback, and its siblings
did not.

v0.7.0 was coherent and semantically *wrong* for four of six languages. The current
state is semantically right and incoherent. **Incoherent is worse**, and it gives
the acceptance test:

> **No read path branches on which spelling it got.** `Learnings.relpath` is a
> one-liner again.

---

## 6. Target shape

### The entry

```
observed   file · lang · container · name · kind · signature · line
           content_hash · file_hash · mtime
identity   symbol_ref              the key
           symbol_was[]            prior bindings — populated by CONVERSION, empty for
                                   a symbol first indexed after it
rendered   fqn                     display, and what the describe prompt shows the model
           scope                   the scope= narrowing axis
semantic   description · keywords · name_terms
edges      calls · called_by · references            (deferred `name [file]`)
meta       schema
gone       fqname → symbol_was[0]  ·  module  ·  parent
```

**`parent` goes.** Its job is to point at another record, so it must be in the key's
spelling — and the parent's reference is a *truncation* of the child's:

```
child   crib/watch.py#_FSWatcher._schedule_dir._Handler.on_moved
parent  crib/watch.py#_FSWatcher._schedule_dir._Handler
```

so the whole ancestor chain is derivable and can never dangle. Two caveats, both
already solved elsewhere in the codebase: split the ref with `id_parts()` before
truncating (the path has dots too), and strip the leaf **by `name`** rather than by
rsplit, because a legal zsh name can contain the separator — the trick
`match()` already uses at `symbols.py:100-102`. A symbol whose only container was
`['for in']` truncates to an empty tail, i.e. no parent record, which is correct.

**`module` goes with it** — its only consumers are `fqname` and `parent`. And once
`symbol_was` is populated by *conversion* rather than *derivation*, the extractor
stops needing `module_of` at all, which is what finally lets `module_of` and
`legacy_fqname` be deleted rather than deprecated.

### The note

```
id (ULID) · title (= entry.fqn) · kind · lang · file · signature · content_hash · source
symbol_ref · symbol_was[] · schema
gone: symbol
```

### The filename — `ref_slug(symbol_ref)`, both stores

```
crib/retrieve.py#reciprocal_rank_fusion       → crib-retrieve.py-reciprocal_rank_fusion-f979eab8
rust/src/…/start.rs#tests::test_parse_env…    → rust-src-…-test_parse_env…-0fa8a85c
src/core/cache.rs#Store<K,V>::get             → src-core-cache.rs-Store-K-V-get-b0f8d4ce

5698 refs → 5698 distinct slugs · 0 collisions · 100% carry a hash
```

`symbol_ref`, **not** `fqn`: `fqn` is not unique, and more fundamentally
`symbol_ref = file + declared_tail` is anchored on **facts** while
`fqn = path_scope + declared_tail` is anchored on a **rule** we may still refine.
A filename should track the stabler of the two.

What needs updating in `symbols.learning_slug`:

- **Name and parameter.** It takes a reference now, and names files in *two*
  stores. Rename for the operation: `ref_slug(ref)`.
- **The conditional hash is vacuous.** A reference always contains `/` or `#`, so
  the munge is always lossy and the hash is unconditional (5698/5698). The
  docstring's promise that a clean fqn "passes through verbatim" is dead for
  references — which is *better*, since `basename == ref_slug(key)` becomes a
  uniform check with no "was this one hashed?" branch.
- **`legacy_learning_slug` generalises.** It is a *prior-name* derivation, and
  prior names now come from `symbol_was`:
  `prior_names(r) = {ref_slug(b) for b in symbol_was(r)} | {legacy_learning_slug(b) …}`.
  That is what `write` unlinks.

---

### `code_graph` node ids — designed, not migrated

```
today   proj:crib.app.Crib.code_graph
after   proj:crib/app.py#Crib.code_graph
```

**Graph node ids are computed per query and never persisted** —
`qualified_symbol(p, e["fqname"])` at `codequery.py:517`, `:530`, `:539`, `:578`,
`:591`, `:627`, and nothing on disk holds one. So unlike the store key this needs no
conversion, no compatibility path and no resume story. It is a payload change, and
it should land *with* the key move rather than be sequenced behind it.

The graph is also where the reference is most obviously the right spelling: a
consumer wants to **act** on a node, and `crib/app.py#Crib.code_graph` is directly
openable where `crib.app.Crib.code_graph` needs a resolve step — and for Rust and
zsh isn't a spelling the language would even recognise. The graph was the worst-
served consumer of the old spelling and is the best-served by the new one.

Release status, checked against `main` rather than a tag (see §12): **none of this
is released.** `950ba3e` (the edges shape) is in the v0.7.0 tag, but v0.7.0 was
tagged on `dev` and never promoted — the last released tag is v0.6.1. So there is no
released shape here to preserve and no released id spelling; the ids-are-ephemeral
argument is now the lesser of two reasons.

Unaffected: `external` nodes stay keyed by their **raw** ref (an unresolved edge has
only `name [file]` to key on), and the rollup is untouched — `_group_key`
(`codequery.py:68-85`) reads `scope`, `file` and `dir`, never a node id and never
`e["module"]`, so dropping the `module` field does not reach it.

Consequence for `※`: the marks test (`:488`, `:625`) compares a node's `fqname`
against `learnings.fqns(p)`, which now returns references. Once node ids *are*
references and the join goes through `bindings()`, both sides are one spelling and
the glyph works with no special case.

## 7. Conversion: one predicate, two stores

```
done(r)        =  r.schema == TARGET  and  basename(r) == ref_slug(key(r))
convertible(r) =  the inputs its derivation needs are present
work-list      =  [r for r in all() if not done(r)]        ← the ONLY record of progress
```

| | key | `convertible` needs | derives |
|---|---|---|---|
| **entry** | `symbol_ref` | `file` and `name` — present back to schema 0 | `symbol_ref`, `fqn`, `scope`, `symbol_was=[fqname]`; drops `fqname`/`module`/`parent` |
| **note** | its binding | an entry that answers to its binding **and is itself done** | `symbol_ref`, `symbol_was+=[old]`, `title=entry.fqn`; drops `symbol` |

### A pass

```
1. entries:  convert every convertible one; write at ref_slug(symbol_ref); unlink prior names
2. build     by_binding = {b: e for e in entries for b in bindings(e)}
3. notes:    convert every convertible one; write THROUGH NoteStore, same rule
4. if both work-lists are empty → stamp the store
```

Entries never block each other, so step 1 completes the entry store in one sweep.
Notes depend on entries and nothing depends on notes, so the dependency is
**one-directional and acyclic**: one uninterrupted run converges completely.
Interleaving matters only for resume.

### The note's four states

```
done (schema current AND canonical name)                       no-op
binding → an entry that is done                                CONVERT
binding → an entry not yet converted                           PENDING   ← come back
binding → nothing                                              ORPHAN    ← the real state
```

`build_map` currently skips entries lacking `symbol_ref`, collapsing **pending**
into **orphan** — which is the lie behind the guard of §4.6. Separating the two
rows removes the need for it, and lets the two conversions interleave freely.

### Why the join is correct at every instant

One function enumerates what an entry answers to, and `key()` is *derivable*, so an
unconverted entry still has one:

```python
def key(e):      return e.get("symbol_ref") or symbol_ref(e["file"], e["container"], e["name"], e["lang"])
def bindings(e): return [key(e), *(e.get("symbol_was") or ([e["fqname"]] if e.get("fqname") else []))]
```

| entry | note | note's binding | found via |
|---|---|---|---|
| unconverted | unconverted | `fqname` | `symbol_was` ✓ |
| converted | unconverted | `fqname` | `symbol_was` ✓ |
| converted | converted | `symbol_ref` | `key` ✓ |
| unconverted | converted | `symbol_ref` | *derived* `key` ✓ |

So `attach`, `report`, `read`, `forget` and `※` are correct throughout — and the
only place that branches on spelling is `bindings()`, which satisfies §5's
acceptance test.

**Immediate consequence:** the five broken notes on this machine are row 3 — both
sides already converted. They fail *only* because the readers key on `fqname`. Fix
the reader and they work with **no data change at all**.

### Crash states: two, plus a leftover

`write_atomic` is temp + `os.replace`, so every record is at all times either
*original* or *done*. The rename adds one transient:

```
write new (canonical)  ──crash──►  old file + new file, same key
unlink prior names
```

Handled by derivation, not a journal: `all()` groups by `key(r)` and keeps the
record whose basename is canonical; anything else is a **leftover**, unlinked by
the next pass's `write`. Because `key()` works on unconverted records, the old file
groups correctly with its replacement despite having no `symbol_ref` field.

### Termination, and an honest residue

Each pass strictly shrinks the work-list unless what remains is *permanently*
unconvertible:

- an entry with no `file`/`name` → **needs a reindex**, said out loud;
- a note whose binding resolves to nothing → a **genuine orphan**, which
  `learning_report` already models and `learning_rehome` already repairs.

The loop terminates, and the residue is exactly the set needing a human or a
reindex — never a set needing another pass.

### What may not be converted

Conversion applies **only** when the new fields are pure functions of fields the
store already holds. `preserved()` stays as the per-entry assertion that
`description`, `keywords`, `calls`, `called_by`, `references` come through
byte-identical — which is what makes "identical before and after" a check rather
than an argument, and it matters: a shuck 0.1.0→0.1.1 upgrade moved svg-mcp by +825
edges mid-migration.

**Edge refs stay `name [file]`.** An edge cannot carry a reference: the target's
container chain is unknown when the edge is written — it is a *deferred* reference
by design. What changes is what resolution *produces* (a `symbol_ref`) and that
graph node ids become `proj:symbol_ref`.

---

## 8. `existing` and `sweep` are deleted; the leaf takes `prior`

`_index_code_file` indexes **one file** but had to answer six questions about the
**whole store**, each arriving as a parameter or as an inference from one:

```
1 has this symbol's body changed?           content_hash gate     ┐
2 what description/keywords exist already?  carry-forward         │ served by `existing`
3 what did this file declare before?        old_in_file → vanished│
4 may I delete what vanished?               _deletion_allowed     ┘
5 may I write into this store's shape?      schema_stale gate     ┐ served by `sweep`
6 who declares the store's shape?           record_schema         ┘
```

**Two of the six stop existing.** Q5's refusal (`codeindexer.py:156-162`) was there
because a one-off write would leave the project half in each shape — the state
nothing downstream could reason about. Per-record `schema` makes a mixed store the
ordinary, *legible* state, which is what `741ae1b` set out to do and never followed
through; there is nothing left to refuse. Q6 moves out of the leaf: `record_schema`
becomes a completion claim written by whatever pass saw every record — the
converter, or the sweep at `:481-484`, which knows it is a sweep *by construction*
rather than via a parameter.

`sweep`'s **only** consumer inside the leaf is that refusal. Delete it and the
parameter has zero consumers.

**And 1–4 dissolve differently** — see below. The leaf becomes:

```python
_index_code_file(root, rel, proj, patch_edges, prior, describe_mode)
```

`prior` **describes the subject** — this file's previous state — rather than
controlling the innards, which is the distinction that made the other two
parameters a layering signal in the first place.

*(This supersedes the earlier answer, an `IndexPass` store session. A pass object
was sized to own the store-wide view and the coverage claim; with Q5 gone, Q6
relocated, and 1–4 needing nothing store-wide, there is nothing left for it to
own. Deleting the parameters satisfies that decision's own general rule more fully
than encapsulating them would have.)*

Replace the store-wide dict with `prior: list[dict]` — this file's previous
entries. The sweep builds the index once:

```python
# codeindexer.py:402
prior_by_file: dict[str, list[dict]] = {}
for e in SymbolIndex(self.paths.project_dir(proj)).all():
    prior_by_file.setdefault(e.get("file", ""), []).append(e)
```

and `:442` passes `prior_by_file.get(rel, [])`. The per-symbol lookup keys on the
identity **parts**:

```python
by_parts = {(tuple(e.get("container") or ()), e.get("name", "")): e for e in prior}
```

`(container, name)` is present in every entry back to schema 0, and within one file
is exactly as unique as `symbol_ref` — which *is* `file` plus a rendering of those
parts. So the cache lookup **never touches a spelling**, and:

- a half-converted store cannot miss its description cache, so the expensive,
  invisible LLM-regeneration failure stops existing rather than being guarded
  against;
- there is nothing to "flip in the same commit as the entry key";
- `existing is None` vs `{}` stops being a distinction anyone can get wrong.

What stays keyed is three lines, not a thread: `vanished` → `store.delete`
(`:283-288`), `_withhold_deletions` (`:288`), and the describe blob (`:296-302`,
which should key by `fqn` — a model cannot echo `crib/app.py#Crib.foo`).

*(Follow-on: with §6's filenames, one file's records share a glob prefix, so even
the one-off path stops needing `store.all()`.)*

---

## 9. What gets deleted

| | why |
|---|---|
| `symmigrate` entirely — `plan_notes`, `apply_plan`, `build_map`, `invariants`, `seen_targets` | conversion derives its own work-list per record |
| the `learnings.py:284` guard | pending ≠ orphan |
| `migrate_symbols`' dry-run/apply dual paths | one path, `--dry-run` prints the work-list |
| `schema_stale()`'s single-file refusal (`codeindexer.py:156-162`) | per-record schema makes a mixed store *legible*, which is what `741ae1b` set out to do; a mixed store is now the ordinary state |
| `existing` and `sweep` (§8) | wrong shape; and with the refusal gone, `sweep` has zero consumers |
| `module`, `parent`, `module_of`, `legacy_fqname` | derivable, or only fed the deleted fields |
| `symbols.match()`'s dead `id` tier | replaced by a real `symbol_ref` / `symbol_was` tier |

---

## 10. Sequence

1. **Delete `existing` and `sweep`** (§8). Independent of everything else; removes
   the regeneration risk from every later step.
2. **Resolver accepts a reference.** Feed `match()` the entry's `symbol_ref`, rename
   the param to `entry_ref`, match **by parts** — split on `#`, `path_matches` the
   left, segment-match the right — so `retrieve.py#foo` and `#foo` also resolve. Add
   the `symbol_was` tier.
3. **One `bindings()`**, and every learning verb through one path. This alone
   revives the five broken notes and the `※` glyph.
4. **Entries carry `schema`; `all()` groups by `key()`** and prefers canonical.
   Do *not* flip `all()`'s filter to `symbol_ref` — that drops every unconverted
   entry, i.e. the whole input. Filter on `file` and `name`.
5. **Move the key**: `_relname`/`_path`/`read`/`delete`/`write`, `by_fq`,
   `drop_file`, `patch_edges`, graph node ids, `resolution()`, error text,
   `_SALVAGE`. Drop `fqname`/`module`/`parent` from the written entry.
6. **`code_convert`**, dry-run by default, re-running *is* the resume. Wire as the
   first step of `project_index`, and as what `schema_stale()` points at.
7. **Local repair script** for this machine's fourteen stores — not shipped code.
8. **Mechanical checks** (§11).

---

## 11. Checks

Every manual version of these has already failed once here, which is the argument
for each being mechanical.

- No module outside `symbols.py` spells a reference — extend
  `test_symbol_shape.py`'s offender-pattern list to `#`-splitting by hand.
- Every entry the extractor writes has a `schema` line — asserted against a
  *rendered* entry, not the dict.
- Every `learning_*` verb resolves its path through one function.
- The `※` join is tested against a **migrated** fixture. The current tests pass
  because they were written against the shape that still worked.
- Round-trip: `ref_slug(symbol_ref)` → filename → parse → same `symbol_ref`.
- `_relname`, `_path`, `read`, `delete`, `by_fq`, graph node ids and edge-target
  resolution never take `fqname`. **This is the demotion**, and it is what the
  field's absence would otherwise have had to guarantee.
- After conversion, a `project_index` regenerates **zero** descriptions.

---

## 12. What is actually released, and who has to be migrated

**Nothing on `dev` is published, pushed or not.** `origin/dev` is sync; `main` is the
released state. Verified:

```
main                          a803641   == merge-base(main, dev)
commits on dev not on main    28
commits on main not on dev    0
v0.7.0 (71ebada) on main?     NO   — tagged on dev, never promoted
950ba3e on main?              NO   — the code_graph edges shape is UNRELEASED
last tag reachable from main  v0.6.1
```

So the whole 28-commit range is freely rewritable, and **v0.6.1 is what users
actually have.** Its entry has exactly one name:

```
fqname · name · kind · lang · module · container · parent · content_hash
file · file_hash · line · mtime · signature · calls · called_by · references · name_terms
```

No `symbol_ref`, no `fqn`, no `scope`, no `schema`, no `SYMBOL_SCHEMA_VERSION`;
learnings under `code-learnings/`.

**That is the whole shipped migration surface: one legacy shape.** The three
populations measured in §1 — four converted, three stamped-but-not, seven unstamped
— are **artifacts of dev builds on this machine**, not states the shipped converter
has to be designed around. It handles them anyway, for free, because the per-record
predicate does not care how a store came to be mixed; but they are not the
requirement, and no compatibility branch should be written for them.

**This machine** is a known finite state repaired by one-off script: re-render every
entry so the stamp exists, rename the four converted stores to canonical names, and
let the notes be picked up by the ordinary converter — their filenames are already
`ref_slug(symbol_ref)` and therefore already right.

**A released (v0.6.1) user** runs one verb: every record converts from the single
legacy shape, a crash at any point leaves the ordinary input to the next run, "half
converted" resolves by running the converter again and **never** by a reindex, and a
note that cannot be converted keeps its binding and is reported as *pending* or
*orphaned* — states the system already models and already has verbs for.

Encoding this machine's specific breakage as a compatibility path in the shipped
converter is rejected: it would make a one-time local mess a permanent branch every
future reader has to reason about.
