<p align="center">
  <img src="docs/images/cribsheet-logo-v2@2x.png" alt="cribsheet logo" width="200">
</p>

<h1 align="center">cribsheet</h1>

<p align="center"><em>persistent memory for your AI — plain markdown on disk, semantically indexed</em></p>

Your AI assistant forgets everything between sessions. **cribsheet** gives it a
durable, searchable memory — as plain markdown files you own, not a black box —
that persists across sessions and is shared across every agent and tool you run.

It remembers two kinds of things:

- **Notes** — decisions, conventions, gotchas, hard-won facts. Written as markdown,
  found by meaning (semantic + keyword search), not just exact words.
- **Code** — a symbol index of your repos: every function/class/method with an LLM
  "what it does" description, a real call graph (who calls what), and any durable
  *learnings* you pin to a symbol. It answers the questions `grep` can't — *find
  this by concept*, *what calls this*, *what does this do* — across files.

Disk is the source of truth; the vector index is a derived, rebuildable cache. One
warm process serves your editor (over MCP) and your terminal (`crib <verb>`) alike.

> `crib` = the command. `cribsheet` = the project. See **[DESIGN.md](DESIGN.md)**
> for the full architecture.

## Why

- **Agents forget. Memory should be durable and shared.** A decision you made last
  week, in another repo, in another tool's session, is one `lookup` away — because
  it's the same markdown tree behind every agent.
- **`grep` can't answer "what does this do" or "what calls this."** The code index
  answers by *intent* and traces the real call graph, so an agent (or you) finds
  code by concept and understands its neighbourhood in one call.
- **It's plain markdown you own.** No proprietary store — edit notes in your own
  editor, diff them in git, sync them across machines. The index rebuilds itself.
- **Built to actually get used.** The hard part of memory isn't storing — it's
  *recall at the right moment*. cribsheet ships the delivery layer: a one-line
  directive that keeps the habit in context, plugins that wire it into Claude Code,
  and an always-warm daemon so a lookup is never the slow path.

## Quickstart

**1 — Install** (`pipx` keeps the heavy deps isolated and puts `crib` on your PATH):

```bash
git clone https://github.com/georgeharker/cribsheet && cd cribsheet
pipx install -e .            # everything: crib + chroma, embeddings, fastmcp, watcher, llmkit
```

The default install is complete — no extras needed (`[st]`, the torch embedder,
is the one genuine extra; torch wheels are host-specific). llmkit isn't on PyPI —
it installs from its git head, so no submodule dance is needed; the
`vendor/llmkit` submodule is only for hacking on llmkit itself or indexing it
in-tree (`git submodule update --init` when you want it).

<details><summary>Dev install (editable venv / uv)</summary>

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .                        # llmkit comes from its git head

uv sync                                 # or: uv pip install -e .

# the torch embedder + llmkit's native LLM adapters:
pip install -e '.[st]' 'llmkit[md,bridge,anthropic,google,claude] @ git+https://github.com/georgeharker/llmkit'

# hacking on llmkit itself: overlay the submodule editable (uv sync restores git)
git submodule update --init && pip install -e ./vendor/llmkit
```
</details>

**2 — Wire it into Claude Code.** The simplest path installs the MCP server *and* the
"reach for memory" directive as a plugin:

```bash
claude plugin marketplace add georgeharker/cribsheet
claude plugin install cribsheet                 # MCP tools + instructions
# already have crib served another way? use the instructions only:
claude plugin install cribsheet-instructions
```

Prefer to wire it by hand? Copy [`CLAUDE.md.example`](CLAUDE.md.example) into your
global `$CLAUDE_CONFIG_DIR/CLAUDE.md` — that one-line directive is what keeps the
habit in context (tool descriptions alone load too late to form it).

**3 — Use it.** The CLI verbs mirror the MCP tools, so anything Claude does you can
do too:

```bash
# notes
crib store "Chroma is refcounted by sharedserver." -p notes   # remember a fact
crib lookup "how is chroma managed" -p notes                  # find it by meaning
crib search -a "how is chroma managed" -p notes               # -a: full sections

# code — onboard a repo, then ask it questions grep can't answer
cd ~/Development/myrepo
crib project setup                                # index code + docs (one call)
crib code lookup "combine ranked lists"           # find a symbol by CONCEPT
crib code dossier LexicalCache.get                # everything about one symbol
crib code graph reciprocal_rank_fusion            # walk the call graph (pstree)
crib code append reciprocal_rank_fusion \
     "fuses by RANK, not score — robust to scale differences"   # pin a learning to it
