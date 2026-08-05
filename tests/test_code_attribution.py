"""Attribution: every description and every graph edge lands on the symbol it
actually belongs to — or on nothing at all.

Both halves used to key on a bare local name, which collides constantly (`A.run`
and `B.run` in one file, a module function and a method of the same name):

- describe results were keyed by the name the LLM echoed, so one description
  overwrote the other and the survivor was attached to both symbols;
- `patch_called_by` looked targets up by `(name, file)`, so a reverse edge could be
  written onto the wrong same-named symbol — and `references` was never patched at
  all, leaving dossier/graph stale under watcher operation.

The rule throughout: match by fqname, fall back only when the fallback is UNIQUE,
and drop the row otherwise. An undescribed symbol re-enters the backlog; a
misattributed one passes the content_hash gate forever.
"""

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


def _entry(fqname: str, *, file: str, name: str | None = None, **kw) -> dict:
    e = {"fqname": fqname, "name": name or fqname.split(".")[-1], "kind": "function",
         "lang": "python", "module": file.replace(".py", ""), "parent": "",
         "content_hash": f"h_{fqname}", "file": file, "line": 1, "mtime": 1,
         "signature": "def _():", "description": "d", "container": [],
         "calls": [], "called_by": [], "references": [],
         "name_terms": [fqname.split(".")[-1]]}
    e.update(kw)
    return e


# ── 2.6 describe results keyed by fqname ──────────────────────────────────────
def test_same_named_methods_get_their_own_descriptions():
    """`A.run` and `B.run` in one file: the rows carry the qualified name, so each
    symbol's fqname resolves to its own row by a unique qualified suffix. Before,
    both were keyed `run` and the second row simply overwrote the first."""
    metas = ci._rows_to_meta({"symbols": [
        {"name": "A.run", "description": "runs A", "keywords": ["a"]},
        {"name": "B.run", "description": "runs B", "keywords": ["b"]}]})
    assert ci.match_meta("pkg.mod.A.run", metas) == ("runs A", ["a"])
    assert ci.match_meta("pkg.mod.B.run", metas) == ("runs B", ["b"])


def test_ambiguous_tail_match_is_dropped_never_guessed():
    metas = {"A.run": {"description": "runs A"}, "B.run": {"description": "runs B"}}
    # no qualified suffix matches, and the bare tail `run` matches BOTH rows →
    # undescribed (the backlog re-describes) rather than 50/50 wrong
    assert ci.match_meta("pkg.mod.C.run", metas) == ("", [])
    assert ci.match_meta("pkg.mod.missing", metas) == ("", [])


def test_rust_fqnames_tail_match_on_double_colon():
    """The tail split used to be `.`-only, so a Rust fqname was one unsplittable
    token and NO Rust symbol ever matched a describe row."""
    qualified = ci._rows_to_meta([{"name": "Server::start", "description": "boots it"}])
    assert ci.match_meta("crate::net::Server::start", qualified)[0] == "boots it"
    bare = ci._rows_to_meta([{"name": "start", "description": "boots it"}])
    assert ci.match_meta("crate::net::start", bare)[0] == "boots it"


def test_exact_fqname_beats_any_fallback():
    metas = {"pkg.mod.A.run": {"description": "exact"},
             "A.run": {"description": "suffix"}}
    assert ci.match_meta("pkg.mod.A.run", metas)[0] == "exact"


def test_longest_qualified_suffix_wins():
    """A module-level `f` and a method `A.f` in one file both end in `.f`; the
    longer suffix is the more specific claim, so each lands on its own symbol
    instead of both collapsing into an ambiguity."""
    metas = {"f": {"description": "module fn"}, "A.f": {"description": "method"}}
    assert ci.match_meta("pkg.mod.f", metas)[0] == "module fn"
    assert ci.match_meta("pkg.mod.A.f", metas)[0] == "method"


def test_duplicate_describe_rows_do_not_clobber():
    """A model contradicting itself about one symbol resolves deterministically to
    the first row, in the order the file was read — not to whichever came last."""
    metas = ci._rows_to_meta({"symbols": [
        {"name": "f", "description": "first"},
        {"name": "f", "description": "second"}]})
    assert metas["f"]["description"] == "first"


