# Plan: plan-facet deferral ergonomics — parked/declined with triggers, and the review sweep

Status: **DESIGN 2026-09-24** — sketched and discussed; not yet built. Build order
and open decisions at the bottom. Companion to
[`design-facet-ergonomics.md`](design-facet-ergonomics.md) (executed 2026-08-05),
whose principles this inherits wholesale.

## Governing intent

The plan facet today has `todo / in-progress / done / verified` — a vocabulary
with no answer for items that are **neither done nor actionable**. They linger in
`todo` forever (littering `plan_next` and the working set) or get dishonestly
marked `done`. Two designs close the gap, and they compose:

1. **`parked` / `declined`** — statuses for "resolved-in-spirit": not now, with a
   stated reason, remembered with a trigger that surfaces re-evaluation.
2. **The review sweep** — a full-plan evaluation where the session LLM retitles,
   merges, reorders, and — the payoff — parks/declines litter with evidence.

Both extend the facet's founding rule unchanged: **the graph reports, it never
re-opens.** A trigger fires the *re-evaluation*; only a human claim moves a
status.

## Status matrix (complete)

| status           | meaning                              | `plan_next` | surface                                        |
| ---------------- | ------------------------------------ | ----------- | ---------------------------------------------- |
| `todo`           | actionable                           | shows       | ready group                                    |
| `in-progress`    | claimed                              | excluded    | in-progress group                              |
| `parked`         | not now; **trigger** may fire        | excluded    | parked group + "trigger fired" banner          |
| `declined`       | never scheduled; remembered why-not  | excluded    | declined group, hidden by default (`--all` shows) |
| `done`/`verified`| the work happened                    | excluded    | done group (hidden)                            |

All states are VISIBLE state — `plan_list` counts them, exports include them
inline. A state exam that hides parked or declined items would lie about the
plan.

## P1 — `parked` status + trigger (build first; the machinery lands here)

### Semantics

`plan_park <ref> [--trigger <ref>] [--why "..."]` (or `plan_status <ref> parked`
with optional trigger). Parked = a *dis*-claim: the item is not work now, with a
stated reason. It never blocks and never shows in `plan_next`, exactly as
`in-progress` doesn't.

### The trigger is a dep edge wearing deferral semantics

One new mechanism serves parked, declined, and every future "re-evaluate me
when X moves" state:

```yaml
status: parked
trigger:
  ref: designs/reranker-ab-on-the-clean-824-....md
  checked: <body_hash at park time>   # the existing edge baseline
  why: "the reranker decision settles whether this is still the live lever"
```

The trigger reuses `checked` (hash-at-edge-time) + the taint machinery:

- **Fired** = the trigger ref's body hash no longer matches `checked`. Surfaced,
  not acted on: `plan_list` shows the parked group with a "trigger fired" banner;
  `plan_status` counts fired triggers; `plan_read` answers "watching X, baseline
  hash, **fired (changed <date>)**" in one call.
- **Unparking stays human** (`plan_status <ref> todo`) — the same reason `revisit`
  never re-opens `done`. The graph reports; the claim is the human's.
- All three natural trigger kinds collapse into one mechanism, because they are
  all "the thing I was waiting on changed": a design decision moving (body
  hash), another item completing (frontmatter change), a decision being
  promoted/superseded (status → frontmatter hash).

**What a trigger cannot be**: free text ("when the corpus doubles"). Only
hash-watchable refs are enforceable; a wish in prose goes in the body and waits
for a human or a sweep to spot it. Stated as a limitation, not hidden.

### The meaning half: one appended body line makes it searchable

Frontmatter isn't retrieval-indexed; the body is. `plan_park --why` appends one
dated line to the item's body:

> parked 2026-09-24, watching: `designs/reranker-ab….md` — "re-evaluate once the
> reranker A/B settles"

That line makes the trigger **semantically searchable** (`plan_lookup "what is
parked waiting on the reranker"` → the item, with status + inline why), carries
it into exports, and keeps the searchable prose and the firing mechanism the
same object — they cannot drift apart. (An edge-aware write like any other:
appending reports what the edit tainted.)

### The most important consumer is the near-dup probe

Re-proposing a parked/declined idea is the exact disease this cures. `plan_add`'s
near-dup probe extends its answer: *"similar item exists — **parked**, waiting on
`design:reranker-ab…` (fired 2026-10-02), why: '…'"* at the moment of
re-proposal. You don't have to remember the item exists; the probe tells you
precisely when you were about to duplicate it.

## P2 — `declined`: terminal-with-rationale

`plan_decline <ref> --why "..."` — a status, not a note, and **in the plan facet
deliberately**:

- **The lifecycle lives there** — deps, rank, history. Moving declined items to
  the design facet orphans both.
