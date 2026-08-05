# Plan: design/plan facet ergonomics — first-class, edge-aware verbs

Status: **EXECUTED 2026-08-05** (approved 2026-08-05). All items landed, plus an
approved mid-build addendum on the plan facet: edge-aware completion
(`plan_status` → `unblocked`), working-set grouping in `plan_list`, batch
`plan_add` with `#n` intra-batch deps and optional bodies, the in-progress =
claimed contract in `plan_next`, and the todo-capture cue in rule 4. The governing
intent below is recorded as a design note in the cribsheet project
(`design/design-facet-is-first-class-and-edge-aware-notes-are-backend.md`), and
the edge principle as `design/every-dependency-edge-propagates-checking.md`.

Governing intent (maintainer): **the facet
is the interface; notes-in-a-dir is backend only and must not leak into the
workflow. The causal/dependency edges are the product — every design verb is
a chance to speak edges.**

## Principle: every edge checks (settled 2026-08-05)

There is one edge semantics for taint: **every dep edge propagates checking**.
An edge kind that doesn't check ("informed-by" that never taints) is the hole
through which an origin changes silently — the exact failure the facet
exists to close. If typed edges ever arrive they may vary *gating*
(plan_next blocking) but never *checking*. Record this as a design note via
the facet (dep on the governing "facet is first-class and edge-aware" note).

## The description layer IS the LLM UX (do this first)

DESIGN §5 precedent: a tool's description doubles as its usage instruction
(`reindex`). Apply to every design/plan verb:

- **State the contract**: one line in each docstring — "staleness is computed
  from dep body hashes; any edit by any path (facet verb, raw file, another
  agent) is caught — never track changes yourself, only `check`."
- **Cue in the docstring, not only global instructions**: design_check =
  "call BEFORE changing a decision or code implementing one"; design_edit =
  "prefer over note_edit for decisions — the result lists dependents your
  change tainted."
- **Check output prescribes its follow-up**: each tainted entry carries ref +
  title, explaining chain, change kind (dep edited / superseded / deleted /
  new unverified edge), the dep's `updated` date, and ends with the action:
  "reconsider, then `design_reaffirm <ref>`; if it no longer holds,
  `design_supersede`."
- **Own the coarseness**: taint means "a dep changed", not "this is wrong" —
  say so in check/reaffirm docstrings so trivial-change reaffirms read as the
  normal cheap case, not an error recovery.

## New verbs (all: both surfaces, registry rows, declared policy, parity green)

1. **`design_read(ref)` — dossier, not file fetch** (code_dossier analog):
   body + status + deps (each annotated: title, status, tainted?) +
   dependents (same) + this node's taint state with explaining chains. The
   one-call orientation before touching anything.
2. **`design_edit(ref, new_content)` / `design_append(ref, content)` —
   edge-aware writes**: perform the edit through the facet, then answer with
   the causal consequences — `newly_tainted: [{ref, via-chain}]` computed
   against the pre-edit state. Hash-taint remains the safety net for raw
   edits; these are the encouraged path.
3. **`design_lookup(query, …)` / `plan_lookup(query, …)`** — thin wrappers
   over notes retrieval scoped to the facet (type filter), hits annotated
   with `status`, `tainted`, dep/dependent counts. The type-as-tag plumbing
   stays; it stops being the interface.
4. **`design_list(project?, tainted?)`** — flat table: title, ref/slug,
   status, tainted flag, dep/dependent counts. `--tainted` filters.
   (Approved explicitly.)

## Changes to existing surface

5. **Bare-noun CLI defaults** (approved explicitly): `crib design` → `list`,
   `crib plan` → `list` (matching `crib project` → status precedent).
6. **`design_verify` → `design_reaffirm`** — same semantics as
   `learning_reaffirm` (re-bless against drift); eliminates the collision
   with plan's `verified` status. Surface is a day old: rename outright, no
   alias. Update registry, parity rows, docstrings, instructions.
7. **Ambient taint markers** (the adoption levers):
   - `status()` gains `design_tainted: N` per the reconciling/
     index_rebuilding precedent (frontmatter scan, cheap; per current
     project or totals across projects — match how status handles projects).
   - `note_lookup`/`design_lookup`/`apropos` hits whose note is a tainted
     design carry `tainted: true` (index_rebuilding marker pattern) — the
     agent retrieving a stale decision is told at the moment it's reasoning
     from it.
8. **`design_add` returns `similar:`** like `note_store` (near-duplicate
   decisions fork the graph — worse than duplicate notes). If the store path
   already computes it, surface it; else add the probe.
9. **CLI body authoring**: `--file <path>` and `-` (stdin) for
   `design add/edit/append` and `plan add`; with body omitted on a tty,
   launch `$EDITOR`. Shell-quoted paragraphs are the worst current friction.
10. **`plan_next` mixed-dep semantics, made explicit** (docstring + test):
    a **design** dep blocks while tainted (untainted = stable ground); a
    plain **note** dep never blocks (reference, not gate); plan deps block
    until done/verified as today.
11. **Instructions route through the facet only**: rule 4 in
    CLAUDE.md.example (and the condensed global bullet) mention
    design_read/design_edit/design_lookup — `note_*` disappears from the
    design workflow entirely. Update docs/surface.md.

## Constraints

- Parity suite must stay green: every verb added/renamed lands on both
  surfaces with registry rows + declared policy in the same change.
- The dogfooded decisions already in the cribsheet project must survive the
  rename (frontmatter is untouched by it — verify).
- Record the governing intent above as a design note (dep on
  "cli-and-mcp-surfaces-stay-paired"): "Design facet is first-class and
  edge-aware; notes are backend" — via the facet itself.
