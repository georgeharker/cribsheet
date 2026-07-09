# cribsheet — user guide

**cribsheet** is durable memory for your AI. It stores what's worth remembering as
plain markdown files you own — on disk, in git, editable in your own tools — and
makes it findable by *meaning*, not just exact words. The same memory is shared
across every session, every tool, and every machine you work on, so a decision you
made last week in another repo is one lookup away. You talk to it with the `crib`
command; your AI reaches the same store through its MCP tools.

This guide walks through the concepts and the everyday workflows. For the intro and
install, see the [README](../README.md); for the exhaustive command list, see
[surface.md](surface.md).

## The four things cribsheet manages

- **Notes** — durable facts: decisions, conventions, gotchas, hard-won answers.
  You write them as short markdown; cribsheet indexes them so you (or your AI) can
  find them later by describing what you're after, even in different words.
- **Code index** — a searchable map of a repo's code. Every function, class, and
  method gets a plain-language "what it does" description plus a real call graph
  (who calls what). It answers the questions `grep` can't: *find this by concept*,
  *what calls this*, *what does this do* — across files.
- **Learnings** — durable notes attached to a specific code symbol. When you finally
  understand a tricky function, you pin the insight to it; it resurfaces whenever
  anyone looks that symbol up, and survives re-indexing.
- **Projects** — separate memory namespaces. Each repo (or topic) has its own notes,
  code index, and learnings, so nothing bleeds between contexts. A `default` project
  holds cross-cutting knowledge.

## The interface

![The command surface — four nouns (note, code, learning, project) and their verbs](images/command-surface.png)

Every command reads as **`crib <noun> <verb>`** — a noun for the facet (`note`,
`code`, `learning`, `project`) and a verb for the action:

```bash
crib note store "…"        crib note lookup "…"
crib code lookup "…"       crib code xref some_symbol
crib learning add sym "…"  crib learning read sym
crib project setup         crib project list
```

That noun-verb shape is the only form — there is no hyphenated `crib code-lookup`.

**Picking a project.** Two selectors work on any command:

- `-p <name>` / `--project <name>` — select by project name.
- `-P <path>` / `--project-path <path>` — select by any path inside the repo (cribsheet
  resolves it to the project).

Code and learning commands act on one *current* project. Set it once with
`crib project use <name>` (or let it be inferred from a path), and later reads need no
selector. **Writes always need an explicit project** — `store`, `append`, `edit`,
`forget`, `move`, and learnings won't silently inherit the current one, so a fact
can't land in the wrong place.

**Two global flags**, placed before the noun:

- `--json` — machine-readable output for any command.
- `--no-daemon` — run in-process instead of attaching to the always-warm background
  process (handy when you've just changed cribsheet itself).

Content-taking verbs (`note store`/`append`/`edit`, `learning add`/`edit`) accept `-`
in place of the text to read it from stdin.

## Common workflows

### 1. Store and recall a note

Remember a durable fact, then find it again by describing it:

```bash
crib note store "Staging deploys need the VPN; prod deploys don't." -p default
crib note lookup "how do I reach staging" -p default      # finds it by meaning
crib note apropos "how do I reach staging" -p default     # same, but full sections
```

`lookup` returns ranked one-line locators; `apropos` returns fewer hits but prints
each matching section in full. To read or revise a specific note:

```bash
crib note read <relpath> -p default        # print its raw markdown
crib note append <relpath> "…" -p default  # add to it
crib note edit <relpath> -p default        # replace its body (or pass - for stdin)
```

Every write is versioned — `crib note versions <relpath>` lists recoverable prior
versions and `crib note restore <relpath> <v>` rolls back. A deleted note
(`crib note forget`) is recoverable too.

### 2. Onboard a repo and search its code

Point cribsheet at a repo once, then ask it questions grep can't answer:

```bash
cd ~/Development/myrepo
crib project setup                          # index code + docs in one call
crib project status                         # confirm: symbol/file counts

crib code lookup "combine two ranked lists" # find a symbol by CONCEPT
crib code dossier reciprocal_rank_fusion    # signature + description + neighbours
crib code xref reciprocal_rank_fusion       # its callers, callees, references
crib code graph reciprocal_rank_fusion      # the call graph as a pstree
```

`project setup` is the full onboard (docs + code). If you only want the code index
re-run after changes, `crib project index` is the cheap, hash-gated repeat. An AI
agent that hits an unindexed repo self-diagnoses toward `project setup` on its own.

### 3. Attach and recall a learning on a symbol

When you work out what a confusing function really does, pin it so the insight is
there next time — for you or the AI:

```bash
crib learning add reciprocal_rank_fusion \
  "Fuses by RANK, not score — robust to scale differences between the two rankers."
crib learning read reciprocal_rank_fusion   # print it back
crib learning report                        # health of all learnings: ok/moved/orphan
```

Learnings survive re-indexing and resurface automatically in `code lookup`, `code
xref`, and `code dossier`. If code moves and a learning is orphaned, `crib learning
rehome <old> [new]` re-points it (with no target it suggests ranked candidates).

### 4. Share memory across machines

Notes live in a git repo, so they sync with plain git plus a merge driver that keeps
provenance from ever conflicting:

```bash
crib note sync --remote git@host:notes.git   # first machine: create + push
crib note setup --remote git@host:notes.git  # every other machine: init + pull
crib note sync                               # thereafter: commit + pull + push
```

`crib note push` and `crib note pull` are the halves of `sync` (pull reindexes after).
The code index and other derived data are regenerable, so they aren't synced — you
rebuild them locally with `crib project reconcile` (sweeps every project for changes).
A full new-machine runbook is in
[resume-on-new-machine.md](resume-on-new-machine.md).

### 5. Reach the same memory from your AI

Everything above is available to an AI agent through cribsheet's MCP tools, whose
names mirror the CLI: `note_lookup`, `note_store`, `code_lookup`, `code_dossier`,
`learning_add`, `project_setup`, and so on. So when your agent looks something up or
onboards a repo, it's reading and writing the *same* markdown tree you use from the
terminal — one shared memory behind both. Wiring it into Claude Code is a plugin
install; see the [README](../README.md#quickstart).

## Where to go next

- [README](../README.md) — the intro, install, and quickstart.
- [surface.md](surface.md) — the complete CLI + MCP reference (every verb and tool).
- [resume-on-new-machine.md](resume-on-new-machine.md) — standing memory up on a new box.
