# Bundles and confined mode: crib for sandboxed review

A **bundle** is a self-contained, disposable crib root handed to a sandbox (a
locked-down VM or container) so an agent can review possibly hostile code with
the whole code index at hand, writing only to its own copy. Design:
"Transient projects, out-of-store homes, and code overlays".

## The flow

On the **trusted side** (the machine with your crib store):

```bash
crib bundle create /bundles/pr-123 \
    --overlay toucan:pr-123 \            # copy `toucan` as the writable project toucan@pr-123
    --context llmkit --context mcp-combiner \   # read-only context projects
    --context-to /ro/pr-123 --context-mount /run/ro \  # optional: a separate read-only mount
    --expires 7d --max-retention 30d
```

This writes the bundle: the overlay's notes and symbol records, the context
copies, a config (embedded store, only the overlay writable, daemon off, the
same embedding model), a ready store in `index/`, the vectors as
`data/embed-cache.parquet`, and a manifest. The bundle is **registered**; the
trusted daemon deletes it when its expiry passes (`crib bundle list`,
`crib bundle delete DIR` to drop it early). A session may extend the expiry,
but never past `--max-retention`, a cap the trusted registry holds (a bundle
whose manifest is missing or unreadable is deleted once the cap passes). Add a `[generate]` section to the
bundle's `config/config.toml` if the sandbox should describe symbols (e.g. an
openai-compatible endpoint at a key-holding proxy; crib sends a placeholder
bearer when no key is configured).

### Writable and read-only projects

A bundle has two projects dirs:

```
<bundle>/data/projects/<overlay>/   the writable project (in --root)
<context dir>/<project>/            each read-only project, WHOLE: stub, notes,
                                    design, plans, symbol records
```

The context dir is `context_dir` in the bundle's `config/config.toml` (from
`--context-mount`; default `$ROOT/context`, or the `--context-to` path). It is
the one base the read-only projects are rehomed by: mount the copy anywhere and
set that line. A project found there is read-only whatever `writable` says, and
a same-named dir in the root can't shadow it; crib never writes there, so it
can be mounted read-only.

In the **sandbox**:

```bash
crib --root /bundle --store /scratch/crib project index -p toucan@pr-123 -P /checkout --budget 300
crib --root /bundle --store /scratch/crib --mcp      # MCP over stdio for the review agent
crib --root /bundle bundle expire --expires 14d     # keep it longer (this bundle only)
```

`project index` runs **phased** under `--root`: the changed files' structure,
then edges re-resolved (`dangling` lists calls whose target the change removed
or renamed), then descriptions; `phases` says what each left unfinished.

## What confined mode guarantees

With `--root DIR` (or `$CRIB_ROOT`):

- crib writes nothing outside `DIR`, except the store if `--store` moves it
  (`$CRIB_STORE`; an inherited `$CRIB_INDEX_DIR` is ignored);
- only the projects in the bundle's `writable` list accept writes; context
  projects (the `context_dir`) and anything homed outside `DIR` are
  read-only;
- a checkout's own `.crib` files are never read or written (projects are
  named explicitly; a planted nested `.crib` can't hide files), and files
  whose real path escapes the checkout are skipped;
- no git sync, no shared store server, no harness-memory mirror, no watchers
  outside `DIR`, and CLI verbs run in-process (there is no daemon to attach to);
- config outside `DIR` is not read: `$CRIB_FACETS_FILE`, and `lsp.json` /
  `grammar.json` come from `DIR/config` (the bundle's copy decides which
  language servers run).

The trusted side reads back only a bundle's expiry (the manifest is writable
in the sandbox: it is read as a small regular file, never through a symlink,
and capped by `--max-retention`), writes nothing through a symlink, and
deletes only paths from its own registry.

Inside the sandbox, the `writable` list lives in the bundle's `config/`: unless
that is mounted read-only, a hostile agent there can edit it, so treat it as
advisory. What
holds against that is crib's root check (nothing written outside `DIR` or
`--store`) and the sandbox's read-only mounts.

## What the sandbox (VM) must provide

crib's guarantees cover crib's own reads and writes. The VM is the real
boundary, and should:

- mount the bundle read-write and any context copy read-only, and nothing of
  the trusted store; mount the bundle's `config/` read-only too (bind it over
  itself): it holds what decides what runs and what may be written
  (`config.toml` with the `writable` list, `lsp.json`, `grammar.json`,
  `facets.toml`), and a read-only `config/` is what makes `writable` binding
  rather than advisory;
- point `HOME`, `XDG_*` and model caches at writable scratch inside the VM:
  language servers (pyright, rust-analyzer, tsc, ...) and the embedding
  runtime keep caches there, outside crib's control, and some execute
  project config from the checkout under review;
- keep `--store` on the VM's own disk: DuckDB needs working file locks, which
  shared or virtualised mounts (virtiofs, 9p) may not provide reliably;
- carry the embedding model's weights and the language-server binaries in
  the image (no network), with model access, if any, through a proxy.
