# Plan: plan-facet deferral ergonomics — parked/declined with triggers, and the review sweep

Status: **EXECUTED 2026-09-25** — P1/P2/P3 all built, tested, live-verified. The
as-built map is at the bottom; the original design below records intent and the
build-order reasoning (kept: the as-built diverges from it in two recorded ways
— the clean corpus cut is 638-not-824, and triggers watch graph-node refs only). Companion to
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
---

# AS-BUILT MAP (2026-09-25)

Everything below is SHIPPED and tested (780+ tests). Commit map: P1 `318960f`,
P2 `8fb71f6`, P3 `998e456`, retitles + newly-tainted fix `c19a114`, injection
guide `49179ce` (+ pi copy sync `02fd39a`), pi verb watch `bd187b9`, sha stamp +
pinned worktree `b8fc567`, plan_next fired count `8d902a2`, pi-plan renderer
`d4071af` (v0.4.0), clean corpus fixture `1d69f5f`.

## 1. The state machine

| status | set by | plan_next | plan_list | dep gating (mirrored by pi-plan) |
|---|---|---|---|---|
| `todo` | `plan_status` | shows (unblocked) | ready | blocks dependents |
| `in-progress` | `plan_status` | excluded (claim) | active | blocks dependents |
| `parked` | `plan_park` | excluded + fired count | parked group, visible | blocks dependents |
| `declined` | `plan_decline` (why REQUIRED) | excluded | hidden by default (`--all`) | blocks dependents |
| `done`/`verified` | `plan_status` | excluded | hidden by default | satisfied |

Leaving parked/declined clears the trigger records with a warning (re-baseline
on the next deferral). Re-parking an already-parked item is legitimate: fresh
baselines.

## 2. The trigger record — thin by design

```yaml
status: parked            # or declined
trigger:                  # a LIST — several grounds are legal, one is normal
  - ref: designs:reranker-ab-….md   # pillar-qualified, graph node (P1 scope)
    checked: <body_hash>           # PARKED: body-hash watch (fires on any edit)
    why: "…"
  - ref: designs:clean-corpus-….md
    checked_status: todo           # DECLINED: status-watch (narrow firing)
    why: "reconsider only if the ground changes state"
```

- Deliberately NOT in `deps`/`checked`: triggers gate **attention**, never work.
- Fired is **computed live on every read** (`_trigger_state`): stored is only the
  baseline; truth is re-derived from the watched ref's current hash/status.
  Pull-not-push → missed-event immunity, nothing to keep in sync.
- Per-state firing: parked watches the BODY (any rewording is a reason to
  re-look); declined watches the STATUS only (promoted/superseded) — "I reworded
  the rationale" is not a reason to resurrect.
- The searchable half: one dated body line ("> parked 2026-09-24, watching: … —
  'why'"), indexed by `plan_lookup`, carried by exports.

## 3. The cue chain (how an LLM session interacts)

```
plan_next (habitual pickup read) — now carries trigger_fired: N  ← the self-cue
   → plan_list (per-row trigger + fired; the re-evaluation queue)
      → plan_read (dossier: baseline vs moved, change kind, the why)
         → plan_status <ref> todo   ← THE UNPARK: a human claim, performed by
                                       the LLM on the user's behalf
         → or re-park with fresh baselines
plan_add (re-proposal) — the near-dup probe returns parked/declined + status
   + why at the cue moment, with facet state on every probe row
```

## 4. The review sweep — evidence handoff, batched apply

- `plan_review` (read): consolidated export + per-item evidence (age, dep
  state, revisit, trigger state, **overlap probes vs open AND done/declined**
  items) + the contract as `instruction`. No model, no writes.
- The session LLM judges and emits a batch: retitle / move / park / decline /
  merge / edit.
- `plan_review_apply` (write): edge-aware, `reviewed` stamp per touched item
  (surfaced in plan_list rows), per-op report, batch continues past failures.
  **Refused**: `done` (statuses are human claims — the LLM never sets done),
  `delete`/`forget` (merge instead — a decline keeps the item readable).
- Merge: loser's body rides under "merged from" heading; dependents are
  reported instead of wedged.

## 5. The integration chain (store → bus → sidebar)

```
plan writes → CRIB_PLAN_MUTATION_VERBS (now incl. plan_park/decline/
              review_apply) → plan:snapshot bus → pi-plan v0.4.0
```
pi-plan renders deferred states: sink like done, never actionable (`⏸`/`×`,
dimmed), deps still block (crib-consistent). Bus item `status` is a plain
string, so new states degrade gracefully on older consumers.

## 6. Invariants (the constitution, as enforced)

- Every edge checks; triggers gate attention, never work — but they CHECK.
- The graph reports, it never re-opens: fired ≠ unparked; unpark is human.
- The LLM never sets `done` — refused at the apply verb, with the reason.
- Pull-not-push: fired is derived (missed-event immune; nothing to sync).
- The newly-tainted diff is a true delta (`nid in t_after and not in before`)
  — fixed after the first live sweep exposed it riding un-tainted rows.

## 7. The escape hatch — promote the condition into a decision

The trigger record stays deliberately thin because a thin record cannot carry
the three-layer gap (fires on any edit ≠ the semantic condition; the why is one
line; the re-evaluation protocol lives in conversation). When a condition
outgrows one line: **`design_add` the wake condition decision** (what evidence
resolves it, what each outcome means, who decides) and watch THAT decision —
the trigger stays a dumb hash-watch on the decision, the semantics live one
`design_read` away, and the gradient is: body line → decision → (never) a
richer trigger record.

## 8. Known limits (recorded, not hidden)

- Fires on any edit of the watched ref — the proxy, stated openly. Rich
  conditions → promote to a decision (§7).
- A declined dep wedges its dependents forever — visible state, the fix is a
  repoint; deliberate, matches crib's plan-dep gating.
- `trigger_fired` does not ride the bus yet — the sidebar shows parked items
  dimmed but cannot display "woke" (the optional extension).
- Free-text triggers ("when the corpus doubles") are unenforceable — only
  hash/status-watchable refs.
- Trigger refs must resolve to graph nodes (design/plan). Note-watching is a
  possible extension (needs a note body-hash story).
