# Decisions & plans — the graph that keeps promises

What the design and plan facets are *for*, the model underneath them, and why each
piece is shaped the way it is. The verb-by-verb reference is
[surface.md](surface.md); storage ownership is [storage.md](storage.md).

## 1. Why a facet, and not just notes

A note can hold the prose of a decision. What it cannot hold is the **promise**.

When you settle a design question — "chunks are keyed by X", "writes must name
their project" — the sentence is the least of it. What matters later is what the
decision *rests on* and what *rests on it*: the day one of its foundations moves,
somebody needs to know that this decision, and everything above it, deserves a
re-read. Prose can't fire that. A dependency edge can, and that is the entire
reason the facet exists:

> **a dep is a promise: *if that changes, reconsider this*.**

Everything else follows from taking that promise seriously:

- **Only the facet verbs speak the edges.** `note_*` refuses facet paths outright.
  You *can* edit the file raw (the watcher reindexes; hash-taint still catches the
  drift), but only `design_edit` can answer "…and here is what you just
  invalidated" in the same breath as the change.
- **Every edge checks.** There is no "informed-by" edge that never taints, because
  that would be exactly the hole through which an origin changes silently.
- **Adding is silent.** `design_add` seeds its baseline from its deps *as they read
  now*, so a new decision is born verified and nothing gains taint just because it
  appeared. The graph tracks the edges you declared — not agreement — so it will
  happily hold two contradicting decisions. That is why `add` returns `similar`:
  a near-duplicate forks the graph and splits its dependents between two records;
  `append` or `supersede` the existing one instead.

## 2. The model

Both facets are pillar stores — sibling directories (`design/`, `plans/`) sharing
the note machinery (version ring, git sync, merge driver) but never the note
search scope. Each entry is one markdown file whose frontmatter carries the graph:

```
id:       ULID — the identity; opaque, survives every retitle/move
deps:     [ULIDs]         — the promises
checked:  {dep-ULID: body-hash-at-last-verify}   — the recorded baseline
sources:  [{ref, heading, hash}]                 — attribution, at SECTION grain
status:   design: proposed | active | superseded
          plan:   todo | in-progress | done | verified
rank:     plans only — lexorank; order is preference, deps are correctness
```

Two edge families, deliberately different:

- **`deps` are graph edges.** Body-hash checked. They *gate*: a plan item's design
  dep blocks `plan next` while the decision is tainted or unpromoted.
- **`sources` are attribution edges** — where an entry was *drawn from*. They are
  hashed at **section** grain (`DESIGN.md#10.3 Fusion`, never the whole file), so
  an edit to that one passage re-checks exactly what came out of it and an edit
  anywhere else re-checks nothing. Sources check but never gate: a moved passage
  is worth a look, not a work stoppage. A bare-doc citation is refused with the
  doc's headings listed — whole-file attribution would re-check every entry drawn
  from a DESIGN.md on any edit anywhere in it.

## 3. Staleness — computed, coarse, and cheap on purpose

Nothing propagates on write. Staleness is **recomputed on every read** from the
recorded baselines against the bodies as they are *now* — which is what lets an
edit by any route (facet verb, `note_edit`, your `$EDITOR`, a git pull from
another machine) get caught without anyone tracking changes.

Taint is **coarse by design**: it means *a dep moved*, never *you were wrong*.
Each tainted entry comes back from `check` with the chain that explains it
(`X → Y`, Y being what actually changed), the change kind (`dep-edited`,
`dep-superseded`, `dep-deleted`, `new-unverified-edge`, `source-changed`,
`source-missing`), and the verb to run next. The normal ending is
`design reaffirm` — "I re-read it against what moved; it still holds; re-record
the baseline" — a one-liner, not error recovery.

That cheapness is a deliberate economics choice, not laziness: **if re-reading
were expensive or blame-shaped, nobody would declare deps** — and an undeclared
dep is precisely the silent breakage the facet exists to prevent.

You don't have to remember to check. `crib status` carries a per-project tainted
count, and any retrieval hit that lands on a stale decision carries
`tainted: true` — the warning arrives at the only moment it can change the
outcome: while you are about to reason *from* the stale thing.

## 4. Plans — a todo list that knows when it stops being safe

A plan item is what you write instead of a todo list, because an in-chat list
dies with the chat and a list in your head dies with the context window. The
mixed-dep rule is where it earns its keep — each dep kind gates differently:

