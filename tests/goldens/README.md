# Corpus goldens

Frozen, **URL-pinned** symbol_index snapshots of a real project, used as the heavy
structural regression gate for the code-index pipeline (extract → store → query).

Each `<project>/` holds:
- `symbol_index/*.toml` — the frozen index (the snapshot).
- `meta` — three lines: `project`, **git remote URL**, `sha`. The URL (not a local path)
  is what makes these portable: `compare` clones the repo from GitHub at the pinned SHA,
  re-indexes it, and diffs against the frozen `symbol_index`.

## What's committed, and why

- `mcp-companion` — public repo, ours, byte-stable, and pinned to a SHA that is **on
  GitHub** (so `compare` can clone + reproduce it anywhere). Verified IDENTICAL.

Deliberately NOT committed:
- **cribsheet (self):** tempting, but a committed golden must pin to a SHA that exists on
  the remote. The refactor-branch commits aren't pushed, so a self-golden would pin to an
  unreachable SHA and `compare` couldn't clone it. Add one once a representative commit is
  pushed to `origin` (capture at that SHA, then commit).
- **Other repos' indexes** would leak their structure into this repo's history and aren't
  portable — they stay in `~/.cache/crib-goldens/` (regenerable via the harness), for
  ad-hoc thorough validation.
- **zdot** / other zsh projects — the shuck autoload reindex is nondeterministic.
- **svg-mcp / sharedserver** — pyright/rust-analyzer resolve a few cross-file edges
  nondeterministically (they'd only ever be "STRUCTURALLY IDENTICAL", never byte-clean).

## Running the gate

Opt-in (clones from GitHub + reindexes — needs network + an LSP + a couple minutes):

```
CRIB_CORPUS_GOLDENS=1 pytest tests/test_corpus_goldens.py
```

Or drive the harness directly:

```
python scripts/snapshot_harness.py compare tests/goldens/mcp-companion
```

`compare` fails only on **structural** drift (added/removed symbols or a non-edge field
change); LSP cross-file edge wobble is reported but tolerated as noise. The fast,
always-on structural gates are the unit suite + `tests/test_notestore_snapshot` (notes) +
`tests/test_codeindex` (extraction) — this is the deliberate deep check.

## Regenerating (after an intentional format change)

```
python scripts/snapshot_harness.py capture <git-url> <sha> tests/goldens/<project>
```

then review the diff before committing.
