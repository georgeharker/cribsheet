# Custom facets

A **facet** is a kind of item crib stores: notes, design decisions, plan items
and learnings are crib's own four. You can declare more, for workflows,
review findings, tickets, incidents, anything that is a markdown file with a
title, a body and some frontmatter, and crib gives it the same machinery as
its own: storage with version history, search, verbs as MCP tools and CLI
commands, its own tool set, and, if you want them, typed fields, links to
other items, statuses and a working set.

Nothing here is code. A facet is a **spec**: a few lines of TOML naming it,
listing the **aspects** it has (behaviours the engine implements once), and
declaring its own fields. crib's own facets are specs too (the shipped
`crib/facets.toml`), built from the same templates as yours.

- [Quick start](#quick-start)
- [The facets file](#the-facets-file)
- [Aspects: what a facet can do](#aspects-what-a-facet-can-do)
- [Typed fields and queries](#typed-fields-and-queries)
- [Graph facets: statuses, transitions, links](#graph-facets-statuses-transitions-links)
- [Mapping a facet from a repo](#mapping-a-facet-from-a-repo)
- [Keeping a pillar in the repo](#keeping-a-pillar-in-the-repo)
- [Conformance: `crib facets sync`](#conformance-crib-facets-sync)
- [Tool sets](#tool-sets)
- [How agents learn about your facets](#how-agents-learn-about-your-facets)
- [Names crib keeps for itself](#names-crib-keeps-for-itself)
- [Spec reference](#spec-reference)
- [Examples](#examples)

## Quick start

```bash
crib facets init                # writes a commented starter facets file
$EDITOR ~/.config/crib/facets.toml
```

Uncomment (or write) a facet:

```toml
[routine]
plural = "routines"
aspects = ["core", "enriched"]
about = "A routine is a job an agent runs on a schedule. Use it for recurring work; a one-off fact is a note."

[routine.fields]
schedule = "str"
timeout = "int"

[routine.when]
add = "Put the schedule in `schedule` (e.g. \"7am weekdays\")."
```

Then:

```bash
crib facets sync                # shows what the store needs (a dry run)
crib facets sync --apply        # does it
# restart the daemon (it reads the facets file at startup)
crib routine add "Brief me on the weather" --title "Morning briefing" --schedule "7am"
crib routine lookup "weather"
```

An agent sees `routine_add`, `routine_lookup`, `routine_list` and the rest as
MCP tools, with `schedule` and `timeout` as typed parameters.

## The facets file

The **system facets file** is `~/.config/crib/facets.toml` (`$CRIB_CONFIG_DIR`
if set; `$CRIB_FACETS_FILE` overrides the path). It is separate from
`config.toml` and is read **once per process, at startup**: after editing it,
run `crib facets sync`, then restart the daemon.

| command | what it does |
|---|---|
| `crib facets init [--force]` | write a starter file: guidance, commented examples, crib's own facets as a commented reference. Everything is commented, so it declares nothing until you uncomment an example. Refuses to replace an existing file without `--force`. |
| `crib facets show` (or `list`) | every facet, built-in and declared, with its aspects, fields, disabled verbs and its state in this store. |
| `crib facets show --toml` | the effective registry as a facets file (crib's own facets exactly as shipped, then yours): an export to read or start from. |
| `crib facets sync [--apply] [--drop] [--shared]` | bring the store in line with the declared facets; see [conformance](#conformance-crib-facets-sync). |
| `crib facets instructions` | the agent-facing section on this machine's declared facets (markdown), as the server and plugins add it; see [how agents learn about your facets](#how-agents-learn-about-your-facets). |

A bad file never stops crib: it warns once and serves the built-ins
(`crib facets show` says why).

One table per facet:

| key | meaning |
|---|---|
| `[<noun>]` | the facet's name, singular: the item kind and the verb prefix (`routine_add`). A lowercase identifier. |
| `plural` | its tool set (`--tools routines:ro`). |
| `store` | the directory its items live in under a project (default: the plural). |
| `aspects` | what it can do (below). `core` is implied. |
| `[<noun>.fields]` | its own typed fields (below). |
| `disable` | verbs it doesn't want, e.g. `["import", "move"]`. Checked against the verbs its aspects give it. |
| `about` | what one of these items is, and when to reach for this family rather than notes. Added to every one of its tool descriptions, after crib's own wording (an agent decides which tool to call from these). |
| `[<noun>.when]` | `verb = "wording"`: extra wording for one verb's tool, e.g. `add = "Name the schedule in the title."` |

Every key, with its type and default, is in the [spec reference](#spec-reference).
crib's own facets can't be changed from this file yet (only declared ones).
`about` and `when` change only what agents read: no `crib facets sync` is
needed, just a daemon restart.

## Aspects: what a facet can do

Each aspect is a behaviour the engine implements once, bringing its own data
and verbs.

| aspect | what it brings | verbs |
|---|---|---|
| `core` (always) | body, title, tags, id, version history | `add`, `read`, `edit`, `append`, `forget`, `lookup`, `apropos`, `locate`, `versions`, `restore`, `reindex`, `import`, `move` |
| `enriched` | LLM-made per-section summaries and keywords (they make paraphrased searches hit) | `summarize`, `elaborate`, `distill` |
| `anchored` | each item is about a code symbol (as learnings are): it shows on `code_dossier`/`code_lookup` under the facet's name, and goes stale when the symbol's code changes | replaces core: `add`, `edit`, `forget`, `read`, `reaffirm`, `migrate`, `report`, `rehome` (items are named by `symbol`, not by file) |
| `links` + `status` | a **graph facet**: dependencies, staleness, statuses ([below](#graph-facets-statuses-transitions-links)) | `add`, `read`, `edit`, `append`, `lookup`, `list`, `forget`, `dep_add`, `dep_remove`, `reaffirm`, `graph`, one verb per transition |
| `progress` (needs `status`) | a working set: what's done, what's ready | `status`, `next`, `review`, `review_apply` |
| `ordered` | a rank among the facet's items | `move` (and `add` takes `after`/`before`/`items`) |
| `supersede` (needs `status`, `links`) | an item replaced by another; what rested on it goes stale | `supersede` |
| `sources` | citations of doc sections, and the extraction procedure | `export`, `extract` (it hands an agent a procedure built from the facet's own abilities: with a gated status, add each with `proposed=True` for a human to confirm; else, if `ordered`, one batch with `#N` deps, otherwise one add per item; it names only the facet's own tools) |

Every facet has `list`: its items with no query (relpath, title, tags; `-k`,
`--tag`). A facet with declared fields can also filter it by them.
`crib project items` (MCP `project_items`) lists a project's items across
every facet at once, `--kind` to narrow it.

Some combinations are refused because the engine would serve nothing for
them: `links` without `status` (or the reverse), `ordered` or `sources`
without both, and `anchored` or `enriched` on a graph facet. `disable` names
verbs the facet really has (a graph facet's include its transitions).

## Typed fields and queries

```toml
[ticket.fields]
changeset_id = "str"
severity = "int"
due = "date"        # ISO YYYY-MM-DD
```

- **Setting.** Each field is a typed, optional parameter on `add` and `edit`
  (`ticket_add(content=..., severity=3)`, `crib ticket add ... --severity 3`).
  On `edit`, the fields you don't pass are left as they are. Values are written
  into the file's frontmatter, which stays the truth; a wrong type is refused
  before anything is written.
- **Listing by tag.** `list` shows each item's tags (its own, plus its
  `type`), and `tags=["periodic-task"]` (`--tag periodic-task`) keeps only
  items carrying any of them.
- **Hand-written values.** A field is read from each file's frontmatter (for
  a mapped directory too). A value that doesn't fit the field's type
  (`timeout: 30s` on an `int` field) is indexed as empty, so filters on that
  field miss the item; `crib project doctor` names the file, the field and
  the fix.
- **Equality.** On `lookup` and `list`, a field parameter is an equality
  filter (`ticket_list(changeset_id="abc123")`). On `lookup` the search runs
  over the matching items only.
- **Operators.** `where=[{field, op, value}]`: `== != < <= > >=` for `int` and
  `date`, `== != re` (a regular expression, searched) for `str`. On the CLI:
  `--where 'severity>=3' --where 'changeset_id re ^abc'`. An item without the
  field matches no condition.

Fields work on note-like and anchored facets; a graph facet can't declare
them yet (its verbs don't set or filter them). Types are `int`, `str` and
`date`. Field names can't be one of
[crib's reserved names](#names-crib-keeps-for-itself), since they become
parameter names and frontmatter keys.

## Graph facets: statuses, transitions, links

A facet with `links` and `status` behaves like decisions and plans. Its
statuses are yours to name; each plays a **role** the engine implements:

| role | behaviour |
|---|---|
| `open` | live; may be the initial status |
| `claimed` | someone is on it: listed first in `next` |
| `gated` | blessed by nobody yet: its changes make nothing stale, and what `rests_on` it is blocked (a decision's `proposed`) |
| `paused` | carries a revisit trigger; visible, never "pick me" |
| `dropped` | carries a revisit trigger; satisfies nothing, hidden by default |
| `done` | satisfies `follows`; if a cited source moves, it gets `revisit`, never reopened |
| `retired` | superseded: dependents go stale and point at the replacement |

```toml
[incident]
plural = "incidents"
aspects = ["links", "status", "progress", "ordered"]

[incident.status]
values = { open = "open", mitigated = "claimed", resolved = "done", shelved = "paused", wontfix = "dropped" }
initial = "open"
transitions = { shelve = "shelved", wontfix = "wontfix" }   # each a verb

[incident.edges]          # the default link kind toward each target facet
incident = "follows"
"*" = "informed_by"
```

- **Transitions** become verbs (`incident_shelve`, `crib incident shelve`),
  shaped by the role they move to: to `paused` or `dropped` they take a `why`
  and revisit `triggers`; to any other role, just the item. A transition
  moves an item from wherever it is: out of a `gated` status it ungates it
  (re-recording what it rests on as of now), into a `done` one it answers
  with what that unblocked, and an item already there is refused. A facet
  without `progress` has no `status` verb, so its transitions are how its
  items move. `retired` is reached only by `supersede`, never a transition.
- **Links** have three kinds: `follows` (ordering: waits until the target is
  done), `rests_on` (grounding: blocked while the target is gated or stale),
  `informed_by` (context: never blocks). `edges` sets the default per target
  facet; `dep_add --kind` overrides it. Without an `edges` table, links
  between the facet's own items are `follows` if it has `progress` (and
  `rests_on` if not); toward plans `follows`, decisions `rests_on`, anything
  else `informed_by`.
- **Staleness** is the engine's, for every graph facet: an item records each
  dependency's hash as it read it; a dependency's body changing, being
  retired, or a cited section moving makes it stale; `reaffirm` says you
  re-read it and it still holds.

Two more keys: `report_taint = true` makes the facet *report* staleness (a
marker on search hits, `check` and `tree` verbs), as decisions do;
`export_with_deps = true` makes its export include what its items depend on,
as plans do.

## Mapping a facet from a repo

A facet's items normally live under the project in crib's data directory. A
repo can map a facet onto a directory of its own in its `.crib`:

```yaml
project: toucan
facets:
  routine: { dir: workflows, glob: "*.md" }
```

| key | meaning |
|---|---|
| `dir` | repo-relative; confined to the repo. A directory of the facet's own: not the repo root, not inside `.git`, not overlapping another mapping or the `store:` dir (such an entry is warned about and ignored). |
| `glob` | which files under `dir` are the facet's (default `**/*.md`). A writable mapping's glob is `*<suffix>` or `**/*<suffix>` ending in `.md` (`**/*.dec.md`), and new items are named to match (`<slug>.dec.md`); a read-only one can be any glob. |
| `read_only` | indexed and searched, never written: write verbs refuse with the reason (edit the files in the repo; the watcher picks it up). |
| `ids` | `stamp` (default): crib writes an `id:` into each file's frontmatter on first index, so history and links survive renames. `derived`: the id comes from the file's path and crib writes nothing; a rename is a new item. |

- crib records the mapping when it next reads the `.crib`
  (`crib project setup`, `crib project index`, `crib project adopt`), in the
  project's machine-local `.cribproject`, so the daemon and watcher find it
  from anywhere.
- A mapped directory is the facet's own: `docs:` globs and code sweeps skip it.
- **Read-only + stamped** (a read-only checkout of a repo whose writable
  checkout stamps ids): files with an id are indexed; a file without one yet
  is skipped, never given a temporary id, and `crib project doctor` lists it
  as "not yet stamped" until a writable checkout stamps and commits it.
- **Read-only + derived**: every file is indexed and not a byte is written.
- A mapped facet's version history lives beside its files (`<dir>/.versions/`,
  gitignored there).
- Frontmatter must be YAML (`---` fences). Until format-preserving writes
  land, crib's writes (an id stamp, an edit) re-render the frontmatter block:
  for hand-authored files shared with another tool, `ids: derived` and
  `read_only: true` keep crib from writing them at all.

### Frontmatter crib reads in a mapped file

A mapped file is often read by another tool too. crib gives meaning only to
these keys, and leaves every other key alone (toucan's `name`, say); a tool
sharing the files should not use them for something else:

| facet | keys crib reads |
|---|---|
| any | `title`, `tags`, `type` (only as an extra tag: `tags=["periodic-task"]` finds it), the facet's declared fields, and `id` only under `ids: stamp` (under `ids: derived` the id is always the path's, whatever the file says) |
| graph facets | also `status`, `deps`, `dep_kinds`, `checked`, `sources`, `superseded_by`, `trigger`, `reviewed`, `rank`, `created`, `updated` |
| anchored facets | also `symbol_ref`, `symbol_was`, `file`, `signature`, `lang` |

`docs:` is the notes facet mapped read-only, with derived ids, under
`sources/<repo>/`; `facets: {note: {dir: docs, glob: "**/*.md"}}` means the
same.

## Keeping a pillar in the repo

crib's own decisions, plans and learnings can be mapped the same way, one
pillar at a time (`store:` still moves every pillar at once):

```yaml
facets:
  design: { dir: decisions }
```

A built-in pillar maps writable, with stamped ids. Its existing items don't
move on their own: until they do, the pillar is **pending** (its verbs refuse,
naming the fix, and `crib project doctor` reports "waiting to be moved").
`crib project adopt` (in the repo) moves them, with their ids and history;
`crib project release` moves them back.

- If the mapped directory already holds items (a teammate committed theirs,
  or an earlier adopt was interrupted), adopt merges the two sets, provided no
  file would be overwritten. A relpath on both sides refuses, moving nothing.
  The same item on both sides (two files, one id) is reported after the move
  (`duplicate_ids`, and `crib project doctor`) for you to keep one copy.
- A refused adopt changes nothing: every check runs before any file moves,
  and a move that fails part-way puts back what it moved.
- Files under the pillar's directory that aren't markdown stay where they
  are, and adopt lists them.
- Removing (or moving) a mapping in `.crib` while its items are still in the
  mapped directory would strand them: crib keeps the recorded mapping and
  says so, until the items are where the new mapping points (`crib project
  release` moves them back to crib's data dir; `git mv` moves them to a new
  dir), or you remove them to start afresh. Other markdown already in the new
  dir (a README) doesn't count as the items, and isn't stamped.
- Adopt refuses items under crib's data dir that the mapping's glob wouldn't
  take (moved, the facet couldn't see them), and never overwrites a file.
- While one facet waits to be moved, only its own verbs refuse: the rest of
  the graph keeps working (a dependency on a waiting item just doesn't
  resolve until it moves).

## Conformance: `crib facets sync`

Each declared facet has its own table in the store. crib never creates,
changes or drops one behind your back: a facet that is new, or whose
definition changed, is **pending** until you run `crib facets sync --apply`.
Until then its verbs refuse with what changed (for example "fields added
owner; retyped severity int -> str") and the command to run; every other facet
works.

`crib facets sync` prints the plan; `--apply` carries it out in one
transaction:

- **new**: its table is created;
- **changed**: its table is rebuilt and its files re-read (nothing is
  re-embedded);
- **removed** from the facets file: kept until you add `--drop`, which drops
  its table and items; your files are never touched.

Then restart the daemon.

Two guards:

- While the facets file **fails to load**, sync refuses: crib is then serving
  the built-ins alone, and every declared facet would look removed.
- On a **shared store** (the crib DuckDB server), a sync changes the store for
  every machine using it, so `--apply` also needs `--shared`. Make every
  machine's facets file say the same first, then restart each daemon. The store is a cache of your files: a field you
remove keeps its values in the files, and comes back if you add it again.

## Tool sets

Each declared facet is its own tool set, named by its plural:
`crib serve --tools notes,routines` or `[tools] expose` in `config.toml`;
`routines:ro` exposes only its read verbs (`lookup`, `apropos`, `read`,
`locate`, `versions`, `list`).

## How agents learn about your facets

An agent picks a tool from two things it reads: each tool's description, and
the instructions it gets at the start of a session.

- **Tool descriptions.** Each of a facet's tools is described by crib's own
  wording for that verb, then the facet's `about`, then any
  `[<noun>.when]` wording for that verb, then its fields. Write `about` for
  the moment an agent is choosing: what one of these items is, and when it
  belongs here rather than in a note.
- **Session instructions.** crib's own facets are covered by the directive
  the plugins ship (`CLAUDE.md.example`). Your facets exist only on the
  machines that declare them, so their section is generated where they run:
  `crib facets instructions` prints it (each facet's tools, `about`, shape and
  fields; nothing if you declare none). The MCP server appends it to its own
  instructions, and the Claude Code, OpenCode and Pi plugins append it to
  their directive at session start. A facet still waiting for
  `crib facets sync --apply` is marked as not ready.

Both are read when the daemon or a session starts: after editing the facets
file, restart the daemon and start a new session.

## Names crib keeps for itself

- **Facet names and plurals** can't be: `code`, `doc`, `docs`, `facet`,
  `facets`, `generate`, `info`, `keywords`, `memory`, `ops`, `project`,
  `projects`, `serve`, `sources`, `status`, `store`, `summary`, `symbol`,
  `symbols`, `system` (crib's own commands and kinds), or another facet's. A
  facet name can't contain `_` (its tools are `<noun>_<verb>`).
- **Transition names** can't be a graph verb's (`list`, `status`, `next`,
  `add`, ...): a transition is a verb of its own.
- **Field names** can't be a verb parameter or a frontmatter key crib writes:
  `id`, `title`, `type`, `kind`, `source`, `source_path`, `imported`,
  `status`, `tags`, `deps`, `created`, `updated`, `content_hash`, `body_hash`,
  `content`, `relpath`, `project`, `query`, `where`, `symbol`, and the other
  verbs' parameter names (`k`, `hints`, `heading`, `version`, `paths`, ...);
  crib names the clash when it refuses one.
- **In a mapped file**, those frontmatter keys mean what crib says they mean;
  another tool reading the same files should leave them alone. crib folds a
  file's `type` into its tag filters.

## Spec reference

Everything a `[<noun>]` table can say. Unknown keys are refused (the whole file
then falls back to the built-ins, and `crib facets show` says why).

| key | type | default | meaning |
|---|---|---|---|
| `[<noun>]` | table name | (required) | the facet's name: a lowercase identifier without `_`, not a [reserved name](#names-crib-keeps-for-itself). The item kind, the CLI noun and the tools' prefix (`<noun>_add`). |
| `plural` | identifier | (required) | its tool set's name (`--tools <plural>:ro`), and how it reads in prose. |
| `store` | directory name | the plural | the directory its items live in, in each project (or at its mapping). Not one crib keeps for itself (`symbol_index`, `sources`, ...). |
| `aspects` | list | `["core"]` | what it can do ([aspects](#aspects-what-a-facet-can-do)); `core` is implied. `links` and `status` come together (a graph facet); `progress`, `ordered`, `sources` and `supersede` need them; `anchored` and `enriched` don't mix with them. |
| `optional` | list of field names | none | the declared fields an item may leave out. Every other declared field is **required**: `add` refuses a new item without it, and `crib project doctor` names files missing it (`edit` still works on an existing item that lacks one). |
| `title_from` | list of sources | `["title"]` | where an item's title comes from, tried in order: `"title"` (its frontmatter title), `"heading"` (its first `# heading`), `"filename"` (its file's name), or any other frontmatter key (toucan's `"name"`). An item none of them names has no title, and `crib project doctor` lists it. |
| `about` | string | none | what one of these items is, and when to use this family rather than notes; added to every one of its tool descriptions and to the session section ([how agents learn](#how-agents-learn-about-your-facets)). |
| `disable` | list of verbs | none | verbs it doesn't want; each must be one its aspects give it (a graph facet's include its transitions), and not all of them. |
| `[<noun>.fields]` | `name = "int" \| "str" \| "date"` | none | its own typed fields ([fields](#typed-fields-and-queries)); not on a graph facet yet. |
| `[<noun>.when]` | `verb = "wording"` | none | extra wording for one verb's tool description. |
| `[<noun>.status]` | table | (required with `status`) | `values = { <status> = "<role>", ... }` (every status plays a [role](#graph-facets-statuses-transitions-links)); `initial` (default: the first value); `transitions = { <verb> = "<status>", ... }`, each a verb of its own, never a graph verb's name and never into a `retired` status. `progress` needs a `done` status, `supersede` a `retired` one. |
| `[<noun>.bands]` | `band = "name"` | the plan's names | a progress facet's own names for the groups its `list` shows: `claimed` (`in-progress`), `ready`, `blocked`, `paused` (`parked`), `dropped` (`declined`), `done`. Unnamed bands keep the plan's names; two can't share one. |
| `[<noun>.edges]` | `target = "follows" \| "rests_on" \| "informed_by"` | see [links](#graph-facets-statuses-transitions-links) | the default link kind toward each target facet (`"*"` for the rest). |
| `report_taint` | bool | false | it reports staleness: a marker on its search hits, and the `check` and `tree` verbs (as decisions do). Needs `links`. |
| `export_with_deps` | bool | false | its export includes the items it depends on, from other facets (as plans' does). Needs `links` and `sources`. |
| `requires_body` | bool | false | an item must have a body: `add` refuses a title alone (as decisions do). Needs `links` and `status`. |
| `label` | string | `<Noun> items` | the heading of its exported document (`export`). Needs `sources`. |

**Flags** are the four keys above that switch an engine behaviour on for a
facet: `requires_body`, `report_taint`, `export_with_deps` and `label`. Each needs particular aspects (a flag on a facet without them
would do nothing, so it is refused, saying which). They are declared in one
place, `crib.facets.FLAGS`, with their type, needed aspects and meaning, so a
new behaviour is one entry there plus the engine code that reads it;
`crib facets show` lists the flags each facet sets.

`legacy_stores` and `[<noun>.verbs]` (renaming a verb) exist for crib's own
facets only; `crib facets show --toml` shows them in use.

## Examples

**Workflows that live in a repo** (read-only, so crib never writes them):

```toml
[routine]
plural = "routines"
aspects = ["core", "enriched"]

[routine.fields]
schedule = "str"
timeout = "int"
```
```yaml
# the repo's .crib
facets:
  routine: { dir: workflows, glob: "*.md", read_only: true, ids: derived }
```

**A review agent's findings, on the code they're about:**

```toml
[review]
plural = "reviews"
aspects = ["core", "anchored"]

[review.fields]
changeset_id = "str"
severity = "int"
```

`review_add(symbol="pkg.Mod.handler", text="swallows the timeout",
changeset_id="abc123", severity=2)`; `review_list(changeset_id="abc123")`;
`code_dossier` shows the review beside any learning.

**Tickets with fields you query:** `ticket_list(where=[{"field": "severity",
"op": ">=", "value": 3}, {"field": "due", "op": "<", "value": "2027-01-01"}])`.

**An incident tracker** with its own statuses, as in
[graph facets](#graph-facets-statuses-transitions-links): `incident_add`,
`incident_dep_add` (follows another incident), `incident_status ...
mitigated`, `incident_next` (ready ones, claimed first), `incident_shelve`.
