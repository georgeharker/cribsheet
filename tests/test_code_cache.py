"""Resident code-index cache + watcher coalescing + reindex lock.

The cache keeps parsed symbols and description embeddings resident so a `code_*`
query need not re-parse every TOML or re-embed every description; it invalidates
by an in-process epoch (`trust`) or the symbol_index dir signature (`scan`). The
code watcher coalesces bursts per project; `_index_file_sync`/`_drop_file` bump
the epoch under a per-project lock.
"""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.codeindex import SymbolIndex
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def run(coro):
    return asyncio.run(coro)


def _write(crib, project, fqname, description, content_hash):
    SymbolIndex(crib.paths.project_dir(project)).write({
        "fqname": fqname, "name": fqname.split(".")[-1], "kind": "function",
        "lang": "python", "module": fqname.rsplit(".", 1)[0], "parent": "",
        "content_hash": content_hash, "file": "pkg/mod.py", "line": 10,
        "signature": f"def {fqname.split('.')[-1]}():", "description": description,
        "container": [], "calls": [], "called_by": [], "name_terms": [fqname.split(".")[-1]]})


# ── Resident cache: parse + embed reuse ───────────────────────────────────────
def test_lookup_builds_resident_cache_and_reuses_it(crib):
    _write(crib, "p", "pkg.alpha", "computes the alpha metric", "h_alpha")
    _write(crib, "p", "pkg.beta", "renders the beta view", "h_beta")

    hits = crib.code_lookup("alpha metric", project="p")
    rc = crib._code_cache.get("p")
    assert hits and rc is not None                      # cache populated
    assert set(rc.emb) == {"computes the alpha metric", "renders the beta view"}
    assert rc._dense is not None                        # dense vectors materialised

    crib.code_lookup("beta view", project="p")          # same freshness token
    assert crib._code_cache["p"] is rc                  # SAME cache object — no reload


def test_reload_reuses_unchanged_embedding_vectors(crib):
    _write(crib, "p", "pkg.alpha", "computes the alpha metric", "h_alpha")
    _write(crib, "p", "pkg.beta", "renders the beta view", "h_beta")
    crib.code_lookup("x", project="p")
    rc1 = crib._code_cache["p"]

    # A third symbol appears on disk → dir signature (scan mode) flips → reload.
    _write(crib, "p", "pkg.gamma", "summarises the gamma path", "h_gamma")
    hits = crib.code_lookup("gamma path", project="p")
    rc2 = crib._code_cache["p"]
    assert rc2 is not rc1                                # reloaded (new token)
    assert any(h["fqname"] == "pkg.gamma" for h in hits)  # new symbol is queryable
    # unchanged descriptions carried the SAME vector object across the reload (no
    # re-embed); only the new one was embedded fresh.
    assert rc2.emb["computes the alpha metric"] is rc1.emb["computes the alpha metric"]
    assert rc2.emb["renders the beta view"] is rc1.emb["renders the beta view"]
    assert "summarises the gamma path" in rc2.emb


# ── Freshness modes: scan (dir signature) vs trust (epoch) ────────────────────
def test_scan_mode_sees_on_disk_writes(crib):
    _write(crib, "p", "pkg.alpha", "alpha", "h1")
    assert crib.code_lookup("alpha", project="p")
    _write(crib, "p", "pkg.beta", "beta", "h2")          # external write, no epoch bump
    ids = {h["fqname"] for h in crib.code_lookup("beta", project="p")}
    assert "pkg.beta" in ids                             # scan catches it via dir signature


def test_trust_mode_ignores_writes_until_epoch_bump(crib):
    crib.config.retrieve.code_freshness = "trust"
    _write(crib, "p", "pkg.alpha", "alpha", "h1")
    assert crib.code_lookup("alpha", project="p")        # builds cache at epoch 0
    _write(crib, "p", "pkg.beta", "beta", "h2")          # external write, NO epoch bump
    ids = {h["fqname"] for h in crib.code_lookup("beta", project="p")}
    assert "pkg.beta" not in ids                         # trust doesn't stat; misses it
    crib._bump_code_epoch("p")                           # an in-process write would do this
    ids = {h["fqname"] for h in crib.code_lookup("beta", project="p")}
    assert "pkg.beta" in ids                             # now the reload picks it up


# ── Lock + epoch bookkeeping ──────────────────────────────────────────────────
def test_drop_file_bumps_epoch_and_invalidates(crib):
    _write(crib, "p", "a.target", "target", "h1")
    _write(crib, "p", "b.gone", "gone", "h2")
    # give b.gone a different file so _drop_file("p","b.py") removes it
    si = SymbolIndex(crib.paths.project_dir("p"))
    e = si.by_fqname("b.gone")[0]; e["file"] = "b.py"; si.write(e)
    crib.code_lookup("target", project="p")
    before = crib._code_epoch.get("p", 0)
    crib._drop_file("p", "b.py")
    assert crib._code_epoch["p"] == before + 1           # epoch bumped → cache stale


