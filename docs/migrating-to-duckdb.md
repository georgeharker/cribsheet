# Migrating from chroma to DuckDB (0.21.x → 0.22)

crib 0.22 replaces chroma with DuckDB as its vector store and removes chroma
entirely. This page covers upgrading a machine that ran 0.21.x or earlier.
For the new setup itself, see [vector-store.md](vector-store.md).

## Why

chroma's project went into wind-down after its acquisition (no release since
1.5.9) with four unpatched server-mode CVEs, the first being CVE-2026-45829.
DuckDB was benchmarked against it on crib's real data: filtered queries took
1.4–3.7 ms against chroma's 4.3–68 ms. The design record is DESIGN §10.1.

## What you need to do

Nothing in your notes changes. The index is a rebuildable cache, so there is
no data migration: the first 0.22 start re-embeds what is on disk with your
local embedder. That costs no LLM calls, and takes about 12 s for 4,600 chunks
on MPS.

Per machine:

1. **Upgrade crib.** For example, `uv tool install --upgrade cribsheet`, or
   `uv pip install --upgrade cribsheet` in the venv crib runs from. (Dev
   releases need `--prerelease allow`.) This also pulls in `duckdb` and
   `polyllmkit>=0.2.2`.

2. **Update `~/.config/crib/config.toml`.** Delete the `[chroma]` table; if you
   leave it, crib ignores it and warns once. Embedded chroma was the old
   default and embedded DuckDB is the new one, so a config that never set
   `[chroma]` needs no change. If you ran
   chroma in shared mode, use DuckDB's shared mode instead:

   ```toml
   # before (0.21.x)
   [chroma]
   mode = "shared"
   port = 7733

   # after (0.22)
   [store]
   backend = "duckdb"   # the default; can be omitted
   [duckdb]
   mode = "shared"      # or leave out for embedded
   port = 7734          # default; a different port than chroma's 7733
   ```

   `[chroma].allow_unpatched_remote` and `CRIB_ALLOW_UNPATCHED_CHROMA_REMOTE`
   no longer exist. There is nothing to opt into: the DuckDB server is
   loopback-only and token-protected.

3. **Restart the daemon** so it starts on the new code and config, from a
   shell: `mcp-combiner restart-server cribsheet` (or however you supervise
   it). If the old `crib-chroma` server is still held by sharedserver, release
   it: `sharedserver admin stop crib-chroma`.

4. **Check it.** `crib info` reports the store as `duckdb (embedded)` or
   `duckdb (shared)`, and a `crib note lookup` returns hits once the first
   reconcile has finished. In shared mode, `GET http://127.0.0.1:7734/`
   returns 200.

5. **Clean up (optional).** Once you are happy:
   - Delete `~/.cache/crib/chroma` (the old index).
   - Uninstall `chromadb` from crib's environment, for example
     `uv pip uninstall chromadb`.
   - Remove any `crib-chroma` entry you added to an mcp-combiner
     `sharedServers` block. If you want the DuckDB server supervised the same
     way, point that entry at `python -m crib.store_quack` and set
     `[duckdb] launch = false`.

## Things that changed alongside

- **`crib store migrate` is gone.** It copied an index between backends;
  re-embedding is cheap enough that it was not worth keeping.
- **Startup is faster on warm machines.** Three per-machine caches now live in
  the DuckDB file: embeddings, parsed symbol-index records, and a generation
  ledger. See [vector-store.md](vector-store.md#what-lives-where).
- **Background generation backs off.** Inputs whose description failed are
  retried after 1 h, then 4 h, 16 h, about 2.7 days, and at most every 7 days.
  A daemon start describes at most `[generate] backlog_budget` files (50).
- **`[generate] timeout` now reaches the provider**, so a hung endpoint is
  abandoned after that many seconds (default 90).
- **crib never forks.** Subprocesses go through `posix_spawn`. Forking a
  process that has loaded macOS's Network.framework can crash the child.

## Mixed versions across synced machines

Machines that share notes through `crib memory sync` can upgrade one at a
time. The synced data (notes, decisions, plans, learnings, symbol-index TOMLs)
is unchanged, and each machine's index is its own. A 0.21.x machine keeps
using chroma until it is upgraded.

## Rolling back

Install 0.21.6 and put the `[chroma]` table back. If `~/.cache/crib/chroma` is
still there, chroma picks it up as it was. If you deleted it, the first start
rebuilds it the same way.
