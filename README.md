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
`sharedserver`, and a CLI + MCP tool surface.

It runs with **zero heavy dependencies** — only PyYAML — using a dependency-free
hash embedder and a persistent JSON store. Install the real backends
(sentence-transformers, Chroma, watchdog, fastmcp) for production use.

Not yet built: `distill` (MCP-sampling re-digest).

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
crib lookup "how is chroma managed" -p notes        # alias: crib search
echo "# Note" | crib store -                         # '-' reads stdin
crib --json lookup "..." -p notes | jq               # scriptable
crib info                                            # paths + available backends
crib import                                          # ingest a repo's docs via .crib
```
Verbs: `lookup`/`search`, `read`, `locate`, `store`, `append`, `edit`,
`reindex`, `reconcile`, `versions`, `restore`, `import`, `snapshot`, `history`,
`projects`, `info`.

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

## How it's wired

- **One path to the index** — every writer (tools, watcher, direct edits) funnels
  through a single idempotent, content-hash-gated `index_file` under a per-path
  lock. Races and noisy filesystem events degrade to redundant work, never a
  wrong index. (`crib/indexer.py`)
- **Pluggable embedder** — `hash` (dependency-free, default for dev/tests) or
  `st:<model>` (sentence-transformers). (`crib/embed.py`)
- **Pluggable store** — `InMemoryStore` (tests), `JsonStore` (persistent, no
  deps), `ChromaStore` (embedded or shared via `sharedserver`). (`crib/store.py`)
- **Two-layer versioning** — automatic per-write ring + manual git snapshots.
  (`crib/versions.py`, `crib/gitbacking.py`)

## Config

`$XDG_CONFIG_HOME/crib/config.toml` (override roots with `CRIB_CONFIG_DIR`,
`CRIB_DATA_DIR`, `CRIB_INDEX_DIR`):

```toml
default_project = "default"
versions_keep = 20

[embed]
model = "hash"            # or "st:BAAI/bge-small-en-v1.5"

[chroma]
mode = "embedded"         # "embedded" | "shared" | "json"
```

## Tests

```bash
pip install pytest && pytest -q
```
