# crib as a language server — the agent face (and the editor one)

Status: design — not built. Extends [code-symbol-index.md §3](../code-symbol-index.md)
(the warm LSP *client* subsystem) and [DESIGN §10.2](../../DESIGN.md) (the daemon owns
the state; every face is a client of it). Depends on one storage change (§9.1) worth
making regardless.

> **Thesis.** crib consumes LSP to *build* the index. Serving LSP puts the enriched
> result back through the same protocol — and the primary consumer is **Claude**, not
> an editor. crib already reaches Claude over MCP, so this face adds no capability.
> What it adds is **invocation**: MCP tools must be chosen over the reflex to grep,
> which is why the whole reach-for-crib directive exists. An LSP server is consulted
> because the agent already reached for code intelligence — and, through diagnostics
> (§4.3), can deliver knowledge the agent never asked for at all. That is the argument
> for building this. The editor face falls out of the same work.

## 1. The inversion

crib is already an LSP client. Becoming a server is the same protocol read in the
opposite direction, over data crib already holds:

| crib as LSP **client** (shipped, §3) | crib as LSP **server** (this doc) |
|---|---|
| calls `documentSymbol` to *learn* the symbol set | answers `workspaceSymbol` *from* it — by concept |
| calls `hover` to harvest doc prose | answers `hover` with the LLM description + pinned learnings |
| calls `callHierarchy`/`references` to build edges | answers them from stored edges, **across projects** |
| consumes `implementation` (§9.2) into a stored edge | answers `implementation` cross-project |
| reads Claude Code's `.lsp.json` schema for its specs | ships an `.lsp.json` entry so Claude attaches to it |

That last row is the loop closing. [§3.3](../code-symbol-index.md) already adopted
Claude Code's `.lsp.json` / plugin `lspServers` schema verbatim so existing spec files
drop in unchanged; the same schema is how crib announces *itself*. The
[cribsheet plugin](../../plugins/claude/.claude-plugin/plugin.json) already ships hooks,
a warm-daemon launcher and injected instructions — an `.lsp.json` is a natural fifth
component, and it means crib's index reaches a new user's Claude with no MCP wiring at
all.

## 2. Who consumes it

### 2.1 What Claude's LSP tool actually exposes

Nine operations: `goToDefinition`, `findReferences`, `hover`, `documentSymbol`,
`workspaceSymbol`, `goToImplementation`, `prepareCallHierarchy`, `incomingCalls`,
`outgoingCalls`. All are position-addressed (`filePath` + 1-based `line`/`character`)
**except `workspaceSymbol`, which takes an opaque query string**.

Two consequences that reshape the design relative to an editor consumer:

- **`workspaceSymbol` is the only position-free door, so it is the front door.** And
  it composes cleanly with the rest: `workspaceSymbol("concept")` → `Location` →
  `hover(that location)` → description + learnings. Two calls, no file read, entirely
  inside one tool.
- **There is no `executeCommand` and no `codeLens`.** The capture loop — which for an
  editor is the highest-leverage surface — is *unavailable* here. Claude captures over
  MCP (`code_append`, `learning add`), which already works. Capture is therefore an
  editor-only concern in this design (§4.5), not the centrepiece.

There is also a surface the editor case would never want: **diagnostics push** (§4.3).

### 2.2 Why bother, when MCP already works?

This face is strictly less expressive than MCP. crib owns its MCP tool names,
descriptions, arguments and project selection; over LSP it gets nine fixed operations
and no say in how they are described. If the goal were capability, don't build it.

The goal is **invocation**, which [retrieval-and-adoption §1/§4](../retrieval-and-adoption.md)
names as the binding constraint on the entire project — *a better index the agent
never queries is an index nobody reads.* Three things follow:

1. **It catches a different reflex.** The directive currently has to talk the model
   out of `grep` *and* out of the built-in `LSP` tool. If crib **is** an LSP server,
   the second reflex lands on crib instead of competing with it. Adoption by
   construction rather than by nagging.
2. **It removes the setup barrier for other people.** MCP wiring plus a CLAUDE.md
   directive is a real install cost. A plugin that registers an LSP server is one
   install and no prose.
3. **It reaches contexts MCP doesn't** — restricted subagents, other LSP-consuming
   agents, any harness with an LSP client and no MCP.

