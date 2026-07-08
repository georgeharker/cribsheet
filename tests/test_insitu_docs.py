"""Docs indexed IN-SITU from a source repo: source is master, never copied.
The note's bytes live in the repo; crib holds only the index; read/locate resolve
back to the repo file; edits/deletes on the source reconcile in."""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.sources import SRC_PREFIX, SourceRoots, src_relpath
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def run(coro):
    return asyncio.run(coro)


def _repo(tmp_path, project="proj", docs=("README.md",)):
    repo = tmp_path / "myrepo"
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    (repo / ".crib").write_text(
        f"project: {project}\ndocs:\n  - README.md\n  - docs/**/*.md\n")
    (repo / "README.md").write_text("# MyRepo\n\nThe widget frobnicates gaskets.\n")
    (repo / "docs" / "guide.md").write_text("# Guide\n\nHow to calibrate the flux.\n")
    return repo


def test_src_relpath_shape():
    assert src_relpath("myrepo", "docs/guide.md") == "sources/myrepo/docs/guide.md"
    assert src_relpath("myrepo", "README.md").startswith(SRC_PREFIX)


def test_index_insitu_no_copy_and_resolves_to_source(crib, tmp_path):
    repo = _repo(tmp_path, project="proj")
    res = run(crib.index_docs_insitu(cwd=repo))
    assert res["project"] == "proj"
    assert res["docs"] == 2 and res["changed"] == 2
    assert res["prefix"] == "sources/myrepo/"

    # Nothing was copied into the crib notes tree.
    nd = crib.notes_dir("proj")
    assert not (nd / "sources").exists()

    # abspath resolves the source-anchored relpath back to the REPO file.
    rel = "sources/myrepo/README.md"
    assert crib.abspath("proj", rel) == repo / "README.md"
    assert crib.locate(rel, project="proj") == str(repo / "README.md")

    # The registry persisted the prefix -> repo root.
    reg = SourceRoots(crib.paths.project_dir("proj") / "doc-sources.json")
    assert reg.all()["sources/myrepo/"] == str(repo)


def test_read_returns_repo_bytes(crib, tmp_path):
    repo = _repo(tmp_path, project="proj")
    run(crib.index_docs_insitu(cwd=repo))
    body = crib.read_note("sources/myrepo/docs/guide.md", project="proj")
    assert "calibrate the flux" in body


def test_edit_source_reindexes(crib, tmp_path):
    repo = _repo(tmp_path, project="proj")
    run(crib.index_docs_insitu(cwd=repo))
    (repo / "README.md").write_text("# MyRepo\n\nEntirely new prose about turbines.\n")
    res = run(crib.index_docs_insitu(cwd=repo))
    assert "sources/myrepo/README.md" in res["changed"] if isinstance(
        res["changed"], list) else res["changed"] >= 1
    assert "turbines" in crib.read_note("sources/myrepo/README.md", project="proj")


def test_delete_source_prunes(crib, tmp_path):
    repo = _repo(tmp_path, project="proj")
    run(crib.index_docs_insitu(cwd=repo))
    (repo / "docs" / "guide.md").unlink()
    res = run(crib.index_docs_insitu(cwd=repo))
    assert res["removed"] >= 1
    assert not crib._indexed_relpaths("proj", "sources/myrepo/") & {
        "sources/myrepo/docs/guide.md"}


def test_sweep_prunes_out_of_glob_docs(crib, tmp_path):
    """`docs:` globs are AUTHORITATIVE: a doc indexed earlier but no longer matching
    the globs is dropped on the next sweep — even though its source file still exists
    (A2). Keeps the sweep and the watcher agreeing on which docs are indexed."""
    repo = _repo(tmp_path, project="proj")          # globs: README.md, docs/**/*.md
    run(crib.index_docs_insitu(cwd=repo))
    idx = crib._indexed_relpaths("proj", "sources/myrepo/")
    assert "sources/myrepo/docs/guide.md" in idx
    # narrow the globs so docs/** no longer qualifies; guide.md stays ON DISK
    (repo / ".crib").write_text("project: proj\ndocs:\n  - README.md\n")
    res = run(crib.index_docs_insitu(cwd=repo))
    assert res["removed"] >= 1
    assert (repo / "docs" / "guide.md").exists()     # the source file is untouched
    assert "sources/myrepo/docs/guide.md" not in crib._indexed_relpaths(
        "proj", "sources/myrepo/")
    assert "sources/myrepo/README.md" in crib._indexed_relpaths("proj", "sources/myrepo/")


def test_watcher_dispatch_reindexes_doc(crib, tmp_path):
    """The CodeWatcher hands \x00doc\x00-tagged changes to _on_code_change, which
    reindexes the in-situ doc on THIS loop (no thread/asyncio.run)."""
    repo = _repo(tmp_path, project="proj")
    run(crib.index_docs_insitu(cwd=repo))
    (repo / "README.md").write_text("# MyRepo\n\nFresh content about pistons.\n")
    # simulate a coalesced watcher batch for a doc edit
    changes = {"\x00doc\x00README.md": (str(repo), False)}
    run(crib._on_code_change("proj", changes))
    assert "pistons" in crib.read_note("sources/myrepo/README.md", project="proj")
