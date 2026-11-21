"""Extensionless-source discovery (grammar map drives enumeration) + the
keep-prior-on-empty guard that stops a flaky LSP pass from pruning real symbols."""

from __future__ import annotations

import asyncio

import pytest

from crib import codeindex as ci
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


def test_enumeration_includes_extensionless_grammar_matches(crib, tmp_path, monkeypatch):
    # pretend a zsh server is installed so `served` includes zsh, regardless of host
    monkeypatch.setattr(ci, "resolve_command", lambda spec: ["fake"]
                        if "zsh" in (spec.get("extensionToLanguage") or {}).values() else None)
    repo = tmp_path / "repo"
    (repo / "functions").mkdir(parents=True)
    (repo / "lib.zsh").write_text("function a() { :; }\n")               # extension
    (repo / "functions" / "fzf_rg").write_text("#!/usr/bin/env zsh\nfzf_rg() { :; }\n")
    (repo / "functions" / "_zdot").write_text("#compdef zdot\n_zdot() { :; }\n")  # marker
    (repo / "functions" / "readme").write_text("just prose, no shell\n")   # not shell
    (repo / "functions" / "img.zwc").write_bytes(b"\x00\x01binary")        # binary junk

    files = crib._enumerate_code_files(repo, crib._detect_code_globs(repo))
    names = {p.name for p in files}
    assert {"lib.zsh", "fzf_rg", "_zdot"} <= names       # all three discovered
    assert "readme" not in names and "img.zwc" not in names


def test_keep_prior_on_empty_extraction(crib, tmp_path, monkeypatch):
    """A still-present, non-trivial file that extracts to ZERO symbols keeps its prior
    symbols (flaky LSP guard) instead of pruning them."""
    proj = "p"
    root = tmp_path / "repo"; (root).mkdir()
    src = root / "thing.zsh"
    src.write_text("function frobnicate() {\n  echo hi\n  local x=1\n  return 0\n}\n")
    # seed a prior symbol for this file
    SymbolIndex(crib.paths.project_dir(proj)).write({
        "fqname": "thing.frobnicate", "name": "frobnicate", "kind": "function",
        "lang": "zsh", "module": "thing", "parent": "", "content_hash": "old",
        "file": "thing.zsh", "line": 1, "signature": "function frobnicate()",
        "description": "does the frobnicate", "container": [], "calls": [],
        "called_by": [], "name_terms": ["frobnicate"]})

    # simulate a flaky pass: extract_file returns [] for a file that still has code
    monkeypatch.setattr(ci, "extract_file", lambda root, rel, **k: [])
    res = crib._index_code_file_tracked(root, "thing.zsh", proj, False)
    assert res.get("skipped") == "empty-extract-kept-prior"
    # the prior symbol survived
    assert SymbolIndex(crib.paths.project_dir(proj)).by_fqname("thing.frobnicate")


def test_genuinely_empty_file_still_prunes(crib, tmp_path, monkeypatch):
    proj = "p"
    root = tmp_path / "repo"; root.mkdir()
    (root / "gone.zsh").write_text("# only a comment now\n")   # no real code left
    SymbolIndex(crib.paths.project_dir(proj)).write({
        "fqname": "gone.old", "name": "old", "kind": "function", "lang": "zsh",
        "module": "gone", "parent": "", "content_hash": "old", "file": "gone.zsh",
        "line": 1, "signature": "", "description": "d", "container": [], "calls": [],
        "called_by": [], "name_terms": ["old"]})
    monkeypatch.setattr(ci, "extract_file", lambda root, rel, **k: [])
    crib._index_code_file_tracked(root, "gone.zsh", proj, False)
    assert not SymbolIndex(crib.paths.project_dir(proj)).by_fqname("gone.old")


def test_an_empty_extract_is_recorded_and_doctor_names_it(crib, tmp_path, monkeypatch):
    """A code file the server returns NO symbols for (a server that couldn't load
    its runtime: typescript-language-server with no tsserver) is retried once,
    then recorded, reported by doctor, and cleared when it extracts again."""
    from crib import emptyextract
    from crib.doctor import diagnose

    proj = "p"
    root = tmp_path / "repo"; root.mkdir()
    (root / "new.zsh").write_text("function a() {\n  echo 1\n  echo 2\n  echo 3\n}\n")
    calls = []
    monkeypatch.setattr(ci, "extract_file", lambda root, rel, **k: calls.append(k) or [])
    monkeypatch.setattr(ci, "server_for", lambda rel, **k: ("fake-ls", ["fake"], "zsh", {}))
    res = crib._index_code_file_tracked(root, "new.zsh", proj, False)
    assert res.get("skipped") == "empty-extract" and res.get("server") == "fake-ls"
    assert any(k.get("settle") == 3.0 for k in calls)  # one slow retry first
    assert emptyextract.load(crib.paths.index_dir, proj) == {
        "new.zsh": {"server": "fake-ls", "kept_prior": 0}}
    found = [f for f in diagnose(crib, proj)["findings"]
             if f["check"] == "no symbols from the language server"]
    assert found and found[0]["server"] == "fake-ls" and found[0]["files"] == ["new.zsh"]

    sym = {"fqname": "new.a", "name": "a", "kind": "function", "lang": "zsh",
           "module": "new", "parent": "", "content_hash": "h", "file": "new.zsh",
           "line": 1, "signature": "function a()", "container": [], "calls": [],
           "called_by": [], "name_terms": ["a"]}
    monkeypatch.setattr(ci, "extract_file", lambda root, rel, **k: [dict(sym)])
    crib._index_code_file_tracked(root, "new.zsh", proj, False, describe_mode="none")
    assert emptyextract.load(crib.paths.index_dir, proj) == {}
