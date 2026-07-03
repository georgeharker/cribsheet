"""Durable learnings attached to a code symbol (docs/code-symbol-index.md §8):
the add/edit/forget primitives, and the query-time join that resurfaces a
learning (with a staleness flag) under code_lookup / code_xref."""

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


def _seed_symbol(crib, project, fqname="pkg.Mod.foo", content_hash="aaaa1111"):
    """Persist one synthetic symbol_index entry so the learning verbs can resolve it."""
    SymbolIndex(crib.paths.project_dir(project)).write({
        "fqname": fqname, "name": fqname.split(".")[-1], "kind": "function",
        "lang": "python", "module": "pkg.Mod", "parent": "",
        "content_hash": content_hash, "file": "pkg/mod.py", "line": 10,
        "signature": "def foo():", "description": "does foo",
        "container": [], "calls": [], "called_by": [], "name_terms": ["foo"]})


def test_append_creates_then_appends_dated_entries(crib):
    _seed_symbol(crib, "p")
    a = run(crib.code_append("pkg.Mod.foo", "first insight", project="p"))
    assert a["created"] and a["relpath"] == "code-learnings/pkg.Mod.foo.md"
    b = run(crib.code_append("pkg.Mod.foo", "second insight", project="p"))
    assert not b["created"]                                   # same running note
    body = crib.code_read("pkg.Mod.foo", project="p")["body"]
    assert "first insight" in body and "second insight" in body


def test_unknown_and_ambiguous_symbols_raise(crib):
    _seed_symbol(crib, "p", fqname="a.foo")
    _seed_symbol(crib, "p", fqname="b.foo")
    with pytest.raises(ValueError, match="unknown symbol"):
        run(crib.code_append("nope.nope", "x", project="p"))
    with pytest.raises(ValueError, match="ambiguous"):          # bare name, two hits
        run(crib.code_append("foo", "x", project="p"))
    run(crib.code_append("a.foo", "exact wins", project="p"))   # exact fqn is fine


def test_forget_removes_learning(crib):
    _seed_symbol(crib, "p")
    run(crib.code_append("pkg.Mod.foo", "x", project="p"))
    run(crib.code_forget("pkg.Mod.foo", project="p"))
    assert crib.code_read("pkg.Mod.foo", project="p")["found"] is False


def test_join_surfaces_learning_and_flags_staleness(crib):
    _seed_symbol(crib, "p", content_hash="aaaa1111")
    run(crib.code_append("pkg.Mod.foo", "the subtle bit", project="p"))

    # fresh: learning attaches to the xref hit, not stale
    hit = crib.code_xref("pkg.Mod.foo", project="p")[0]
    assert hit["learning"]["body"].endswith("the subtle bit")
    assert hit["learning"]["stale"] is False

    # body changes (new content_hash) but the note keeps its authoring snapshot → stale
    _seed_symbol(crib, "p", content_hash="bbbb2222")
    hit = crib.code_xref("pkg.Mod.foo", project="p")[0]
    assert hit["learning"]["stale"] is True

    # a symbol with no learning carries no `learning` key
    _seed_symbol(crib, "p", fqname="pkg.Mod.bar")
    bar = [h for h in crib.code_xref("pkg.Mod.bar", project="p")][0]
    assert "learning" not in bar
