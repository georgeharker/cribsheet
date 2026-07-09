# Corpus goldens

Frozen, **URL-pinned** symbol_index snapshots of a real project, used as the heavy
structural regression gate for the code-index pipeline (extract → store → query).

Each `<project>/` holds:
- `symbol_index/*.toml` — the frozen index (the snapshot).
- `meta` — three lines: `project`, **git remote URL**, `sha`. The URL (not a local path)
  is what makes these portable: `compare` clones the repo from GitHub at the pinned SHA,
  re-indexes it, and diffs against the frozen `symbol_index`.

## What's committed, and why

- `cribsheet` — self, pinned to `c9ec609` on the pushed `codestore-refactor` branch.
  Verified IDENTICAL (1157 symbols, byte-clean). **Durability caveat:** the pin is a
  *branch* commit, not `main`. If the branch is later squash-merged and deleted, that SHA
  becomes unreachable and `compare` can't clone it — re-capture at the merge commit then
  (`capture <url> <merge-sha> tests/goldens/cribsheet`).
- `mcp-companion` — public repo, ours, byte-stable, pinned to a SHA **on GitHub**.
  Verified IDENTICAL.

A committed golden MUST pin to a SHA reachable on the remote — that's the whole point of
URL pinning. (An early self-golden attempt pinned to an unpushed local commit; `compare`
cloned fine but couldn't `checkout` it. Pushing the branch fixed it.)

Deliberately NOT committed:
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
