"""Code symbol index — deterministic structural checks (no LLM).

The naming/qualification/store logic is language-specific and pure, so it gets fast
unit tests; the LSP extraction + call graph gets one integration test that skips when
pyright isn't available (like the retrieval gate). Descriptions (the LLM facet) are
NOT tested here — this gate is the structural, deterministic contract.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from crib import codeindex as ci

REPO = Path(__file__).resolve().parent.parent


# --- fqn normalization (per-language qualify / module / local name) ----------
def test_module_of_python_strips_roots_and_init():
    assert ci._module_of("crib/retrieve.py", "python") == "crib.retrieve"
    assert ci._module_of("src/pkg/mod.py", "python") == "pkg.mod"
    assert ci._module_of("lua/mcp_companion/init.lua", "lua") == "mcp_companion"


def test_module_of_rust_uses_colons():
    assert ci._module_of("src/foo/bar.rs", "rust") == "foo::bar"


def test_local_name_strips_lua_table_prefix():
    assert ci._local_name("M.setup", "lua") == "setup"
    assert ci._local_name("T:method", "lua") == "method"
    assert ci._local_name("plain", "python") == "plain"


def test_qualify_is_language_idiomatic():
    assert ci._qualify("python", "crib.retrieve", ("BM25",), "scores") \
        == "crib.retrieve.BM25.scores"
    assert ci._qualify("rust", "foo::bar", ("Type",), "method") \
        == "foo::bar::Type::method"
    # Lua: the `M` table var is dropped from the container, module comes from path
    assert ci._qualify("lua", "mcp_companion", ("M",), "setup") \
        == "mcp_companion.setup"


def test_name_terms_split_compound_identifiers():
    terms = ci._name_terms("SharedServerManager", "mod.SharedServerManager")
    assert {"shared", "server", "manager"} <= set(terms)          # subtokens
    assert "SharedServerManager" in terms                          # unqualified name


# --- learning_slug: fqn → filesystem/git-safe basename ----------------------
def test_learning_slug_clean_fqn_verbatim():
    # a pure dotted fqn is already filesystem-clean → passes through unchanged
    assert ci.learning_slug("crib.retrieve.LexicalCache.get") == \
        "crib.retrieve.LexicalCache.get"


def test_learning_slug_munges_unsafe_and_disambiguates():
    # anything outside [A-Za-z0-9._-] collapses to '-'; a lossy munge appends a
    # short fqn hash so distinct symbols can't collide on disk
    a = ci.learning_slug("core::cache::Store::get")
    b = ci.learning_slug("core-cache-Store-get")        # would collide sans hash
    assert a.startswith("core-cache-Store-get-")
    assert a != b and "::" not in a and "/" not in a
    # Go import paths, C++ generics/operators/dtors — all land valid and unique
    for fq in ("pkg/foo.Bar", "Vec<T>::push", "operator+", "ns::~Dtor"):
        s = ci.learning_slug(fq)
        assert set(s) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
        assert s and not s.startswith("-") and not s.endswith("-")


# --- documentSymbol flattening (descend classes into methods) ----------------
def test_walk_descends_containers():
    tree = [{"name": "C", "kind": 5, "children": [
        {"name": "m", "kind": 6, "children": []}]},
        {"name": "f", "kind": 12}]
    flat = ci._walk(tree)
    names = {(s["name"], parents) for s, parents in flat}
    assert ("C", ()) in names
    assert ("m", ("C",)) in names          # method carries its class as container
    assert ("f", ()) in names


# --- TOML store render/parse round-trip (incl. the empty-array case) ---------
def test_render_parse_roundtrip_including_empty_arrays():
    e = {"fqname": "a.B.c", "name": "c", "kind": "method", "lang": "python",
         "module": "a", "parent": "a.B", "content_hash": "deadbeef",
         "file": "a.py", "line": 12, "signature": "def c(self):",
         "description": 'has "quotes" and, commas', "container": ["B"],
         "calls": ["x [a.py]"], "called_by": [], "name_terms": ["c", "a.B.c"]}
    got = ci._parse(ci._render(e))
    assert got["fqname"] == "a.B.c" and got["line"] == 12
    assert got["called_by"] == []          # empty array parses as [], not "[]"
    assert got["calls"] == ["x [a.py]"]
    assert got["parent"] == "a.B"
    assert 'quotes' in got["description"]


# --- integration: real LSP extraction + call graph (skips without pyright) ---
def test_extract_call_graph_anchor_edges():
    if ci.server_for("crib/retrieve.py") is None:
        pytest.skip("no Python LSP server available (pyright/basedpyright)")
    entries = {e["fqname"]: e for e in ci.extract_file(REPO, "crib/retrieve.py")}
    # fqn normalization: module-qualified, class-nested
    assert "crib.retrieve.LexicalCache.get" in entries
    get = entries["crib.retrieve.LexicalCache.get"]
    assert get["name"] == "get" and get["parent"] == "crib.retrieve.LexicalCache"
    # deterministic call edges (semantic, cross-file) — the LSP-sync contract
    callees = {c.split(" [")[0] for c in get["calls"]}
    assert {"BM25", "_lexical_tf", "get_docs"} <= callees
    callers = {c.split(" [")[0] for c in get["called_by"]}
    assert "_retrieve" in callers