def test_revalidate_reindexes_merge_dirtied_file(crib, tmp_path, monkeypatch):
    """A merge-dirtied symbol (blank content_hash, written by the sync merge
    driver on divergent code states) forces a reindex of its file even though
    the toml is FRESHER than the source (post-pull mtime) — the mtime gate
    alone would never catch it."""
    src_root = tmp_path / "srcrepo"
    (src_root / "pkg").mkdir(parents=True)
    (src_root / "pkg" / "mod.py").write_text("def alpha():\n    pass\n")
    _write(crib, "p", "pkg.alpha", "alpha", "h1")        # toml written AFTER the source
    si = SymbolIndex(crib.paths.project_dir("p"))
    si.set_source_root(src_root)

    reindexed: list[str] = []
    monkeypatch.setattr(
        crib, "_index_file_sync",
        lambda root, rel, proj, patch_edges, existing=None: reindexed.append(rel))
    crib._revalidate("p")
    assert reindexed == []                               # fresh toml, real hash → clean

    e = si.by_fqname("pkg.alpha")[0]
    e["content_hash"] = ""                               # what the merge driver writes
    si.write(e)
    crib._revalidate("p")
    assert reindexed == ["pkg/mod.py"]                   # dirty forces the rebuild


def test_on_code_change_pumps_watched_files_into_lsp_pool(crib, monkeypatch):
    """Watcher batches reach the warm LSP sessions as didChangeWatchedFiles
    (docs §3.2) so a server that doesn't self-watch stays disk-fresh."""
    from crib import codeindex as ci
    pumped: list = []
    monkeypatch.setattr(ci._POOL, "notify_changes",
                        lambda root, changes: pumped.append((str(root), sorted(changes))))
    monkeypatch.setattr(crib, "_index_file_sync", lambda *a, **k: None)
    monkeypatch.setattr(crib, "_drop_file", lambda *a, **k: None)
    run(crib._on_code_change("p", {"pkg/mod.py": ("/src/repo", False),
                                   "pkg/gone.py": ("/src/repo", True)}))
    assert pumped == [("/src/repo", [("pkg/gone.py", 3), ("pkg/mod.py", 2)])]


def test_reconcile_rebuilds_merge_dirtied_files(crib, tmp_path, monkeypatch):
    """Post-pull reconcile eagerly rebuilds files with merge-dirtied symbols —
    and collapses many dirty symbols of one file into ONE rebuild."""
    src_root = tmp_path / "srcrepo"
    (src_root / "pkg").mkdir(parents=True)
    (src_root / "pkg" / "mod.py").write_text("def alpha():\n    pass\n")
    _write(crib, "p", "pkg.alpha", "alpha", "")          # merge-dirtied (blank hash)
    _write(crib, "p", "pkg.beta", "beta", "")            # second dirty sym, SAME file
    SymbolIndex(crib.paths.project_dir("p")).set_source_root(src_root)

    reindexed: list[str] = []
    monkeypatch.setattr(
        crib, "_index_file_sync",
        lambda root, rel, proj, patch_edges, existing=None: reindexed.append(rel))
    out = run(crib._reindex_dirty_code())
    assert out == {"p": 1}                               # one FILE rebuilt…
    assert reindexed == ["pkg/mod.py"]                   # …for two dirty symbols


def test_code_lock_is_per_project_and_stable(crib):
    assert crib._code_lock("p") is crib._code_lock("p")  # same lock reused per project
    assert crib._code_lock("p") is not crib._code_lock("q")


# ── Watcher coalescing ────────────────────────────────────────────────────────
def test_code_watcher_coalesces_burst_into_one_batch(tmp_path):
    from crib.watch import CodeWatcher

    async def scenario():
        got: list[dict] = []

        async def on_change(project, changes):
            got.append({"project": project, "changes": dict(changes)})

        cw = CodeWatcher(on_change, asyncio.get_running_loop())
        root = str((tmp_path / "repo").resolve())
        # three edits + one delete-then-recreate, all within the debounce window
        cw._schedule(("proj", root, "a.py", False))
        cw._schedule(("proj", root, "b.py", False))
        cw._schedule(("proj", root, "c.py", True))
        cw._schedule(("proj", root, "c.py", False))       # last event wins for c.py
        await asyncio.sleep(0.7)                           # > CODE_DEBOUNCE_SEC
        return got

    got = asyncio.run(scenario())
    assert len(got) == 1                                   # ONE coalesced dispatch
    assert got[0]["project"] == "proj"
    assert set(got[0]["changes"]) == {"a.py", "b.py", "c.py"}
    assert got[0]["changes"]["c.py"][1] is False           # recreate superseded the delete


def test_on_code_change_falls_back_to_revalidate_on_large_burst(crib, monkeypatch):
    revalidated: list[str] = []
    per_file: list[str] = []
    monkeypatch.setattr(crib, "_revalidate", lambda proj: revalidated.append(proj))
    monkeypatch.setattr(crib, "_index_file_sync",
                        lambda root, rel, proj, patch: per_file.append(rel))
    from crib.watch import CODE_BATCH_FALLBACK

    big = {f"f{i}.py": ("/root", False) for i in range(CODE_BATCH_FALLBACK + 1)}
    run(crib._on_code_change("p", big))
    assert revalidated == ["p"] and not per_file           # collapsed to one sweep

    small = {"one.py": ("/root", False), "two.py": ("/root", False)}
    run(crib._on_code_change("p", small))
    assert sorted(per_file) == ["one.py", "two.py"]         # reindexed file-by-file