- **The near-dup probe is the payoff.** `plan_add` on a declined idea returns
  *"declined 2026-09-01: 'no — the export covers this; revisit only if
  multi-target gold exists'"* at re-proposal time. The plan becomes the place
  where "we already said no to this" is retrievable — antibodies against
  re-litigation.
- **Required rationale.** A decline without a why-not is litter; refuse it (same
  rule as `design_add` refusing a bodyless decision).
- **Retrieval participation.** `plan_lookup` finds declined items, status + reason
  inline: "should we do X?" surfaces "we declined X because Y" instead of silence
  that invites re-proposal. The status annotation does the work: the hit itself
  says *settled, don't re-litigate*.
- **Sibling of `supersede`, not a synonym**: supersede = "replaced by *that*";
  declined = "replaced by *nothing, on purpose*". Both terminal, both readable,
  both carrying rationale as the payload. `plan_forget` still requires
  confirmation; declining is the *articulate* way to stop tracking.
- **Narrower firing**: a declined item's trigger fires on **status transitions**
  of the watched ref (promoted/superseded/edited-status), not prose rewording —
  "I reworded the rationale" is not a reason to resurrect. Per-status firing rule
  on top of the shared hash watch; cheap to get right the first time.

## P3 — the review sweep: evidence handoff, batched apply

The shape follows `design_import`'s precedent: **crib runs no model and writes
nothing — it hands over evidence, the session LLM judges, a structured batch
applies.**

### Phase 1 — `plan review` (read; MCP `plan_review`)

One call returns: the consolidated export doc (the dep-topological state exam
`plan export` already produces); per-item evidence (age since created/updated,
dep state, overlap probes against open **and done** items — a duplicate of a done
item is the louder one: the work regressed or the item was mis-scoped;
`revisit` flags; parked/trigger states); and an `instruction` field carrying the
review contract (below).

### Phase 2 — the session LLM emits an edit batch

Structured items, each evidence-backed:

- **retitle** (clarity; the title is the plan's own surface)
- **merge** (duplicate onto the better-worded one; the loser is forgotten or
  superseded)
- **reorder** (rank assignments)
- **dep add/remove** (where the evidence says structure changed)
- **park with trigger** / **decline with why-not** — the verdicts that give the
  sweep teeth: it doesn't tidy the list, it writes down *why things were not
  done*, the knowledge that otherwise evaporates and gets re-litigated quarterly

### Phase 3 — `plan review --apply <batch>` (write, edge-aware)

Applies the batch; every touched item is stamped `reviewed`; the apply report
answers per edit what it tainted (`plan_edit`'s shape). **Permission line — the
load-bearing rule: the LLM may never set `done`.** `done` means *the work
happened*, a claim only the actor who did it can make (the same reason
`plan_next` excludes claimed items). The LLM proposes ("looks complete,
evidence: X") and the human confirms. Titles/order/parks/declines apply
directly — reversible, edge-aware, stamped. Status proposals ride the report.

## P4 — composition and the export loop

The sweep's most common verdict on a littering item is **park-with-trigger**, not
delete; and the *next* sweep reads parked items whose triggers fired as its
re-evaluation queue. Over time the committed `STATE-plans.md` diff becomes the
review audit trail: the plan stops being an append-only graveyard and becomes a
maintained document whose history is readable in git.

## Build order

1. **P1 `parked` + trigger** — one status, one frontmatter record, one body line,
   one surface group; zero new graph machinery (the trigger is a dep edge with
   deferral semantics).
2. **P2 `declined`** — same trigger field (narrower firing), required reason,
   probe + retrieval participation.
3. **P3 review sweep** — by then the LLM's verdict vocabulary is complete.

Each independently landable; P1 unlocks P3's most valuable verdict.

## Constraints (inherited)

- **Every edge checks.** Triggers gate nothing in `plan_next` — they are
  attention, not blocking — but they *check*: a moved trigger ref is surfaced,
  never silent. Typed-edge caveat from the edge principle: trigger edges may vary
  gating, never checking.
- **The graph reports, never re-opens.** Firing surfaces; only a human moves a
  status.
- **Parity**: every verb on both faces, registry rows, declared policy;
  `test_surface_parity` stays green.
- **Descriptions are the LLM UX**: each new verb's docstring states its contract
  and cues its use (the design-facet-ergonomics precedent).

## Tests

- parked items: excluded from `plan_next`, grouped in `plan_list`, counted in
  `status`, present in exports; unpark is human.
- trigger fires when the watched ref's body changes (all three trigger kinds:
  design body move, plan completion, decision promotion/supersede); fired is
  surfaced, not acted on; `plan_read` explains (baseline hash, what changed).
- declined: refuses an empty reason; probe returns declined+why on re-proposal;
  retrieval hits carry status + reason; export includes them.
- sweep apply: batch applies atomically per edit with per-edit taint reports;
  `reviewed` stamps present; an attempt to set `done` in a batch is refused with
  the reason (statuses are human claims); LLM-generated batches against a real
  plan are idempotent under re-run.