# Plan: doc import & source attribution for the design/plan graph

Status: approved 2026-08-05, queued behind the ergonomics batch (it extends
`designs.py`'s data model — sequential). Prereqs from that batch: batch add
with intra-batch dep refs; the description-layer conventions.

Governing intents (maintainer, 2026-08-05):
- The graph's real corpus already exists in docs (DESIGN.md, plan.md,
  docs/plans/*) — there must be **a way to instruct the LLM to import** plan
  and design entries from them: LLM-driven creation, not a mechanical parser.
- Entries **reference source docs for attribution** — and per the settled
  "every edge checks" principle, those references are live: a changed source
  re-checks what was drawn from it.

## Data model

### `sources:` — attribution edges (both facets)

```yaml
sources:
  - ref: sources/cribsheet/DESIGN.md     # note relpath OR in-situ doc key
    heading: "10. Stack/10.3 Retrieval"  # heading_path; optional (whole doc)
    hash: "ab12…"                        # section_hash at capture/reaffirm
```

- Two edge families, unified semantics: **deps** (graph edges, body-hash
  checked, gate `plan_next`) and **sources** (attribution edges,
  section-hash checked, never gate). Both check; only deps gate.
- Section granularity is load-bearing: whole-file hashing of DESIGN.md would
  churn every entry on any edit; `section_hash` (already in chunk metadata)
  re-checks an entry only when *its* section moved.
- `design_check` gains taint kind `source changed` with the doc + heading in
  the explaining chain; `design_reaffirm` re-records source hashes alongside
  dep hashes. Missing section (heading renamed/removed) ⇒ tainted with kind
  `source missing`.
- **Uniform across facets**: plan items' sources check too. A `done`/
  `verified` plan item with a since-changed source gets a `revisit` flag in
  `plan_list --all` output — the graph reports, it never re-opens status.
  (This makes the revisit-flag semantics from the plan discussion mandatory;
  same machinery, falls out free.)
- CLI: `--source "docs/DESIGN.md#Retrieval"` on add/import flows; resolution
  matches a heading-path suffix uniquely or errors listing candidates.

### `status: proposed` (design facet)

The status enum opens: `proposed | active | superseded`. Imported/extracted
entries land as **proposed** — quarantine-tier logic applied to the graph:
LLM-extracted decisions must not carry the authority of hand-recorded ones.
- `proposed` entries never taint-gate dependents and are rendered distinctly
  (dim/`?` glyph) in tree/list.
- Promotion is explicit: `design_promote <ref>` (proposed → active; seeds
  `checked` fresh). `design_add` keeps landing as `active` — authoring is
  already a human/agent decision; only *extraction* quarantines.

## Import verbs — the description IS the procedure

`design_import(relpath, project?)` / `plan_import(relpath, project?)` — they
run no model. The result carries:
1. the doc split into sections, each with `heading_path` and current
   `section_hash` (ready to cite as `sources` entries verbatim);
2. any existing graph entries that already cite this doc (dedupe context);
3. **the extraction procedure as the result's instruction** (the crib
   pattern — description doubles as instruction): for each decision /
   actionable item found: batch `design_add`/`plan_add` with (a) deps between
   the extracted entries, (b) `sources` citing the exact section drawn from,
   (c) design entries as `proposed`; prefer `dep_add` onto existing entries
   over near-duplicates (`similar:` is returned on add); finish by reporting
   the resulting subgraph (`design_tree` / `plan_list`).

The session LLM does the reading and judgment — this session's plan.md →
docs/plans/* flow is the reference workflow. An MCP prompt wrapping the same
procedure is optional sugar later; the verb form works on every client path.

Bridge-driven automation (server-side extraction via llmkit bridge, as
`distill` does) layers on later with no data-model change: same verbs, same
provenance, `proposed` landing tier — just a different driver. Out of scope
here.

## Wiring checklist

1. `designs.py`: `sources` parse/serialize; section-hash lookup against the
   store's chunk metadata (by relpath+heading; fall back to hashing the
   section text via `chunk`'s splitter for unindexed targets); `_taint`
   extension (source changed/missing kinds); `proposed` status + promote;
   revisit flag derivation for finished plan items.
2. Verbs (both surfaces, registry rows, policies): `design_import` /
   `plan_import` (read policy), `design_promote` (read policy, ref-keyed);
   `--source` on the add/edit CLI verbs.
3. `check`/`read`/`list`/`tree` render source attribution + the new taint
   kinds; docstrings per the description-layer conventions.
4. Instructions: rule 4 gains the import cue — "asked to capture a design
   doc or plan file into the graph? `design_import`/`plan_import` and follow
   the returned procedure."
5. Tests: source-hash taint (edit the cited section → tainted with kind;
   edit elsewhere in the doc → clean), missing heading, promote flow,
   revisit flag on done-with-changed-source, import result shape (sections +
   hashes + existing citations + instruction), uniqueness errors on
   ambiguous `--source`.
6. Dogfood: run `design_import` on DESIGN.md §4 (coordination model) and
   §10.3 (retrieval fusion) for real — the extracted, promoted decisions
   with real `sources` become the living proof, and future DESIGN.md edits
   start tainting them.
