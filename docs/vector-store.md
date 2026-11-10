# The vector store — DuckDB setup

crib keeps its search index in **DuckDB**. The index holds chunk embeddings,
chunk text and metadata, plus a few per-machine caches. It is a **rebuildable
cache**: the notes, decisions, plans and learnings on disk are the truth, and
an empty store is refilled by the startup reconcile. Rebuilding re-embeds with
the local embedder only and makes no LLM calls. (Design rationale: DESIGN §10.1.)

DuckDB is the only vector store. chroma was removed in 0.22.0; coming from an
older release, see [migrating-to-duckdb.md](migrating-to-duckdb.md).

## Choosing a mode

| | `embedded` (default) | `shared` |
|---|---|---|
| Who opens the file | the crib process itself | a crib-owned DuckDB server, `crib-duckdb` |
| Processes per store | one | any number of crib clients |
| Needs | nothing beyond the `duckdb` package | the `sharedserver` binary on `PATH` |
| Query cost | lowest | about 4–5 ms extra per query, slower writes |

**Use `embedded`** when one crib daemon serves everything, which is the normal
setup: Claude, the plugins and the `crib` CLI all attach to the same daemon. A
second process that opens the same file in embedded mode gets a clear "store is
locked" error naming the file.

**Use `shared`** when several crib processes need the store at once, for
example `crib --no-daemon` runs beside a live daemon, or more than one daemon
on a box.

## Configuration

In `~/.config/crib/config.toml`:

```toml
[store]
backend = "duckdb"        # the only store (the default; may be omitted)

[duckdb]
mode = "embedded"         # "embedded" (default) | "shared"
# The keys below only matter in shared mode.
server_name = "crib-duckdb"  # the sharedserver name
port = 7734               # crib's 773x band; the MCP daemon is 7732
host = "127.0.0.1"        # loopback only; anything else is refused at startup
grace_period = "1h"       # keep the server warm this long after the last client
launch = true             # false: attach to a server something else supervises
attach_timeout = 60.0     # with launch = false: seconds to wait for it
```

A leftover `[chroma]` table is ignored with a one-time warning.

## Shared mode in detail

With `launch = true`, crib starts the server itself through sharedserver,
refcounted like the daemon:

```bash
sharedserver use crib-duckdb --grace-period 1h -- python -m crib.store_quack
```

`python -m crib.store_quack` reads host, port and paths from crib's own
config, so it needs no arguments. On shutdown crib releases its reference
(`sharedserver unuse crib-duckdb`) and the server stops after the grace period.

With `launch = false`, crib only attaches. Run the same command under your own
supervisor, for example an mcp-combiner `sharedServers` entry whose command is
`python -m crib.store_quack`. crib waits up to `attach_timeout` seconds for it.

**Security posture.** The server binds to loopback only and requires a token,
kept in a mode-0600 file beside the database. Once it is serving it locks
itself down: no file access, no extension installs, and its configuration is
locked. A token holder can therefore reach only the store, not the filesystem.

One gap is known and accepted: a token holder can also call Quack's own
listener controls, starting a second listener with a token of their choosing,
or stopping crib's (the next client operation then relaunches it). DuckDB has
no way to block individual functions for client sessions. It adds nothing to
what the token already implies: reading the token takes same-user access to
crib's cache directory, which already gives the database file itself and the
server process. A test pins the behaviour, so it shows up if DuckDB changes.

**Health check.** `GET http://127.0.0.1:7734/` returns 200 while the server is
up. `sharedserver list` shows its refcount and grace state. If the server goes
away, clients reconnect on their next operation (with `launch = true` crib
starts it again; with `launch = false` it waits up to `attach_timeout`), and
each reconnect is logged and counted in `crib status`.

**First run needs the network once.** The shared path uses DuckDB's `quack` and
`httpfs` extensions, which DuckDB downloads from extensions.duckdb.org on first
use and caches under crib's own directory. For a container image or an offline
machine, fetch them ahead of time with `crib store install-extensions`.
Embedded mode needs no extensions.

## What lives where

Everything is under the index dir (`~/.cache/crib`, or `$CRIB_INDEX_DIR`):

```text
~/.cache/crib/duckdb/
├── crib.duckdb        the store: chunks, embeddings and the caches below
├── crib.duckdb.wal    DuckDB's write-ahead log
├── quack.token        shared-mode token (0600)
├── extensions/        downloaded DuckDB extensions
└── secrets/           DuckDB's secrets dir (0700)
```

crib passes these directories explicitly on every connection, so it never
reads or writes `~/.duckdb`.

Besides the chunk table, the database holds three per-machine caches. They are
never synced and are safe to lose:

| Table | Holds | Effect |
|---|---|---|
| `crib_embed_cache` | vectors for text already embedded (no index) | a warm start embeds only new or changed text |
| `crib_record_cache` | parsed symbol-index TOML records | daemon start and revalidation skip unchanged files |
| `crib_gen_ledger` | failed generation inputs, per provider | background generation backs off from them |

The generation ledger's backoff runs 1 h → 4 h → 16 h → about 2.7 days, then
caps at 7 days, keyed on the input's hash and the provider. Only background
generation consults it; an explicit request always runs, and still records
its outcome. `[generate] backlog_budget` (default 50) caps how many files'
describes one daemon start attempts.

Ledger rows for files that no longer need a description are dropped at each
daemon start, and the embedding cache drops vectors from any embedder other
than the current one.

A small `crib_meta` table records the file's schema version, the record-cache
format, and which embedder wrote the vectors. On a schema change crib drops
and rebuilds every table above; on a record-format change it empties only the
record cache; and on an embedder change (even at the same vector size) the
next full reindex recreates the chunks.

Search is exact: DuckDB scores every row the filters select with
`array_cosine_similarity` and keeps the top k. Filters, including `tags=`, run
inside that query, so a selective tag still returns k results. Nothing
approximate is persisted, so no index format is tied to a DuckDB version.

`crib status` reports the store under `store_stats`: rows per table, the
recorded versions, the file and WAL sizes, and in shared mode the server
address and reconnect count. After a startup reconcile that changed anything,
crib checkpoints the WAL into the file.

## Recovery

Because the store is a cache, deleting it is a supported fix:

```bash
sharedserver admin stop cribsheet       # stop the daemon (and crib-duckdb, if shared)
sharedserver admin stop crib-duckdb
rm -f ~/.cache/crib/duckdb/crib.duckdb ~/.cache/crib/duckdb/crib.duckdb.wal
```

The next client starts the daemon again, which re-embeds from disk. With an
MPS or GPU embedder that takes about 12 s for 4,600 chunks; a CPU-only
embedder is slower. The symbol index's
LLM descriptions live in the TOML files, not the store, so nothing has to be
regenerated.
