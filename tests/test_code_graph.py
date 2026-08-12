"""`code_graph` shapes: the pstree tree, the deduplicated subgraph, the module
rollup, and the whole-project export.

The tree and the subgraph answer different questions, and the seeded fixture is
built to make the difference bite: `deep` is reachable both directly from the root
and via `left → shared`, and `shared` is reached from two sides. A tree can only
show that by duplicating a node or cutting it off with `repeat`; the subgraph
states it as one node with two in-edges."""

from __future__ import annotations

import asyncio

import pytest

from crib import codequery
from crib.app import Crib
from crib.codeindex import SymbolIndex
from crib.config import Config
from crib.errors import CribUserError
from crib.paths import Paths
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def _sym(crib, project, fqname, file, **over):
    e = {"fqname": fqname, "name": fqname.split(".")[-1], "kind": "function",
         "lang": "python", "module": fqname.rsplit(".", 1)[0], "parent": "",
         "content_hash": f"h_{fqname}", "file": file, "line": 1,
         "signature": f"def {fqname.split('.')[-1]}():", "description": "",
         "container": [], "calls": [], "called_by": [], "references": [],
         "name_terms": [fqname.split(".")[-1]], **over}
    SymbolIndex(crib.paths.project_dir(project)).write(e)
    return e


def _diamond(crib, tmp_path):
    """app.main ⇒ {left, right, deep};  left ⇒ shared;  right ⇒ shared;
    shared ⇒ {deep, an unindexed vendor call};  orphan is called by nobody."""
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "app.py").write_text("def main(): pass\n")
    (root / "core.py").write_text("def shared(): pass\n")
    (root / "orphan.py").write_text("def orphan(): pass\n")
    _sym(crib, "p", "app.main", "app.py",
         calls=["left [app.py]", "right [app.py]", "deep [core.py]"])
    _sym(crib, "p", "app.left", "app.py", calls=["shared [core.py]"],
         called_by=["main [app.py]"])
    _sym(crib, "p", "app.right", "app.py", calls=["shared [core.py]"],
         called_by=["main [app.py]"])
    _sym(crib, "p", "core.shared", "core.py",
         calls=["deep [core.py]", "vendored [vendor/pkg.py]"],
         called_by=["left [app.py]", "right [app.py]"])
    _sym(crib, "p", "core.deep", "core.py",
         called_by=["shared [core.py]", "main [app.py]"])
    _sym(crib, "p", "orphan.orphan", "orphan.py")
    SymbolIndex(crib.paths.project_dir("p")).set_source_root(root)
    return root


def _ids(g):
    return {n["id"] for n in g["nodes"]}


def _edge_set(g):
    return {(e["from"], e["to"]) for e in g["edges"]}


# --- the tree loses convergence; the subgraph keeps it -------------------------

def test_tree_duplicates_or_truncates_the_shared_node(crib, tmp_path):
    _diamond(crib, tmp_path)
    tree = crib.code_graph("app.main", project="p")
    left, right, deep = tree["children"]
    # `shared` appears under BOTH left and right — a second time it is `repeat`
    # with its own children dropped, so the second reader cannot see what it calls
    assert left["children"][0]["fqname"] == "core.shared"
    assert right["children"][0] == {"fqname": "core.shared", "kind": "function",
                                    "file": "core.py", "line": 1, "children": [],
                                    "repeat": True}
    # and depth-first order means the DIRECT main→deep edge is the one truncated,
    # after `deep` was already shown three levels down under left→shared
    assert deep["repeat"] is True


