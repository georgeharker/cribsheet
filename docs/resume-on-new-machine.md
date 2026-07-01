# Resume on a new machine — update, pull, ingest, index

Runbook to stand up the retrieval/generation work on a faster box: update source,
pull all crib data + `.crib`-tagged repos, ingest their docs, and (re)build every
index. Commands are copy-pasteable; adjust paths if your checkout layout differs.

## 0. Prerequisites (once)

- **Repos cloned** at the expected paths (see the loop in §3). The `.crib`-tagged
  repos: `cribsheet`, `zsh-ai`, `zdot`, `dotfiler`, `sharedserver`, `svg-mcp`,
  `mcp-companion`.
- **Dotfiles deployed** so crib config is in place:
  - `~/.config/crib/config.toml` — must have the `[generate]` and `[retrieve]`
    blocks (generation provider + `keyword_labels=["keywords"]`,
    `keyword_weight=0.3`).
  - `~/.config/crib/models.toml` — llmkit providers/profiles (the `opencode-qwen`
    zen provider). Kept separate from `~/.config/zsh-ai/models.toml`.
- **`export OPENCODE_API_KEY=…`** in the shell (zen auth; crib holds no key).
- **Shared Chroma:** config uses `[chroma].mode = "shared"`, which needs the
  `sharedserver` binary on `PATH`. If it isn't installed on the new box, either
  install it or set `[chroma].mode = "embedded"` in `config.toml` for a
  self-contained store.
- **Python env** for cribsheet (below).

## 1. Update source (cribsheet + llmkit submodule)

```sh
cd ~/Development/cribsheet
git pull --ff-only origin main
git submodule update --init --recursive          # vendor/llmkit → pinned commit

# install crib + the extras this work needs
uv sync                                            # or: pip install -e '.[full,generate]'
pip install -e './vendor/llmkit[anthropic]'        # zen adapter (native Messages API)
# optional real embedder (bge via ONNX): pip install -e '.[embed]'
```

**Restart the daemon after updating source.** The warm MCP daemon (sharedserver
name `cribsheet`) keeps running the code it started with, so CLI calls (new code)
hit a stale tool schema — e.g. `import` fails with `Unexpected keyword argument
cwd`. Bounce it so it reloads:

```sh
sharedserver stop cribsheet        # next crib call respawns from the new code
# (won't stop? sharedserver admin kill cribsheet)
```

Alternatively, add `--no-daemon` to any verb to run current code in-process and
skip the daemon entirely — recommended for the one-shot ingest/index batch below.

Update the other source repos too (so imported docs are current):

```sh
for r in ~/Development/zsh/zsh-ai ~/Development/zsh/zdot ~/Development/zsh/dotfiler \
         ~/Development/neovim-plugins/sharedserver ~/Development/svg-mcp \
         ~/Development/neovim-plugins/mcp-companion; do
  echo "== $r =="; git -C "$r" pull --ff-only 2>&1 | tail -1
done
```

## 2. Pull the crib data repo (notes)

First time on this machine — join the shared note repo (sets remote + the
frontmatter merge driver, then pulls):

```sh
crib setup --remote git@github.com:georgeharker/.crib.git
```

Already joined — just pull (notes only; indexes are gitignored/regenerable):

```sh
crib pull                                          # fetches notes, then reindexes
```

## 3. Ingest all `.crib`-tagged repos (creates/fills each project)

`crib import` reads the `.crib` in (or above) the cwd, so run it from each repo.
This copies the declared docs into that repo's crib project.

```sh
for r in ~/Development/cribsheet ~/Development/zsh/zsh-ai ~/Development/zsh/zdot \
         ~/Development/zsh/dotfiler ~/Development/neovim-plugins/sharedserver \
         ~/Development/svg-mcp ~/Development/neovim-plugins/mcp-companion; do
  echo "== import $(basename "$r") =="; ( cd "$r" && crib import )
done
```

## 4. Reindex everything (embeddings + BM25)

`reconcile` sweeps every project — catches the just-imported docs and any offline
changes. Idempotent (hash-gated).

```sh
crib reconcile
```

## 5. Build the LLM indexes per project

Keyword index (BM25 side) for every project — this is the shipped default, so
generate it everywhere. Summary index (dense aliases) is optional/experimental —
generate it where you want to test the paraphrase hypothesis (the volume corpora
are the interesting case, since cribsheet's own recall is saturated).

```sh
PROJECTS="cribsheet zsh-ai zdot dotfiler sharedserver svg-mcp mcp-companion"

for p in $PROJECTS; do
  echo "== keyword_index: $p =="
  crib elaborate keywords -p "$p"                  # one zen call per section
done

# optional — dense summary aliases (off by default; activate via
# [retrieve].summary_labels once measured to help on the bigger corpora)
for p in $PROJECTS; do
  echo "== summary_index: $p =="
  crib summarize summary -p "$p"
done
```

Generation is concurrent + per-call-timeout + error-isolated. On a faster box,
raise throughput in `config.toml`:

```toml
[generate]
concurrency = 12        # parallel zen calls (default 6)
timeout = 90            # per-call seconds
```

## 6. Verify

```sh
crib projects                                      # all 7 present
crib --json lookup "reciprocal rank fusion" -p cribsheet -k 3
# retrieval-quality harness (baseline + per-index lift):
python scripts/eval_retrieval.py                         # default config bars
python scripts/eval_retrieval.py --lift keywords --elab-weight 0.3
python scripts/eval_retrieval.py --lift-summaries summary --summary-weight 0.1
```

## Notes / gotchas

- **Indexes are gitignored** in the data repo (regenerable, churn while tuning),
  so they do **not** come down with `crib pull` — §5 rebuilds them locally.
- **Provenance:** freshly generated index TOMLs record the resolved provider
  (e.g. `qwen3.6-plus`), so you can tell them apart across machines/models.
- **The summary finding to retest here:** dense aliases were net-negative on
  cribsheet (71 clustered sections, saturated recall). The reason to add the
  volume corpora is to test them where recall is *not* saturated — build eval
  cases spanning `zsh-ai`/`sharedserver`/etc. and run `--lift-summaries`.
- **Daemon:** the MCP server (`crib --mcp --http`) is registered via the
  combiner; CLI verbs attach to it automatically. Use `--no-daemon` to force
  in-process (e.g. to exercise freshly edited code without restarting the daemon).
```