| dep kind | blocks when | because |
|---|---|---|
| plan | until done/verified | it is work that must happen first |
| design | while tainted or `proposed` | unstable/unblessed ground is not something to build on |
| note | never | it is a reference, not a gate |

So an item that declares the **decision** it implements drops out of `plan next`
the moment that decision's ground moves — the work stops looking actionable
exactly when it stops being safe to do, and nobody had to ask.

**Statuses are claims, made to other agents as much as to you.** `in-progress`
takes the item out of everyone else's `plan next` (a visible claim on the work);
`done` answers with `unblocked` — the items its completion just freed, the
plan-side mirror of `design_edit`'s `newly_tainted`. A finished item whose cited
source later changes gets a `revisit` flag: the graph reports, it never re-opens
a status behind the human who set it.

**Two stalenesses, two claims.** The *decision's own* taint gates dependents until
someone reaffirms the decision. Separately, each item records every dep's hash
*as it read when the edge was made*, so the **item** shows ⚠︎ stale in lookups
when a decision it rests on is edited — even benignly. These are different facts
and get different verbs:

```bash
crib plan status <ref> done      # a claim about the WORK: it happened
crib plan reaffirm <ref>         # a claim about the GROUND: what this rests on
                                 # moved; I re-read it; the item still stands
```

Clearing a benign ⚠︎ must never require pretending the work moved — nor the old
workaround (dep-remove + dep-add per edge), which was a reaffirm wearing a verb
costume.

## 5. The import tier — extraction is quarantined

`design import DESIGN.md` / `plan import` prepare a doc for extraction: its
sections, each with the exact citable `source` string and current hash, plus what
already cites it, plus the procedure. They run no model and write nothing — the
session LLM does the reading and the judgement; the tool supplies the one thing
it can't derive (correct-by-construction citations).

Extracted decisions land **`proposed`**: they taint nothing (quarantine that
spreads authority isn't quarantine) and they *gate* any plan item depending on
them (unblessed ground is unstable ground of a different flavour).
`design promote` is the human act that makes one `active`, seeding its baselines
fresh. Hand-authored decisions land `active` directly — you already made the
judgement; only extraction quarantines.

## 6. The graphs — one contract, three facets

Both facets export as diagram-ready graphs, under the **same consumer contract
as `code graph --edges`** — one renderer draws all three:

```bash
crib design graph -p myrepo          # the decision map
crib plan graph -p myrepo            # the plan + the decisions items rest on
```

- `{nodes, edges}`; every node an object carrying `id` and `name` — nothing a
  consumer has to string-parse. Facet ids are the pasteable pillar-qualified refs
  (`design:x.md` — the spelling `sources` citations already use), with the ULID
  alongside.
- **edges ⊆ nodes, always**: a dep pointing at a note or outside the export gets
  a lean declared node (`external` / `truncated`), never a bare string endpoint.
- Edge kinds: `dep`, `superseded_by` (old → new), and opt-in `source` attribution
  edges (`--sources`).
- `tainted` on a design node is the facet graphs' equivalent of the code graph's
  learning glyph: computed live at export, never stored.
- `plan graph` includes exactly the design nodes plan items rest on — those edges
  gate, so a plan drawn without them looks self-contained when it isn't — and no
  more of the decision map.

## 7. Resolution & ergonomics

Refs resolve by ULID (or unique prefix), relpath, or title/slug — ambiguity lists
candidates rather than guessing. A ref that exists in the *other* facet says so
("`x.md` is not a design note — it exists as a PLAN item: use the plan_* verbs")
instead of reading like store corruption.

Decisions are usually written *before* the doc of record grows around them, so
citations can be wired **post-hoc**: `design append <ref> "…" --source
"DESIGN.md#7a"` adds citations (deduped) while existing ones keep the hash they
were captured at — that hash *is* their meaning. `design edit --source` remains
the deliberate replace-everything form.

## 8. Where to go next

- [surface.md](surface.md) — every verb, both faces, including the full tables.
- [guide.md](guide.md) — workflows 4 and 5 walk a decision and a plan end to end.
- [storage.md](storage.md) — which store owns which bytes.
- `docs/plans/design-plan-tracking.md` — the original design doc these facets
  were built from (historical).