**The cost to name honestly:** crib loses the tool-description channel. The `LSP`
tool's description is fixed and says *"workspaceSymbol: Search for symbols matching a
query"* — nothing tells the model it may pass a *concept* rather than a name, which is
crib's single best capability. That has to be carried by
[`plugins/claude/instructions.txt`](../../plugins/claude/instructions.txt) instead, which
is exactly the delivery-layer problem crib already solves for MCP, now applied to a
channel crib does not own. The instructions table gains a row: *want a symbol you can
only describe? `workspaceSymbol` with the description, not the name.*

### 2.3 Multi-server attach is settled

The design hinges on crib attaching **alongside** the real language server, not
instead of it. That is not speculative: this machine already runs
`georgeharker/pylsp` and `georgeharker/ruff-lsp` as separate Claude Code plugins,
**both** mapping `.py`/`.pyi`/`.pyw` in their `.lsp.json`, installed concurrently —
pylsp for navigation and `pylsp_mypy` diagnostics, ruff for lint. Multiple servers per
extension works, and the sidecar shape (§5) is available to crib for free.

## 3. The rule: additive in kind

The failure mode is not crib answering wrong. It is **crib answering a question
pyright already answered**, so the consumer gets two competing results and turns crib
off. Every advertised capability must be one the co-attached server structurally
cannot serve.

| Surface | crib's answer | Serve? |
|---|---|---|
| `workspaceSymbol` | semantic search over descriptions | **yes** — no server does concept |
| `hover` | description + learnings | **yes** — additive to types, different content |
| diagnostics (Hint) | "this symbol has pinned learnings" | **yes, Claude only** — §4.3 |
| `references`, `callHierarchy`, `implementation` | stored edges | **conditionally** — §4.4 |
| `executeCommand`, `codeLens` | pin a learning at point | **editor only** — §4.5 |
| `definition`, `typeDefinition`, `documentSymbol`, `completion`, `rename`, formatting | — | **no** — §4.6 |

## 4. The surfaces

### 4.1 `workspaceSymbol` — concept search

Build first. Nothing in the protocol requires the query to match a name — it is an
opaque string, and crib answers it with `code_lookup`. This is the grep-can't-do value
boundary from [code-symbol-index.md](../code-symbol-index.md), delivered through an
operation the agent already reaches for.

**Client-side refiltering — an editor risk that the primary consumer probably
doesn't have.** Editor pickers (telescope, fzf-lua) commonly fuzzy-match the query
against returned names and would drop `_refresh_oauth` for the query "handle auth
expiry" before it is ever displayed; LSP has no score field to defend with. Claude's
LSP tool has no obvious reason to post-filter, and results reach the model as text.
This should still be *checked* rather than assumed, but if it holds it means the
largest risk to the headline feature belongs to the secondary consumer only. The
defence, if needed, is returning the description as `containerName` so the refiltered
text contains the query's own vocabulary.

### 4.2 `hover` — description and learnings

pyright gives the type; crib gives what it does and the gotcha someone pinned in
March. Different content, same gesture, and multi-provider hover is fine in Claude and
merges in nvim 0.11+ / VSCode.

Position → symbol is a range lookup in the file's stored entries — the reverse of
indexing, where crib holds a position and asks the server.

For Claude specifically there is a second, better-than-expected path: the agent is
usually hovering *because it just read or edited that file*, so the position is
already in hand. "What do I know about the symbol I am about to change" becomes a
cheap single call rather than a decision to consult memory.

### 4.3 Diagnostics — the push channel (Claude only)

**The most interesting surface here, and the one that inverts crib's invocation
problem.** Everything else in this doc still waits to be asked. Diagnostics are the
one LSP channel that is *unsolicited* — Claude Code surfaces them after edits without
the model choosing to look, which is precisely why `pylsp` is configured on this
machine with `pylsp_mypy` in `live_mode`.

So: publish a `Hint`-severity diagnostic on a symbol that has **pinned learnings**.
The agent edits a function; the learning someone wrote about that function arrives
unbidden, at the exact moment it is relevant. No directive, no reflex to catch, no
decision to consult. For a project whose stated gate is invocation, that is the
strongest available mechanism, and it exists on no other face.

It is also the easiest thing here to get wrong, so the constraints are tight:

- **`Hint` severity only.** Never Warning or Error — this channel's value depends on
  the agent trusting that a diagnostic means the code is broken.
