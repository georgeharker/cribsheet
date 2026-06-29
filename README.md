<p align="center">
  <img src="cribsheet-logo-v2@2x.png" alt="cribsheet logo" width="200">
</p>

<h1 align="center">cribsheet</h1>

<p align="center"><em>persistent memory for your AI — plain markdown on disk, semantically indexed</em></p>

A local MCP server that keeps your long-term memory as plain markdown on disk,
indexed for semantic retrieval by embeddings. Disk is the source of truth; the
vector index is a derived, rebuildable cache. See [DESIGN.md](DESIGN.md) for the
full architecture.

`crib` = the command. `cribsheet` = the project.

## Status

Working: `store → index → lookup`, the hash-gated single index path, the
per-write version ring, append/edit/restore, the file watcher (auto-reindex on
external edits), `import` of a repo's local docs via `.crib`, shared Chroma via
`sharedserver`, and a CLI + MCP tool surface. Plus:

- **Daemon-client CLI** — `crib <verb>` attaches to the warm MCP daemon over the
  same sharedserver process instead of cold-starting (≈13s → ≈2s). (§10.2)
- **Hybrid retrieval** — dense (vector) ⊕ BM25 lexical, fused by reciprocal-rank
  fusion, with an optional cross-encoder rerank stage. (§10.3)
- **`apropos`** — like `lookup` but renders the full matching sections.
- **Harness-memory mirror** — `import-memory` mirrors Claude Code's own
  `memory/*.md` into a crib project (host-namespaced) and live-syncs it. (§13)
- **Git sync** — `setup`/`sync`/`push`/`pull` share notes across machines, with a
  frontmatter-aware merge driver so provenance never conflicts. (§14)

It runs with **zero heavy dependencies** — only PyYAML — using a dependency-free
hash embedder and a persistent JSON store. Install the real backends
(sentence-transformers/fastembed, Chroma, watchdog, fastmcp, rich) for production.

Not yet built: `distill` (MCP-sampling re-digest).

Section refs (§) point at `DESIGN.md`.

## Install

**Dev (editable, local venv):**
```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .                 # core only (PyYAML)
pip install -e '.[full]'         # + chroma, sentence-transformers, fastmcp, watchdog
```

**Daily driver (global `crib` on PATH)** — `pipx` keeps the heavy deps isolated:
```bash
pipx install -e '/home/geohar/Development/cribsheet[full]'
```
HTTP serving and the registered MCP entry need `crib` on PATH with the `[mcp]`
(or `[full]`) extra.

## CLI

One binary, two faces — verbs mirror the MCP tools:
```bash
crib store "Chroma is refcounted by sharedserver." --title "process model" -p notes
crib lookup "how is chroma managed" -p notes        # alias: crib search — locator lines
crib search -a "how is chroma managed" -p notes     # -a/--render: full sections (alias: apropos)
echo "# Note" | crib store -                         # '-' reads stdin
crib --json lookup "..." -p notes | jq               # scriptable (--json before the verb)
crib info                                            # paths, backends, daemon/chunk/retrieve
crib import                                          # ingest a repo's docs via .crib
crib import-memory                                   # mirror Claude's harness memory (§13)
crib setup --remote git@host:notes.git               # join the shared repo on a new machine (§14)
crib sync                                            # share notes across machines via git (§14)
```
Verbs: `lookup`/`search` (`-a` renders), `apropos`/`a`, `read`, `locate`,
`store`, `append`, `edit`, `reindex`, `reconcile`, `versions`, `restore`,
`import`, `import-memory`, `setup`, `snapshot`, `sync`/`push`/`pull`, `history`,
`projects`, `info`.

By default a verb attaches to the **warm daemon** (one process shared with the
MCP server) via `sharedserver`, avoiding a per-call cold start; `--no-daemon`
runs in-process, `--host`/`--port` override the endpoint. Human output is
formatted (markdown rendered via the vendored rich pipeline on a tty); `--json`
is machine-readable.