def test_edges_shape_states_convergence_once(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph("app.main", project="p", shape="edges")
    assert g["shape"] == "edges" and g["scope"] == "symbol"
    assert g["root"] == "app.main"
    # every symbol exactly once, no repeats to reconstruct
    assert [n["id"] for n in g["nodes"]].count("core.shared") == 1
    # both paths into `shared` survive as edges — that IS the convergence
    assert ("app.left", "core.shared") in _edge_set(g)
    assert ("app.right", "core.shared") in _edge_set(g)
    assert ("core.shared", "core.deep") in _edge_set(g)
    assert ("app.main", "core.deep") in _edge_set(g)
    assert all(e["kind"] == "calls" for e in g["edges"])


def test_edges_are_deduplicated(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph("app.main", project="p", shape="edges")
    assert len(_edge_set(g)) == len(g["edges"])


def test_bfs_records_the_shortest_distance(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph("app.main", project="p", shape="edges")
    depth = {n["id"]: n["depth"] for n in g["nodes"]}
    assert depth["app.main"] == 0
    assert depth["app.left"] == 1
    # depth-first found `deep` at 3 (main→left→shared→deep) before the direct edge;
    # the subgraph reports the distance that is actually true
    assert depth["core.deep"] == 1
    assert depth["core.shared"] == 2


def test_unresolved_edge_target_is_an_external_node(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph("app.main", project="p", shape="edges")
    ext = [n for n in g["nodes"] if n.get("external")]
    assert [n["id"] for n in ext] == ["vendored [vendor/pkg.py]"]
    assert ("core.shared", "vendored [vendor/pkg.py]") in _edge_set(g)


def test_depth_bound_flags_the_frontier(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph("app.main", project="p", shape="edges", depth=1)
    assert _ids(g) == {"app.main", "app.left", "app.right", "core.deep"}
    trunc = {n["id"] for n in g["nodes"] if n.get("truncated")}
    assert trunc == {"app.left", "app.right"}      # reached, deliberately unwalked
    assert not any(n.get("truncated") for n in g["nodes"] if n["id"] == "core.deep")


def test_callers_direction_keeps_caller_to_callee_orientation(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph("core.shared", project="p", direction="callers",
                        shape="edges")
    # walked backwards, but the arrows still point the way the calls go, so the
    # output is composable with a callees graph and reads the same in a diagram
    assert ("app.left", "core.shared") in _edge_set(g)
    assert ("app.main", "app.left") in _edge_set(g)
    assert ("core.shared", "app.left") not in _edge_set(g)


# --- module rollup ------------------------------------------------------------

def test_module_rollup_weights_the_file_edges(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph("app.main", project="p", group_by="module")
    assert g["group_by"] == "module" and g["shape"] == "edges"
    w = {(e["from"], e["to"]): e["weight"] for e in g["edges"]}
    assert w[("app.py", "core.py")] == 3          # left→shared, right→shared, main→deep
    assert w[("app.py", "app.py")] == 2           # main→left, main→right, kept
    assert w[("core.py", "core.py")] == 1         # shared→deep
    counts = {n["id"]: n["symbols"] for n in g["nodes"]}
    assert counts["app.py"] == 3 and counts["core.py"] == 2


def test_module_rollup_implies_edges_and_refuses_a_tree(crib, tmp_path):
    _diamond(crib, tmp_path)
    with pytest.raises(ValueError, match="group_by"):
        crib.code_graph("app.main", project="p", shape="tree", group_by="module")


# --- whole-project export -----------------------------------------------------

def test_whole_project_includes_what_no_walk_reaches(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph(project="p")
    assert g["scope"] == "project" and g["shape"] == "edges"
    assert "root" not in g
    # `orphan` is called by nobody, so no rooted walk can ever produce it
    assert "orphan.orphan" in _ids(g)
    assert _ids(g) >= {"app.main", "app.left", "app.right", "core.shared",
                       "core.deep", "orphan.orphan"}
    assert ("app.left", "core.shared") in _edge_set(g)
    assert all("depth" not in n for n in g["nodes"])   # no root ⇒ no distance


def test_whole_project_rolls_up_and_refuses_a_tree(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph(project="p", group_by="module")
    assert {n["id"] for n in g["nodes"]} >= {"app.py", "core.py", "orphan.py"}
    with pytest.raises(ValueError, match="whole-project"):
        crib.code_graph(project="p", shape="tree")


def test_whole_project_symbol_export_is_capped_but_the_rollup_is_not(
        crib, tmp_path, monkeypatch):
    _diamond(crib, tmp_path)
    monkeypatch.setattr(codequery, "MAX_GRAPH_NODES", 2)
    with pytest.raises(ValueError, match="group_by"):
        crib.code_graph(project="p")
    assert crib.code_graph(project="p", group_by="module")["nodes"]


def test_unknown_shape_says_what_is_accepted(crib, tmp_path):
    _diamond(crib, tmp_path)
    with pytest.raises(ValueError, match="edges"):
        crib.code_graph("app.main", project="p", shape="dag")


# --- symbol resolution: never guess, always disclose ---------------------------

def _wrapper_and_op(crib, tmp_path):
    """The shape that produced the original bug: an MCP-style wrapper and the op it
    wraps share a bare name. The wrapper has NO callers, the op has two."""
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "server.py").write_text("def add_node(): pass\n")
    (root / "ops.py").write_text("def add_node(): pass\n")
    _sym(crib, "w", "server.add_node", "server.py", calls=["add_node [ops.py]"])
    _sym(crib, "w", "ops.add_node", "ops.py",
         called_by=["caller_a [ops.py]", "caller_b [ops.py]"])
    _sym(crib, "w", "ops.caller_a", "ops.py", calls=["add_node [ops.py]"])
    _sym(crib, "w", "ops.caller_b", "ops.py", calls=["add_node [ops.py]"])
    SymbolIndex(crib.paths.project_dir("w")).set_source_root(root)
    return root


def test_an_ambiguous_bare_name_refuses_instead_of_picking(crib, tmp_path):
    _wrapper_and_op(crib, tmp_path)
    # the failure this replaces: silently answering "0 callers" for the wrapper
    with pytest.raises(ValueError) as e:
        crib.code_graph("add_node", project="w", direction="callers", shape="edges")
    msg = str(e.value)
    assert "2 symbols match" in msg and "NO result" in msg
    assert "ops.add_node (2 callers)" in msg          # ranked, and the count is stated
    assert msg.index("ops.add_node") < msg.index("server.add_node")


def test_qualifying_the_name_resolves_and_answers(crib, tmp_path):
    _wrapper_and_op(crib, tmp_path)
    g = crib.code_graph("ops.add_node", project="w", direction="callers",
                        shape="edges")
    assert {n["id"] for n in g["nodes"]} == {"ops.add_node", "ops.caller_a",
                                             "ops.caller_b"}
    assert g["resolved"] == {"query": "ops.add_node", "fqname": "ops.add_node",
                             "via": "fqname"}


def test_every_result_says_what_the_name_became(crib, tmp_path):
    _diamond(crib, tmp_path)
    tree = crib.code_graph("main", project="p")                  # bare, unique
    assert tree["resolved"] == {"query": "main", "fqname": "app.main", "via": "name"}
    edges = crib.code_graph("app.main", project="p", shape="edges")
    assert edges["resolved"]["via"] == "fqname"
    rolled = crib.code_graph("main", project="p", group_by="module")
    assert rolled["resolved"]["fqname"] == "app.main"   # the rollup carries it through


# --- languages whose qualified names are not dotted ----------------------------

def _rust_ish(crib, tmp_path):
    """crib renders a Rust qualified name with `::` (`_qualify`), so a dot-only
    match rule cannot see it — the symbol is indexed and reads as unknown."""
    root = tmp_path / "rs"
    (root / "src").mkdir(parents=True)
    (root / "src" / "lockfile.rs").write_text("pub struct ClientsLock;\n")
    _sym(crib, "r", "rust::src::core::lockfile::ClientsLock", "src/lockfile.rs",
         name="ClientsLock", lang="rust", kind="struct",
         called_by=["acquire [src/lockfile.rs]"])
    _sym(crib, "r", "rust::src::core::lockfile::acquire", "src/lockfile.rs",
         name="acquire", lang="rust",
         calls=["ClientsLock [src/lockfile.rs]"])
    SymbolIndex(crib.paths.project_dir("r")).set_source_root(root)
    return root


def test_a_bare_name_resolves_under_a_non_dot_separator(crib, tmp_path):
    _rust_ish(crib, tmp_path)
    g = crib.code_graph("ClientsLock", project="r", direction="callers",
                        shape="edges")
    assert g["resolved"] == {"query": "ClientsLock",
                             "fqname": "rust::src::core::lockfile::ClientsLock",
                             "via": "name"}
    assert ("rust::src::core::lockfile::acquire",
            "rust::src::core::lockfile::ClientsLock") in _edge_set(g)


@pytest.mark.parametrize("query", ["lockfile::ClientsLock", "lockfile.ClientsLock",
                                   "rust::src::core::lockfile::ClientsLock"])
def test_a_partial_path_matches_in_either_separator(crib, tmp_path, query):
    _rust_ish(crib, tmp_path)
    # the caller may not know which separator crib stored, and should not have to
    g = crib.code_graph(query, project="r", shape="edges")
    assert g["resolved"]["fqname"] == "rust::src::core::lockfile::ClientsLock"


def test_an_unknown_symbol_is_still_an_empty_result_not_an_error(crib, tmp_path):
    _diamond(crib, tmp_path)
    assert crib.code_graph("no_such_symbol", project="p") == {}


# --- the match rule itself: segments, in the entry's own language --------------

def test_the_reader_and_the_writer_share_one_separator():
    from crib.codeindex import _qualify, fqname_sep, fqname_segments
    for lang in ("rust", "python", "zsh", "lua", "c"):
        fq = _qualify(lang, "mod", ("Outer",), "leaf")
        # whatever the writer joined with, the reader cuts back into the same parts
        assert fqname_segments(fq, lang) == ["mod", "Outer", "leaf"]
        assert fqname_sep(lang) in fq


@pytest.mark.parametrize("fq, name, lang, query, tier", [
    # exact, in either spelling the caller might reach for
    ("a::b::c", "c", "rust", "a::b::c", "fqname"),
    ("a::b::c", "c", "rust", "a.b.c", "fqname"),
    ("a.b.c", "c", "python", "a::b::c", "fqname"),
    # trailing run of SEGMENTS
    ("a::b::c", "c", "rust", "b::c", "suffix"),
    ("a::b::c", "c", "rust", "b.c", "suffix"),
    # the bare local name, whatever qualified it
    ("a::b::c", "c", "rust", "c", "name"),
    ("a.b.c", "c", "python", "c", "name"),
    # a partial that is not a segment boundary matches nothing
    ("a.bc.d", "d", "python", "c.d", None),
    ("a::bcd", "bcd", "rust", "cd", None),
    # a name CONTAINING the separator keeps its boundary: `push` is not a symbol
    # here, and re-deriving the tail from the fqname would have said it was
    ("helpers.git.push", "git.push", "zsh", "push", None),
    ("helpers.git.push", "git.push", "zsh", "git.push", "name"),
])
def test_match_tiers(fq, name, lang, query, tier):
    from crib.codeindex import fqname_match
    assert fqname_match(fq, name, query, lang) == tier


def test_every_verb_that_narrows_to_one_symbol_discloses_it(crib, tmp_path):
    _diamond(crib, tmp_path)
    graph = crib.code_graph("main", project="p")
    dossier = crib.code_dossier("main", project="p")
    learning = crib.learning_read("main", project="p")
    expected = {"query": "main", "fqname": "app.main", "via": "name"}
    assert graph["resolved"] == dossier["resolved"] == learning["resolved"] == expected


def test_a_verb_that_lists_every_match_discloses_nothing(crib, tmp_path):
    _wrapper_and_op(crib, tmp_path)
    # code_xref NARROWS NOTHING — it returns both symbols, so the list already is
    # the disclosure. Wrapping it in an envelope to carry `resolved` would be
    # tidying away the rule, not an improvement.
    hits = crib.code_xref("add_node", project="w")
    assert {h["fqname"] for h in hits} == {"server.add_node", "ops.add_node"}
    assert not any("resolved" in h for h in hits)


# --- scope: the language's own qualified context ------------------------------

@pytest.mark.parametrize("lang, file, container, scope", [
    # the path IS the namespace: python import path, lua require path
    ("python", "crib/chunk.py", ["Chunk"], ["crib", "chunk", "Chunk"]),
    ("python", "crib/__init__.py", [], ["crib"]),
    ("python", "src/pkg/mod.py", ["A", "B"], ["pkg", "mod", "A", "B"]),
    ("lua", "lua/sharedserver/health.lua", [], ["sharedserver", "health"]),
    # rust is CRATE-relative — the crate name lives in Cargo.toml and adds nothing
    # `path` does not already disambiguate
    ("rust", "rust/src/core/state.rs", ["impl ServerState"],
     ["core", "state", "ServerState"]),
    ("rust", "crates/foo/src/a/mod.rs", ["impl T"], ["a", "T"]),
    # declared in the source; the path contributes nothing
    ("cpp", "src/engine/render.cpp", ["gfx", "Renderer"], ["gfx", "Renderer"]),
    ("ruby", "app/models/user.rb", ["Admin", "User"], ["Admin", "User"]),
    # go qualifies as `store.Store` — the package is the directory, not the file
    ("go", "pkg/store/index.go", ["Store"], ["store", "Store"]),
    # no namespace exists, and a directory is not a substitute for one
    ("c", "bin/sharedserver-watcher.c", [], []),
    # zsh nests in LOCATION only: the function is global once declared
    ("zsh", "core/plugin-bundles/omz.zsh", ["_zdot_load_omz_lib"], []),
    # an LSP artifact is not a scope
    ("lua", "scripts/codeindex/dump_lsp.lua", ["for in"],
     ["scripts", "codeindex", "dump_lsp"]),
])
def test_scope_per_language(lang, file, container, scope):
    from crib.codeindex import scope_of
    assert scope_of(lang, file, container) == scope


def test_scope_needs_no_manifest():
    """Every language derives from source and layout alone; only Rust's crate name
    is out of band, and crate-relative scope does without it."""
    from crib.codeindex import scope_of
    assert scope_of("rust", "rust/src/core/state.rs", []) == ["core", "state"]


def test_an_unknown_language_still_matches_on_either_separator():
    from crib.codeindex import fqname_match
    assert fqname_match("a::b::c", "c", "b::c", "") == "suffix"
    assert fqname_match("a::b::c", "c", "c", "") == "name"


# --- expected refusals are delivered, not dumped -------------------------------

def test_symbol_errors_are_user_errors_and_still_value_errors(crib, tmp_path):
    _wrapper_and_op(crib, tmp_path)
    from crib.refs import AmbiguousSymbol, UnknownSymbol
    assert issubclass(AmbiguousSymbol, CribUserError)
    assert issubclass(UnknownSymbol, CribUserError)
    assert issubclass(CribUserError, ValueError)     # every old handler still catches
    with pytest.raises(CribUserError):
        crib.code_graph("add_node", project="w")


def test_the_mcp_face_delivers_the_message_verbatim(crib, tmp_path):
    """The candidate list IS the answer, so it has to survive the tool boundary —
    FastMCP renders a ToolError's text and may mask anything else."""
    from fastmcp.exceptions import ToolError

    from crib.server import build_server
    _wrapper_and_op(crib, tmp_path)
    mcp = build_server(crib)
    tool = {t.name: t for t in asyncio.run(mcp.list_tools())}["code_graph"]
    with pytest.raises(ToolError) as e:
        asyncio.run(tool.run({"symbol": "add_node", "project": "w",
                              "direction": "callers"}))
    assert "2 symbols match" in str(e.value)
    assert "ops.add_node (2 callers)" in str(e.value)