- **Learnings only, never descriptions.** Descriptions exist for nearly every symbol;
  a hint on all of them is wallpaper and would poison the channel permanently.
  Learnings are sparse and deliberate — someone wrote them down *because* the code
  misled them once. That sparsity is the whole safety margin.
- **Off by default, one flag.** The blast radius of getting this wrong is every edit
  in every session.
- **Editors: no.** In a buffer this is squiggle noise on working code. The channel is
  valuable here only because the agent has no peripheral vision — it reads diagnostics
  as text or not at all.

Worth being clear-eyed that this is a mild abuse of the channel: diagnostics mean
"something is wrong with this code," and "someone knows something about this code" is
a different claim. The `Hint` tier is the narrowest available reading of that, and the
sparsity constraint is what keeps it honest. If it proves noisy in practice, drop it —
it is the one item here with no fallback design.

### 4.4 Edges — only where the real server cannot reach

`findReferences`, call hierarchy and `goToImplementation` are duplicative in the
common case. Two cases are genuinely additive:

1. **Cross-project.** pyright searches its own root; crib's `refs:` fan-out already
   resolves across project boundaries (llmkit callers of a crib symbol, or the
   inverse). No single-root server shows these.
2. **No server configured** for that language or file — crib's stored edges are then
   the only edges available, and crib knows when this is the case because it knows
   which specs resolved during indexing.

Case 2 is self-detecting and safe to advertise. Case 1 is not expressible in the
protocol — a server cannot tell whether another client is attached — so it needs
either a config flag or acceptance of duplicate results. Open (§10).

### 4.5 Capture — editor-only

Unavailable to Claude (§2.1), and unnecessary there since MCP already carries it. For
the editor face it remains the highest-value surface: `workspace/executeCommand` with
a `crib.learning.add` command, plus `codeLens` showing "2 learnings" above symbols
that have them. Capture at the moment of understanding — cursor on the symbol, no
context switch — is the twin of the recall problem the README names.

**Limitation to record:** LSP has no server-initiated free-text input
(`window/showMessageRequest` gives buttons, not a field), so the client must collect
the learning body and pass it as a command argument. That means a small per-editor
keybind. The read surfaces need no editor-side code.

### 4.6 Not served

`definition`/`typeDefinition` (duplicative, and crib's copy is staler than the
server's), `documentSymbol` (duplicative), `completion` (crib has no business in a
latency-critical hot path), `rename`/formatting/code actions (crib does not mutate
source).

## 5. Sidecar, not proxy

The alternative is middleware: crib between consumer and pyright, forwarding
everything, enriching `hover` and augmenting `workspaceSymbol` in passing. One server
to configure, merged rather than adjacent results.

**Rejected:**

- **Two lifecycles for one binary.** crib already owns pyright inside
  `LspSessionPool` for indexing (§3.1). Proxying adds a second, consumer-driven
  lifecycle over the same server — a nasty bug class, in the subsystem crib already
  paid to get right once.
- **Degradation.** A dead sidecar costs descriptions and concept search. A dead proxy
  costs the language server.
- **Blast radius.** A proxy sits on every keystroke-adjacent request; a sidecar sits
  on nothing it did not opt into.

§2.3 removes the proxy's main justification anyway — multi-attach demonstrably works,
so there is no forced choice between crib and pyright.

## 6. Transport — `crib lsp`, a stdio shim

Consumers spawn a process and speak LSP over stdio; the daemon is a long-lived HTTP
MCP server. So the face is a thin `crib lsp` shim: stdio in, attach to the warm daemon
via sharedserver, translate to the existing tool surface. Structurally the same move
[`crib/client.py`](../../crib/client.py) already makes for the CLI — which is why this is
cheaper than it looks. `--no-daemon` falls back to in-process `Crib.open()`, as
everywhere else. The `.lsp.json` entry is then just
`{"command": "crib", "args": ["lsp"], "extensionToLanguage": {...}}`.

**Divergence from §3's stdio note.** There, "an LSP server talks over pipes to one
parent" forced the *sessions* in-process in the daemon. Here the same fact forces the
opposite shape — a per-consumer shim, because the consumer insists on being the
parent. Same constraint, inverted conclusion; noted so the two sections don't read as
contradicting each other.