def test_mop_up_blob_labels_each_block_with_the_fqname(monkeypatch):
    """The focused describe request carries the fqname per block and demands it
    back verbatim, so its results key on the index's own identities."""
    seen: dict[str, str] = {}

    def fake_generate(cfg, sysp, blob, schema, purpose=None, schema_name=None):
        seen["blob"], seen["sys"] = blob, sysp
        return {"symbols": [{"name": "m.A.run", "description": "runs A"},
                            {"name": "m.B.run", "description": "runs B"}]}

    monkeypatch.setattr("crib.generate.generate_structured", fake_generate)
    out = ci.describe_symbols(None, [
        {"fqname": "m.A.run", "name": "run", "kind": "method", "_body": "a"},
        {"fqname": "m.B.run", "name": "run", "kind": "method", "_body": "b"}])
    assert "# method m.A.run" in seen["blob"] and "# method m.B.run" in seen["blob"]
    assert out["m.A.run"]["description"] == "runs A"
    assert out["m.B.run"]["description"] == "runs B"


def test_deferred_describe_patches_each_symbol_by_fqname(crib, tmp_path, monkeypatch):
    """End of the deferred path: two same-named methods of one file come back from
    the queue and each gets ITS description — the bug wrote one to both."""
    store = SymbolIndex(crib.paths.project_dir("p"))
    store.write(_entry("m.A.run", file="m.py", name="run"))
    store.write(_entry("m.B.run", file="m.py", name="run"))
    monkeypatch.setattr(ci, "describe_symbols", lambda cfg, syms: {
        "m.A.run": {"description": "runs A", "keywords": ["a"]},
        "m.B.run": {"description": "runs B", "keywords": ["b"]}})

    pending = {fq: {"fqname": fq, "name": "run", "kind": "method",
                    "content_hash": f"h_{fq}", "_body": "..."}
               for fq in ("m.A.run", "m.B.run")}
    out = run(crib.indexer._describe_and_patch("p", tmp_path, "m.py", pending))
    assert out["described"] == 2
    assert store.read("m.A.run")["description"] == "runs A"
    assert store.read("m.B.run")["description"] == "runs B"


# ── 2.7 patch_edges: both relations, fqname-keyed targets ─────────────────────
def test_patch_edges_updates_references_symmetrically(crib):
    """A call site IS a mention, so a new `A→B` must show in B's `called_by` AND
    B's `references` — without B reindexing. Removing the call removes both."""
    store = SymbolIndex(crib.paths.project_dir("p"))
    store.write(_entry("a.target", file="a.py"))

    crib.code.patch_edges(
        store, [_entry("b.caller", file="b.py", calls=["target [a.py]"])], "b.py")
    tgt = store.read("a.target")
    assert tgt["called_by"] == ["caller [b.py]"]
    assert tgt["references"] == ["caller [b.py]"]     # the half that was never patched

    crib.code.patch_edges(store, [_entry("b.caller", file="b.py")], "b.py")
    tgt = store.read("a.target")
    assert tgt["called_by"] == [] and tgt["references"] == []


def test_patch_edges_skips_an_ambiguous_same_named_target(crib):
    """`run [a.py]` cannot name which of a.py's two `run`s is meant, so neither is
    patched. The old `(name, file)` dict silently kept one and edged the wrong
    symbol."""
    store = SymbolIndex(crib.paths.project_dir("p"))
    store.write(_entry("a.A.run", file="a.py", name="run"))
    store.write(_entry("a.B.run", file="a.py", name="run"))

    crib.code.patch_edges(
        store, [_entry("b.caller", file="b.py", calls=["run [a.py]"])], "b.py")
    assert store.read("a.A.run")["called_by"] == []
    assert store.read("a.B.run")["called_by"] == []


def test_patch_edges_resolves_a_qualified_edge_target_exactly(crib):
    """A server that hands back a qualified name resolves outright, ambiguity or
    not — the fqname is an exact identity."""
    store = SymbolIndex(crib.paths.project_dir("p"))
    store.write(_entry("a.A.run", file="a.py", name="run"))
    store.write(_entry("a.B.run", file="a.py", name="run"))

    crib.code.patch_edges(
        store, [_entry("b.caller", file="b.py", calls=["a.B.run [a.py]"])], "b.py")
    assert store.read("a.A.run")["called_by"] == []
    assert store.read("a.B.run")["called_by"] == ["caller [b.py]"]


def test_same_named_sources_in_one_file_patch_independently(crib):
    """Two same-named symbols in the REINDEXED file each carry their own outbound
    edges; the changed-entry map is keyed by fqname, so their targets are patched
    independently instead of one overwriting the other's work."""
    store = SymbolIndex(crib.paths.project_dir("p"))
    store.write(_entry("a.first", file="a.py"))
    store.write(_entry("c.second", file="c.py"))

    crib.code.patch_edges(store, [
        _entry("b.X.run", file="b.py", name="run", calls=["first [a.py]"]),
        _entry("b.Y.run", file="b.py", name="run", calls=["second [c.py]"]),
    ], "b.py")
    assert store.read("a.first")["called_by"] == ["run [b.py]"]
    assert store.read("c.second")["called_by"] == ["run [b.py]"]