On server startup crib runs a **reconcile sweep** (changed/added files reindexed,
deleted notes' chunks dropped) so offline edits are caught even though the
watcher wasn't running. `crib reconcile` runs it manually across all projects.

## MCP server

```bash
crib --mcp                                   # stdio (default)
crib --mcp --http --port 7732                # streamable-HTTP on a port
crib serve --http --port 7732                # equivalent
```

Registered in `~/.config/secrets/mcpservers.json` as a **sharedserver-backed
HTTP MCP** (so one warm crib serves every client, refcounted with a grace
period — same pattern as `jupyter`/`svg-mcp`):
```jsonc
"mcpServers":  { "cribsheet": { "url": "http://localhost:7732/mcp",
                                "sharedServer": "cribsheet" } },
"sharedServers": { "cribsheet": {
    "command": "crib",
    "args": ["--mcp", "--http", "--host", "127.0.0.1", "--port", "7732"],
    "grace_period": "1h", "health_timeout": 30 } }
```

This is the same warm process the CLI attaches to (§10.2) — Claude (over MCP) and
`crib <verb>` share one daemon. Tools: `lookup`, `apropos`, `read`, `locate`,
`store`, `append`, `edit`, `reindex`, `reconcile`, `versions`, `restore`,
`import`, `import_memory`, `snapshot`, `history`, `projects`. (`sync`/`push`/`pull`
are CLI-only — pushing notes is outward-facing and needs interactive auth.)

## How it's wired

- **One path to the index** — every writer (tools, watcher, direct edits) funnels
  through a single idempotent, content-hash-gated `index_file` under a per-path
  lock. Races and noisy filesystem events degrade to redundant work, never a
  wrong index. (`crib/indexer.py`)
- **Pluggable embedder** — `hash` (dependency-free, default for dev/tests),
  `fe:<model>` (fastembed/ONNX), or `st:<model>` (sentence-transformers). English
  BGE models get the s2p query instruction automatically. (`crib/embed.py`)
- **Pluggable store** — `InMemoryStore` (tests), `JsonStore` (persistent, no
  deps), `ChromaStore` (embedded or shared via `sharedserver`). (`crib/store.py`)
- **Hybrid retrieval** — dense vector ranking fused with a warm-cached BM25
  lexical ranking via RRF; optional cross-encoder rerank (fused, default off).
  (`crib/retrieve.py`)
- **Daemon-client** — the CLI speaks MCP to the warm `crib --mcp --http` process;
  git ops in `sync` run client-side, then trigger a daemon reconcile.
  (`crib/client.py`)
- **Harness-memory mirror** — one-way sync of Claude Code's `memory/*.md` into
  `notes/claude-memory/<host>/`, with a live daemon watcher. (`crib/claudemem.py`,
  `crib/memmirror.py`)
- **Two-layer versioning + git sync** — automatic per-write ring + git snapshots,
  shareable across machines. (`crib/versions.py`, `crib/gitbacking.py`)
- **Frontmatter-aware merge** — a `merge=cribnote` git driver resolves note
  *headers* deterministically (provenance never conflicts) while genuine *body*
  conflicts still surface, header already merged. (`crib/merge.py`)

## Sharing notes across machines (§14)

The data dir is a git repo with a remote; notes sync via plain git.

```bash
# first machine — create the shared repo and push
crib sync --remote git@host:notes.git

# every other machine — join it
crib setup --remote git@host:notes.git    # init + merge driver + pull
crib sync                                  # thereafter: commit + pull + push
```

Derived-note provenance is built to *not* conflict: each note's `id` is derived
from its path (not a per-machine random id), `source_repo` is stored as a portable
`$LOCATION/rest` token (see `[locations]`), and `imported` is pinned to
first-import. Anything that still diverges is settled by the `cribnote` merge
driver — a header-only difference resolves silently; a body difference stops the
pull and is listed for you to resolve, with the header already merged clean. (`crib
setup` registers the driver per machine; git config doesn't travel with the repo.)

## Config

`$XDG_CONFIG_HOME/crib/config.toml` (override roots with `CRIB_CONFIG_DIR`,
`CRIB_DATA_DIR`, `CRIB_INDEX_DIR`):

```toml
default_project = "default"
versions_keep = 20

[embed]
model = "hash"            # or "fe:BAAI/bge-small-en-v1.5", "st:<model>"
# query_prefix = ""       # override the auto BGE s2p query instruction

[chunk]
window_words = 320        # split long sections under the model's token cap
overlap_ratio = 0.20      # overlap between adjacent windows (derived: 64 words)

[retrieve]
hybrid = true             # dense ⊕ BM25, RRF-fused
rerank = false            # optional cross-encoder rerank (fused)

[daemon]
enabled = true            # CLI attaches to the warm MCP process
port = 7732               # also the bind port for `serve`/`--mcp`

[memory]
watch = true              # daemon live-mirrors bound harness memory dirs (§13)

[chroma]
mode = "embedded"         # "embedded" | "shared" | "json"

[locations]              # named path roots → portable $LOCATION tokens (§14)
DEV = "~/Development"     # provenance paths stored as $DEV/... so they sync clean
```

## Make Claude actually use it

The MCP tools and the server's `instructions` are loaded **lazily** — the model
only sees them once it has decided to reach for a tool, which is too late for a
"check memory first" habit. The reliable lever is a short directive in a global
`CLAUDE.md` (always in context, loaded once per session — not per turn):

```markdown
# Memory (cribsheet)
- Before answering about a project/topic/past decision — or exploring a codebase
  cold — `lookup`/`apropos` cribsheet first; the answer may be stored.
- When a durable fact emerges (decision, convention, gotcha, contract), `store`
  it — `lookup` first, `append`/`edit` over duplicating.
```

Put it at `$CLAUDE_CONFIG_DIR/CLAUDE.md` (e.g. dotfiles-managed, like
`settings.json`). Plugins can't bundle eager instructions, so this stays a
CLAUDE.md file; the MCP registration is what the plugin/combiner carries.

Pair it with `crib import-memory` so the harness `memory/*.md` notes you (Claude)
already write are mirrored into cribsheet's searchable index automatically — one
source of truth, two searchable surfaces.

## Tests

```bash
pip install pytest && pytest -q
```