Project resolution: `initialize`'s `rootUri` → crib project by the same path inference
`project_path` uses; `workspaceFolders` for multi-root.

## 7. The read-from-disk invariant

crib would be an LSP **server** in a process that owns LSP **clients**. If a hover can
trigger revalidation that acquires a pool session, a consumer request is blocking on a
language server crib is starting — with the consumer's timeout running, and a
plausible self-deadlock when the server being started is the one that would answer.

The two-tier model already forbids it: *"the LSP is a generator/refresher, not a
serving dependency"* (`crib/codeindex.py` docstring). Everything in §4 is served from
stored tomls and the resident index, never the pool.

**With a server face this stops being a preference and becomes a hard constraint** —
enforced structurally (separate entry points), not by discipline, because the failure
is a hung consumer rather than a slow query. It is the clearest payoff of the existing
two-tier design and what makes this face safe to build at all.

## 8. Staleness becomes visible

An agent tolerates a stale description in a `code_dossier` it deliberately asked for.
A hover describing code it rewrote five minutes ago is a wrong answer delivered with
the authority of tooling — and a diagnostic (§4.3) about a symbol that no longer
exists is worse. This promotes two `todos.md` items from quality-of-life to
load-bearing:

- **The `$/progress` readiness barrier** — under-resolved edges from an early-answering
  cold server become *served* wrong answers.
- **Eager source-watcher revalidation (Phase 2)** — query-time revalidation suffices
  when the querier is patient. Neither of these consumers is.

Neither blocks §9.3–9.4; both gate calling the face finished.

## 9. Build order, each gated by a proof

1. **Persist `character`.** `_symbol_position` already computes the exact position
   from `selectionRange` (`crib/codeindex.py:1067`) and discards the column; the toml
   keeps only `line`. Persisting it makes every symbol a precise LSP address — for
   `Location` ranges, for position→symbol in hover, for anything later. *Proof:
   `scripts/snapshot_harness.py` shows the field added and nothing else structurally
   changed.*
2. **`implementation` edges.** Pure generator-side, independently valuable: call
   edges dead-end at abstract dispatch, so Protocol/ABC/trait/interface targets are
   the standing hole in the graph. rust-analyzer, gopls, clangd and pyright implement
   it; **verify `ty`**, the first-choice `.py` server. Gate on `implementationProvider`
   exactly as call hierarchy gates today. *Proof: a known Protocol in crib resolves to
   its implementors.*
3. **The shim + `workspaceSymbol` alone**, registered via the plugin's `.lsp.json`.
   *Proof: a concept query through Claude's `LSP` tool returns the right symbols, and
   survives whatever post-processing the client does (§4.1).*
4. **`hover`.** Description + learnings by position. *Proof: hovering a symbol with a
   pinned learning shows it, alongside pyright's types rather than instead of them.*
5. **Diagnostics push**, off by default, learnings-only, `Hint` (§4.3). *Proof: edit a
   symbol carrying a learning and the learning appears unprompted — and a session of
   ordinary work produces no hint the agent should have ignored.*
6. **Cross-project edges** (§4.4), then the **editor capture loop** (§4.5) with a
   reference Neovim keybind.

Steps 1 and 2 are worth landing whether or not this face is ever built.

## 10. Open questions

- **Does Claude's LSP tool pass `workspaceSymbol` results through unmodified?** Step 3
  exists to answer this early; it is the difference between the headline feature
  working and needing the `containerName` defence.
- **Will the model actually pass concepts to `workspaceSymbol`** when the tool
  description says "matching a query" and crib cannot change that text? The
  instructions file is the only lever (§2.2) — and if it proves insufficient, this
  face is strictly worse than MCP and the honest conclusion is to keep the index on
  MCP and ship only §9.1–9.2.
- **Diagnostics noise (§4.3).** Sparsity is the safety margin; is learnings-only
  sparse enough on a repo with heavy learning coverage, or does it need a
  recently-edited filter too?
- **Advertising edge capabilities (§4.4)** — no in-protocol way to detect a
  co-attached client. Config flag, or accept duplicates?
- **Is this a projection of the MCP surface or a parallel implementation?**
  `workspaceSymbol` is `code_lookup` wearing a protocol; `hover` is a narrowed
  `code_dossier`. If the translation layer is as thin as it looks, build it as a
  projection from the start — one set of semantics, two wire formats.