```

That's the loop: **store what's worth keeping, look it up by meaning, onboard a repo
and explore it by concept.** An agent that hits an unindexed repo self-diagnoses
toward `project setup`, runs it, and carries on.

## The surface

Every capability, its CLI form, its MCP tool, and a one-liner lives in
**[docs/surface.md](docs/surface.md)** — the full reference. The essentials:

| | notes | code |
|---|---|---|
| **find** | `lookup` / `apropos` (full sections) | `code lookup` (concept ⊕ name), `code dossier`, `code graph`/`xref` |
| **write** | `store`, `append`, `edit`, `forget`, `move` | `code append`/`edit`/`forget` (learnings) |
| **onboard** | `import` (files → memory), `import-memory` | `project setup` / `index` / `status` |
| **housekeeping** | `reindex`, `reconcile`, `versions`, `restore`, `history` | `code learnings`, `code rehome` |

Notes and code share one store, so `lookup` surfaces a repo's docs alongside your
stored knowledge. A repo's `.crib` file ties it to a project and declares which
source and docs to index (docs are indexed **in-situ** — the repo stays the master;
crib holds only the index).

By default a verb attaches to the **warm daemon** (the same process the MCP server
runs), so it's fast; `--no-daemon` runs in-process, `--json` gives machine output.

## Going further

- **Run the MCP server** — the plugin (Quickstart) is the easy path. To run it
  yourself: `crib --mcp` (stdio) or `crib serve --http --port 7732`, registered as a
  [`sharedserver`](https://github.com/georgeharker/claude-sharedserver)-backed HTTP
  MCP so one warm crib serves every client (and the same process the CLI attaches
  to). If a combiner already proxies crib's tools to you, install
  `cribsheet-instructions` (directive only, no second MCP).
- **Share across machines** — the data dir is a git repo; notes sync via plain git
  with a frontmatter-aware merge driver so provenance never conflicts:
  ```bash
  crib sync --remote git@host:notes.git   # first machine: create + push
  crib setup --remote git@host:notes.git  # every other: init + merge driver + pull
  crib sync                               # thereafter: commit + pull + push
  ```
  Full walkthrough: [docs/resume-on-new-machine.md](docs/resume-on-new-machine.md).
- **Mirror Claude's own memory** — `crib import-memory` (MCP: `import_memory`, so
  an agent can do it too) mirrors Claude Code's harness `memory/*.md` into crib
  (host-namespaced) and live-syncs it, so it's searchable alongside everything else.
- **Configure** — `$XDG_CONFIG_HOME/crib/config.toml` picks the embedder, retrieval
  mode, daemon, and backends; `crib info` prints the resolved paths. The defaults are
  sensible — a minimal override:
  ```toml
  [embed]
  model = "hash"          # or "fe:BAAI/bge-small-en-v1.5" (fastembed), "st:<model>"
  [retrieve]
  hybrid = true           # dense ⊕ BM25, RRF-fused; rerank = true adds a cross-encoder
  [chroma]
  mode = "embedded"       # "embedded" | "shared" (sharedserver) | "json"
  [locations]
  DEV = "~/Development"   # provenance paths stored as $DEV/… so they sync clean
  ```
  Language-server and generation-provider examples:
  [`docs/lsp.json.example`](docs/lsp.json.example),
  [`docs/generate.toml.example`](docs/generate.toml.example).

## How it works

The short version: every write funnels through one idempotent, content-hash-gated
index path under a per-path lock, so races degrade to redundant work, never a wrong
index. Retrieval fuses a dense vector ranking with a warm BM25 lexical ranking
(RRF), with an optional cross-encoder rerank. Embedder and store are both pluggable
(dependency-free defaults for dev). The code index is built live from language
servers (`.lsp.json` specs — ty/pyright, rust-analyzer, gopls, clangd, shuck, …) for
the structural facet and an LLM for the "what it does" descriptions.

The full picture lives in separate docs, not this README — pick by what you want:

- **[docs/implementation.md](docs/implementation.md)** — *how it works today*: a
  subsystem-by-subsystem map (ingestion, indexing, watchers, warm LSP sessions,
  cross-project refs, sync/merge), anchored to files and symbols. Start here to
  work on the code.
- **[DESIGN.md](DESIGN.md)** — the architecture and the *why* behind the
  decisions, end to end.
- **[docs/surface.md](docs/surface.md)** — the complete CLI + MCP reference.
- **[docs/code-symbol-index.md](docs/code-symbol-index.md)** — how the code↔note
  index is built, and the learnings model.
- **[docs/retrieval-and-adoption.md](docs/retrieval-and-adoption.md)** — retrieval
  quality, and why delivery (not capability) is what makes a memory tool get used.
- **[docs/knowledge-capture.md](docs/knowledge-capture.md)** — `distill` /
  `elaborate` / `summarize`, the generation layer over notes.

## Status & tests

The default install is the complete product: chromadb (store), **fastembed** (the
recommended embedder — ONNX, no torch), **fastmcp** (serves MCP *and* the
warm-daemon path the CLI uses by default), **watchdog** (external-edit watchers),
and **llmkit** from its git head (rendering + generation). The only real extra is
`[st]`, the torch embedder — torch wheels are host-specific. The dependency-free
fallbacks (hash embedder, JSON store, `--no-daemon`) exist as *code properties*
that tests and CI exercise, not install profiles. The store → index → lookup path,
the version ring, the file watcher, git sync, the code index, and the CLI + MCP
surface are all working.

```bash
pip install pytest && pytest -q
```
