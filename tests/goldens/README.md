# Corpus goldens

Frozen, **URL-pinned** symbol_index snapshots of a real project, used as the heavy
structural regression gate for the code-index pipeline (extract → store → query).

Each `<project>/` holds:
- `symbol_index/*.toml` — the frozen index (the snapshot).
- `meta` — three lines: `project`, **git remote URL**, `rev`. The URL (not a local path)
  is what makes these portable: `compare` clones the repo from GitHub, checks out the
  pinned rev, re-indexes it, and diffs against the frozen `symbol_index`.

## Pin a TAG, not a SHA

`rev` is a **release tag** (`v0.9.1`), and that is the durable choice — `compare` clones
before it checks out, so anything git can resolve works, but the two forms fail very
differently when the remote moves:

- A **bare SHA** stops resolving the moment history is rewritten, and `compare` then dies
  at `checkout` — the gate reports an unreachable ref, not a verdict about the code. That
  is silent rot: the gate looks present and tests nothing.
- A **tag** keeps resolving across a rewrite. If a rewrite changes what the tag points at,
  the re-index drifts and the gate FAILS LOUDLY, which is the outcome you want: a real
  signal to review and re-capture.

Both pins here were dead on arrival for exactly that reason (a rewrite across these repos
invalidated `a1577b2` and `21c573e80e97`), which is why they were re-captured against
tags. **After a history rewrite, re-capture** — a tag that survived still needs its
snapshot re-derived from whatever the tag now names.

## What's committed, and why

- `cribsheet` — self, pinned to tag `v0.9.1`. Re-derived by `compare`: 2334 symbols,
  IDENTICAL (byte-clean, zero edge-wobble).
- `mcp-companion` — public repo, ours, pinned to tag `v0.9.4`. Re-derived by `compare`:
  1831 symbols, STRUCTURALLY IDENTICAL (1 edge-wobble, LSP noise).

Both lines above mean a `compare` run was executed against the committed snapshot, not
that it looked right when captured. Capture and re-derivation are different claims —
`capture` writing 2334 files says nothing about whether 2334 come back.

A committed golden MUST pin to a rev reachable on the remote — that's the whole point of
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

## Regenerating (after an intentional format change, or a history rewrite)

```
python scripts/snapshot_harness.py capture <git-url> <tag> tests/goldens/<project>
```

then review the diff before committing. Pass the release **tag**, not a SHA — `capture`
writes whatever rev you hand it straight into `meta`, so a SHA passed here is the pin
`compare` inherits.
