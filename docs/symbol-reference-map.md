# The symbol reference map

What a symbol is called, which spelling each interface speaks, and where each is
enforced. Current as of the completed `symbol_ref` conversion (all 14 live stores,
~15,000 symbols); the design and its evidence live in
`docs/plans/symbol-ref-conversion.md`.

## 1. One name, one key, one history

    symbol_ref = <repo-relative path> # <declared tail>

| field | what it is | authoritative for |
|---|---|---|
| `symbol_ref` | `path#tail` — **THE KEY** | the store filename, graph node ids, learning bindings, `resolved` echoes |
| `symbol_was` | prior keys, a LIST | resolving anything a symbol used to be called; populated by conversion, empty for symbols first indexed after it |
| `fqn` | the language's OWN qualified name — **THE NAME** | display, the describe prompt, `match_meta` |
| `name` | bare local name | the loosest match tier |
| `scope` | typed list of qualifying terms | narrowing (`path=`/`scope=`/`lang=`) |
| `container` | declared chain, RAW (artifacts included) | ground truth the renderings clean |

`fqname`, `module` and `parent` are **gone from the entry**. `fqname` was never a
name — it was the old key, a synthetic uniquifier that pathified the module for
every language whether or not the language namespaces by path (`rust::src::…` is
not a Rust path); its stored value survives as `symbol_was[0]`. `module` had no
reader but the two deleted fields. `parent` is a truncation of the child's
reference — derivable, and as a stored string it was a foreign key in a retired
spelling.

**Not `scope#fqn`.** The path carries the module part; the tail carries the rest;
neither repeats the other:

| lang | `fqn` | `symbol_ref` |
|---|---|---|
| python | `crib.app.Crib.DEDUPE_WARN_SCORE` | `crib/app.py#Crib.DEDUPE_WARN_SCORE` |
| rust | `cli::commands::start::tests::test_x` | `rust/src/cli/commands/start.rs#tests::test_x` |
| lua | `sharedserver.health.check_binary` | `lua/sharedserver/health.lua#check_binary` |
| zsh | *(identical to the ref)* | `core/functions/zdot#zdot._zdot_help_bench` |
| c | *(identical to the ref)* | `bin/sharedserver-watcher.c#main` |

**For file-scoped languages** (`symbols.FILE_SCOPED`) `fqn == symbol_ref` exactly
— the file *is* the scope. By design, not a coincidence to tidy away.

**`symbol_ref` ≡ `(file, fqn)`**, verified both directions on every converted
entry (5698/5698 each way): the reference is the pair serialized, not a third
concept.

**Uniqueness is checked, not assumed.** "0 collisions in 6626" was falsified at
~15k: two same-named Lua block-locals, told apart only by synthetic containers
(`for in` vs `do end`) that tail-cleaning strips, derive one reference. The
converter detects distinct-identity collisions, converts one, reports the rest
every run, and never stamps the store complete over them. (Upstream fix — don't
index block-scoped locals at all — is an open plan item.)

## 2. Matching: one rule over the canonical run

Every spelling anyone might type is a **trailing run** of one sequence — the
file's path segments (extension stripped, index file dropped) plus the declared
tail segments — with `#` optionally pinning where the path ends:

    file  rust/src/cli/commands/check.rs    name execute
    run   rust · src · cli · commands · check · execute
    old key   rust::src::cli::commands::check::execute   the whole run   → exact
    fqn                  cli::commands::check::execute   a trailing run  → suffix
    reference       …/check.rs#execute  ·  check.rs#execute  ·  #execute → ref
    bare name                                            execute         → name

Measured: 5718/5720 legacy keys are trailing runs of the run; the two that are
not (names built from since-stripped LSP artifacts) resolve **exactly** via
`symbol_was` — a compatibility surface, never a search space, so a retired
spelling cannot re-introduce ambiguity. Tiers are *disclosures* of how much the
caller pinned down, not separate matching strategies.

The recorded `name` is authoritative before any splitting (a legal zsh `git.push`
never splits), and ambiguity always refuses with reference-spelled candidates.
Every handle crib emits — dossier neighbours, graph nodes, xref hits, `resolved`
— is a reference you can paste into the next call.

## 3. Identity: what an id is, and what it is not

- **Note id** — a ULID in frontmatter. Opaque, permanent, survives every rebind.
- **Filenames** — BOTH stores file records at `ref_slug(symbol_ref)` (+`.toml` /
  `.md`). A reference always munges (it carries `/` or `#`), so the disambiguating
  hash is unconditional and *canonical-by-derivation* is a uniform check: a record
  is where it belongs iff its basename is the slug of its own key. A record at any
  other name is a crash leftover, swept by the next write of that entry.
- **Binding** — frontmatter `symbol_ref`, prior spellings in `symbol_was` (notes
  snapshot their entry's bindings at authoring, so an *orphan* stays reachable by
  any name a human ever used for it). The learnings join reads **frontmatter,
  never filenames**, and meets graph nodes at the key (`Learnings.marks`).

## 4. Conversion

One predicate, two stores: `done(r) = schema == TARGET and basename ==
ref_slug(key(r))`; the work-list is derived from the data and re-running is the
resume. `SymbolIndex.write` normalizes identity on every write
(`normalize_identity`), so **conversion is read + write + stamp** — `code_convert`
does it without LSP or LLM, `preserved()` proves per record that descriptions,
keywords and edges came through byte-identical, and a full `project_index`
converts as a side effect at LSP+LLM price. Notes: `learning_migrate`, three
states (`noop` / `convert` / `orphan` — *pending* dissolved because the key is
derivable), collisions kept and reported, never merged. Correctness never depends
on the note conversion running; it buys canonical filenames only.

Operational note: **restart the daemon before converting** — an old-code daemon
cannot see converted stores and its watcher forks them.

## 5. Where this is enforced

| what | where |
|---|---|
| every spelling | `crib/symbols.py` (pure leaf) |
| no spelling outside it | `tests/test_symbol_shape.py` |
| entry shape / persist allow-list | `tests/test_symbol_shape.py`, `codeindex.py` `_SCALARS`/`_ARRAYS` |
| matching | `symbols.match_entry` over `symbols.canonical`; `tests/test_code_graph.py`, `tests/test_symbol_migration.py` |
| canonical filenames + normalize-on-write | `SymbolIndex.write`/`normalize_identity`; `tests/test_symbol_convert.py` |
| only a pass that saw every record stamps | `tests/test_symbol_shape.py` (source-level: the sweep + the converter) |
| description cache survives id changes | `tests/test_code_graph.py::test_the_description_cache_hits_across_an_id_format_difference` (keys on `(container, name)`, no spelling) |
| conversion | `crib/symconvert.py`, `app.code_convert`, `learnings.convert_notes` |
| before/after evidence | `scripts/symstats.py` |
| both faces agree | `tests/test_surface_parity.py` |
