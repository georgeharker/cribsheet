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


def test_reaffirm_clears_stale_without_touching_body(crib):
    _seed_symbol(crib, "p", content_hash="aaaa1111")
    run(crib.code_append("pkg.Mod.foo", "still true", project="p"))
    _seed_symbol(crib, "p", content_hash="bbbb2222")          # body moved on
    assert crib.code_xref("pkg.Mod.foo", project="p")[0]["learning"]["stale"] is True

    out = run(crib.code_reaffirm("pkg.Mod.foo", project="p"))
    assert out["reaffirmed"]
    hit = crib.code_xref("pkg.Mod.foo", project="p")[0]
    assert hit["learning"]["stale"] is False                  # cleared
    assert hit["learning"]["body"].endswith("still true")     # body untouched


def test_code_graph_marks_nodes_with_learnings(crib):
    # foo → bar (callee); attach a learning to bar, expect the glyph flag on that node
    SymbolIndex(crib.paths.project_dir("p")).write({
        "fqname": "pkg.Mod.foo", "name": "foo", "kind": "function", "lang": "python",
        "module": "pkg.Mod", "parent": "", "content_hash": "h1", "file": "pkg/mod.py",
        "line": 1, "signature": "def foo():", "description": "", "container": [],
        "calls": ["bar [pkg/mod.py]"], "called_by": [], "name_terms": ["foo"]})
    _seed_symbol(crib, "p", fqname="pkg.Mod.bar")
    run(crib.code_append("pkg.Mod.bar", "gotcha", project="p"))
    tree = crib.code_graph("pkg.Mod.foo", project="p")
    assert not tree.get("has_learning")                       # foo has none
    assert tree["children"][0]["fqname"].endswith("bar")
    assert tree["children"][0]["has_learning"] is True        # bar is glyphed


def test_learnings_report_and_orphan_lifecycle(crib):
    _seed_symbol(crib, "p", fqname="a.foo", content_hash="h")
    run(crib.code_append("a.foo", "note", project="p"))
    assert crib.code_learnings(project="p")[0]["status"] == "ok"

    # the symbol is renamed away → the learning orphans; report flags it
    import shutil
    shutil.rmtree(crib.paths.project_dir("p") / "symbol_index")
    _seed_symbol(crib, "p", fqname="a.bar", content_hash="h")     # renamed foo→bar
    rows = crib.code_learnings(project="p", orphans_only=True)
    assert rows and rows[0]["symbol"] == "a.foo" and rows[0]["status"] == "orphan"

    # rehome suggestions surface the rename target; then confirm the move
    sugg = run(crib.code_rehome("a.foo", project="p"))
    assert any(c["fqname"] == "a.bar" for c in sugg["candidates"])
    moved = run(crib.code_rehome("a.foo", "a.bar", project="p"))
    assert moved["new"] == "a.bar"
    assert crib.code_read("a.bar", project="p")["found"] is True
    assert crib.code_learnings(project="p", orphans_only=True) == []   # resolved


def test_dossier_annotates_neighbours_with_their_descriptions(crib):
    si = SymbolIndex(crib.paths.project_dir("p"))
    si.write({"fqname": "m.foo", "name": "foo", "kind": "function", "lang": "python",
              "module": "m", "parent": "", "content_hash": "h", "file": "m.py", "line": 1,
              "signature": "def foo():", "description": "does foo", "container": [],
              "calls": ["bar [m.py]"], "called_by": [], "references": [], "name_terms": ["foo"]})
    si.write({"fqname": "m.bar", "name": "bar", "kind": "function", "lang": "python",
              "module": "m", "parent": "", "content_hash": "h", "file": "m.py", "line": 9,
              "signature": "def bar():", "description": "does the bar thing", "container": [],
              "calls": [], "called_by": ["foo [m.py]"], "references": [], "name_terms": ["bar"]})
    d = crib.code_dossier("m.foo", project="p")
    assert d["fqname"] == "m.foo" and d["description"] == "does foo"
    assert d["calls"][0]["symbol"] == "m.bar"
    assert d["calls"][0]["description"] == "does the bar thing"   # neighbour's OWN description


def test_code_tools_self_diagnose_unindexed_project(crib):
    # an unindexed project → a helpful error naming the fix, not a bare [] the agent
    # would misread as "this codebase isn't indexed"
    with pytest.raises(ValueError, match="no code index"):
        crib.code_xref("anything", project="ghost")
    with pytest.raises(ValueError, match="no code index"):
        crib.code_graph("anything", project="ghost")
    # once populated, the guard passes; an unknown symbol just returns empty
    _seed_symbol(crib, "ghost", fqname="pkg.foo")
    assert crib.code_xref("pkg.foo", project="ghost")            # found
    assert crib.code_xref("nonexistent", project="ghost") == []  # indexed → plain miss


def test_ensure_crib_creates_sensible_defaults_and_anchors_in_repo(crib, tmp_path):
    repo = tmp_path / "myrepo"
    (repo / "src").mkdir(parents=True)
    (repo / ".git").mkdir()                     # marker → anchors here, not the parent
    (repo / "src" / "a.py").write_text("x = 1\n")
    link, created = crib._ensure_crib(repo, None, want_code=True, want_docs=True)
    assert created and link.root == repo         # never escapes to tmp_path
    assert link.project == "myrepo"
    text = (repo / ".crib").read_text()
    assert 'project: myrepo' in text
    assert '"**/*.py"' in text                    # quoted glob (YAML-safe, not an alias)
    # idempotent: a second call finds the existing .crib, doesn't recreate
    link2, created2 = crib._ensure_crib(repo, None, want_code=True, want_docs=False)
    assert not created2 and link2.project == "myrepo"


def test_project_forget_clears_index_but_keeps_learnings(crib):
    _seed_symbol(crib, "p", fqname="m.foo")
    run(crib.code_append("m.foo", "keep me", project="p"))
    assert crib.project_status(project="p")["indexed"]
    out = crib.project_forget(project="p")        # default: keep learnings
    assert out["symbols_removed"] == 1 and out["learnings_removed"] == 0
    assert not crib.project_status(project="p")["indexed"]
    # the learning survived the index wipe (durable human source-of-truth)
    _seed_symbol(crib, "p", fqname="m.foo")       # re-index the symbol
    assert crib.code_read("m.foo", project="p")["found"]


def test_forget_removes_an_orphan(crib):
    _seed_symbol(crib, "p", fqname="a.foo")
    run(crib.code_append("a.foo", "note", project="p"))
    import shutil
    shutil.rmtree(crib.paths.project_dir("p") / "symbol_index")        # foo is gone
    out = run(crib.code_forget("a.foo", project="p"))                 # still removable
    assert out["symbol"] == "a.foo" and out["removed"] >= 1
