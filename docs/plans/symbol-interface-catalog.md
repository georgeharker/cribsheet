# Symbol interface — full catalog

Everything that speaks "symbol", and what each needs for the struct interface
(`{id, project, path, scope, name, lang}`). Enumerated from the code, not from memory.

Companion to the design entries *A symbol is a typed struct* and *Two data tiers, two
migration policies*.

## 0. There are THREE symbol spellings in flight today, not one

This is the item most easily missed, because only the first is ever discussed.

| # | spelling | example | where it lives |
|---|---|---|---|
| 1 | qualified name | `crib.app.Crib.code_graph`, `rust::src::core::state::ServerState::exit_code` | `fqname` on every entry; every verb's input and output |
| 2 | **edge ref** | `helper [dep:src/util.py]`, `main [bin/watcher.c]` | **stored inside** `calls`/`called_by`/`references` on every entry |
| 3 | graph node id | `crib.app.Crib` · `llmkit:llmkit.bridge.ChatRequest` · `__init__ [crib/app.py]` | `nodes[].id`, `edges[].from/to` |

Spelling 2 is the deep one: it is *persisted*, it is parsed in five places by string
surgery, and it **leaks to callers** — `code_lookup` and `code_xref` return
`calls`/`called_by`/`references` as raw `name [file]` strings. Spelling 3 mixes three
different syntaxes in one field, distinguished only by punctuation a caller must parse.

Under the struct interface, 2 and 3 both become structured refs. That is a bigger
change than renaming ids, and it is where most of the work is.

## 1. Verbs that ACCEPT a symbol

| verb | param | notes |
|---|---|---|
| `code_xref` | `symbol` | lists every match; narrows nothing |
| `code_dossier` | `symbol` | narrows to one |
| `code_graph` | `symbol` (optional) | narrows to one; omitted ⇒ whole project |
| `learning_add` / `edit` / `forget` / `read` / `reaffirm` | `symbol` | narrow to one; `forget` also accepts an orphan's recorded fqn |
| `learning_rehome` | `old_fqn`, `new_fqn` | **two** symbols, explicitly fqn-typed |
| `code_lookup` | `query` | a concept query, not a symbol — but returns symbols |

**Fix:** phase 2 adds constraint params (`path=`, `scope=`, `lang=`) alongside
`symbol=`. `learning_rehome` needs it most: re-pointing a note at a symbol is exactly
the operation where naming the target by the axis you know matters, and it currently
demands two fully-qualified names.

## 2. Verbs that RETURN a symbol

| verb | shape today |
|---|---|
| `code_lookup` | hits with `fqname`, `kind`, `file`, `line`, + **raw edge-ref strings** |
| `code_xref` | same entry shape, one per match |
| `code_dossier` | `fqname` + neighbours as `{symbol, file, project, description}` |
| `code_graph` | `nodes[].id/fqname`, `edges[].from/to`, `resolved` |
| `learning_*` | `symbol` |
| `learning_report` | rows of `{symbol, status, file}` |
| `learning_rehome` | candidates `{fqname, score, file}` |

**`code_dossier`'s `neigh()` is already 80% of the struct** — `{symbol, file, project,
description}`. It is the existing precedent to generalise from, not a fresh design.

## 3. Persistence keyed by a symbol name

| what | keyed by | tier |
|---|---|---|
| symbol_index entry files | `fqname` (one file per symbol) | **derived** — rebuilt by reindex |
| `calls`/`called_by`/`references` | edge refs (spelling 2) | **derived** — rebuilt |
| `name_terms` (search) | `_name_terms(name, fqname)` | **derived** — rebuilt |
| describe queue entries | `fqname` | **derived** — transient |
| **learning note `symbol:` frontmatter** | qualified name | **authored** — must be re-attached |
| **learning note filename** | `learning_slug(fqn)` | **authored** — renamed with the binding |

Only the last two migrate. Everything else is regenerated, which is the whole point of
the two-tier policy. Note an id change renames ~2200 index files per project — free,
but not instant.

## 4. CLI presentation — six independent renderers

No shared symbol renderer exists. Each emitter formats its own, differently:

| emitter | format |
|---|---|
| `_emit_code` (lookup) | `[rank] kind  fqname` |
| `_emit_code` (xref) | `fqname  (kind)  file:line` |
| `_emit_code_dossier` | `fqname  (kind)  file:line` + neighbour rows |
| `_emit_code_graph` | `fqname (kind) [direction]`, tree rows `fqname tag file:line` |
| `_emit_code_edges` | `depth  id  ×n  file:line  tag` |
| `_emit_code_rehome` | `[score] fqname   file` |
| `_emit_code_report` | `status  symbol  file` |
| `_emit_resolved` | `'query' → fqname, matched on via` |

Adding a display form and module adornment means touching all eight, or introducing
one renderer. **Introduce the renderer.**

Proposed: a single `render_symbol(ref, style)` taking the struct, with styles for the
contexts that actually differ — `inline` (one line with location), `id` (canonical,
for pasting), `tree` (a graph row), `hit` (ranked list). Adornment of the
path-derived part lives there and nowhere else, so "CLI shows both" is one decision
in one place rather than eight consistent edits.

## 5. Work list, in dependency order

1. **phase 1** ✅ `scope_of` — done, unwired
2. wire `scope` into indexing + store it (reindex; `id` untouched)
3. **presentation layer** — `render_symbol(ref, style)`, adopted by all eight emitters
   with today's output preserved, so the later change is a one-line switch
4. structured refs for spellings 2 and 3 — edge refs and node ids
5. phase 2 — constraint params on the wire
6. phase 3 — `group_by` as an axis choice
7. phase 4 — move the id, re-attach learnings

Step 3 is deliberately early and behaviour-preserving: it is the cheapest point to
introduce the seam, and doing it before step 4 means the id change is one edit rather
than eight.
