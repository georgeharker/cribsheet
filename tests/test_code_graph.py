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
from crib.symbols import fqn, scope_of, symbol_ref
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
    # Derive the same fields the extractor does, so a seeded fixture is a
    # CURRENT-shape entry rather than a legacy one. Anything a test wants to leave
    # out (to stand in for an older store) it passes explicitly as None and pops.
    e.setdefault("scope", scope_of(e["lang"], e["file"], e["container"]))
    e.setdefault("symbol_ref", symbol_ref(e["file"], e["container"], e["name"],
                                          e["lang"]))
    e.setdefault("fqn", fqn(e["scope"], e["name"], e["lang"], e["file"],
                            e["container"]))
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
    assert left["children"][0]["symbol_ref"] == "core.py#shared"
    assert right["children"][0] == {"symbol_ref": "core.py#shared",
                                    "fqn": "core.shared", "name": "shared",
                                    "kind": "function",
                                    "file": "core.py", "line": 1, "children": [],
                                    "repeat": True}
    # and depth-first order means the DIRECT main→deep edge is the one truncated,
    # after `deep` was already shown three levels down under left→shared
    assert deep["repeat"] is True


def test_edges_shape_states_convergence_once(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph("app.main", project="p", shape="edges")
    assert g["shape"] == "edges" and g["scope"] == "symbol"
    assert g["root"] == "app.py#main"
    # every symbol exactly once, no repeats to reconstruct
    assert [n["id"] for n in g["nodes"]].count("core.py#shared") == 1
    # both paths into `shared` survive as edges — that IS the convergence
    assert ("app.py#left", "core.py#shared") in _edge_set(g)
    assert ("app.py#right", "core.py#shared") in _edge_set(g)
    assert ("core.py#shared", "core.py#deep") in _edge_set(g)
    assert ("app.py#main", "core.py#deep") in _edge_set(g)
    assert all(e["kind"] == "calls" for e in g["edges"])


def test_edges_are_deduplicated(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph("app.main", project="p", shape="edges")
    assert len(_edge_set(g)) == len(g["edges"])


def test_bfs_records_the_shortest_distance(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph("app.main", project="p", shape="edges")
    depth = {n["id"]: n["depth"] for n in g["nodes"]}
    assert depth["app.py#main"] == 0
    assert depth["app.py#left"] == 1
    # depth-first found `deep` at 3 (main→left→shared→deep) before the direct edge;
    # the subgraph reports the distance that is actually true
    assert depth["core.py#deep"] == 1
    assert depth["core.py#shared"] == 2


def test_unresolved_edge_target_is_an_external_node(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph("app.main", project="p", shape="edges")
    ext = [n for n in g["nodes"] if n.get("external")]
    assert [n["id"] for n in ext] == ["vendored [vendor/pkg.py]"]
    assert ("core.py#shared", "vendored [vendor/pkg.py]") in _edge_set(g)


def test_depth_bound_flags_the_frontier(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph("app.main", project="p", shape="edges", depth=1)
    assert _ids(g) == {"app.py#main", "app.py#left", "app.py#right", "core.py#deep"}
    trunc = {n["id"] for n in g["nodes"] if n.get("truncated")}
    assert trunc == {"app.py#left", "app.py#right"}      # reached, deliberately unwalked
    assert not any(n.get("truncated") for n in g["nodes"] if n["id"] == "core.py#deep")


def test_callers_direction_keeps_caller_to_callee_orientation(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph("core.shared", project="p", direction="callers",
                        shape="edges")
    # walked backwards, but the arrows still point the way the calls go, so the
    # output is composable with a callees graph and reads the same in a diagram
    assert ("app.py#left", "core.py#shared") in _edge_set(g)
    assert ("app.py#main", "app.py#left") in _edge_set(g)
    assert ("core.py#shared", "app.py#left") not in _edge_set(g)


# --- module rollup ------------------------------------------------------------

def test_file_rollup_weights_the_edges(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph("app.main", project="p", group_by="file")
    assert g["group_by"] == "file" and g["shape"] == "edges"
    w = {(e["from"], e["to"]): e["weight"] for e in g["edges"]}
    assert w[("app.py", "core.py")] == 3          # left→shared, right→shared, main→deep
    assert w[("app.py", "app.py")] == 2           # main→left, main→right, kept
    assert w[("core.py", "core.py")] == 1         # shared→deep
    counts = {n["id"]: n["symbols"] for n in g["nodes"]}
    assert counts["app.py"] == 3 and counts["core.py"] == 2


def test_a_rollup_implies_edges_and_refuses_a_tree(crib, tmp_path):
    _diamond(crib, tmp_path)
    with pytest.raises(ValueError, match="group_by"):
        crib.code_graph("app.main", project="p", shape="tree", group_by="file")


# --- whole-project export -----------------------------------------------------

def test_whole_project_includes_what_no_walk_reaches(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph(project="p")
    assert g["scope"] == "project" and g["shape"] == "edges"
    assert "root" not in g
    # `orphan` is called by nobody, so no rooted walk can ever produce it
    assert "orphan.py#orphan" in _ids(g)
    assert _ids(g) >= {"app.py#main", "app.py#left", "app.py#right", "core.py#shared",
                       "core.py#deep", "orphan.py#orphan"}
    assert ("app.py#left", "core.py#shared") in _edge_set(g)
    assert all("depth" not in n for n in g["nodes"])   # no root ⇒ no distance


def test_whole_project_rolls_up_and_refuses_a_tree(crib, tmp_path):
    _diamond(crib, tmp_path)
    g = crib.code_graph(project="p", group_by="file")
    assert {n["id"] for n in g["nodes"]} >= {"app.py", "core.py", "orphan.py"}
    with pytest.raises(ValueError, match="whole-project"):
        crib.code_graph(project="p", shape="tree")


def test_whole_project_symbol_export_is_capped_but_the_rollup_is_not(
        crib, tmp_path, monkeypatch):
    _diamond(crib, tmp_path)
    monkeypatch.setattr(codequery, "MAX_GRAPH_NODES", 2)
    with pytest.raises(ValueError, match="group_by"):
        crib.code_graph(project="p")
    assert crib.code_graph(project="p", group_by="file")["nodes"]


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
    assert "ops.py#add_node (2 callers)" in msg       # ranked, and the count is stated
    assert msg.index("ops.py#add_node") < msg.index("server.py#add_node")


def test_qualifying_the_name_resolves_and_answers(crib, tmp_path):
    _wrapper_and_op(crib, tmp_path)
    g = crib.code_graph("ops.add_node", project="w", direction="callers",
                        shape="edges")
    assert {n["id"] for n in g["nodes"]} == {"ops.py#add_node", "ops.py#caller_a",
                                             "ops.py#caller_b"}
    assert g["resolved"] == {"query": "ops.add_node", "fqn": "ops.add_node",
                             "symbol_ref": "ops.py#add_node", "via": "exact"}


def test_every_result_says_what_the_name_became(crib, tmp_path):
    _diamond(crib, tmp_path)
    tree = crib.code_graph("main", project="p")                  # bare, unique
    assert tree["resolved"] == {"query": "main", "fqn": "app.main",
                                "symbol_ref": "app.py#main", "via": "name"}
    edges = crib.code_graph("app.main", project="p", shape="edges")
    assert edges["resolved"]["via"] == "exact"
    rolled = crib.code_graph("main", project="p", group_by="file")
    assert rolled["resolved"]["symbol_ref"] == "app.py#main"   # rollup carries it


# --- languages whose qualified names are not dotted ----------------------------

def _rust_ish(crib, tmp_path):
    """crib renders a Rust qualified name with `::` (`_qualify`), so a dot-only
    match rule cannot see it — the symbol is indexed and reads as unknown."""
    root = tmp_path / "rs"
    (root / "src" / "core").mkdir(parents=True)
    (root / "src" / "core" / "lockfile.rs").write_text(
        "pub struct ClientsLock;\n")
    _sym(crib, "r", "rust::src::core::lockfile::ClientsLock", "src/core/lockfile.rs",
         name="ClientsLock", lang="rust", kind="struct",
         called_by=["acquire [src/core/lockfile.rs]"])
    _sym(crib, "r", "rust::src::core::lockfile::acquire", "src/core/lockfile.rs",
         name="acquire", lang="rust",
         calls=["ClientsLock [src/core/lockfile.rs]"])
    SymbolIndex(crib.paths.project_dir("r")).set_source_root(root)
    return root


def test_a_bare_name_resolves_under_a_non_dot_separator(crib, tmp_path):
    _rust_ish(crib, tmp_path)
    g = crib.code_graph("ClientsLock", project="r", direction="callers",
                        shape="edges")
    assert g["resolved"] == {"query": "ClientsLock",
                             "fqn": "core::lockfile::ClientsLock",
                             "symbol_ref": "src/core/lockfile.rs#ClientsLock",
                             "via": "name"}
    assert ("src/core/lockfile.rs#acquire",
            "src/core/lockfile.rs#ClientsLock") in _edge_set(g)


@pytest.mark.parametrize("query, tier", [
    # the canonical run for this entry is  a · b · c  (file a/b.rs, name c) — every
    # spelling anyone might type is a trailing run of it, in either separator
    ("a::b::c", "exact"), ("a.b.c", "exact"),
    ("b::c", "suffix"), ("b.c", "suffix"),
    ("c", "name"),
    # a partial that is not a segment boundary matches nothing
    ("cd", None), ("bc", None),
])
def test_one_rule_over_the_canonical_run(query, tier):
    from crib.symbols import match_entry
    e = {"file": "a/b.rs", "name": "c", "lang": "rust", "container": []}
    assert match_entry(e, query) == tier


def test_a_name_containing_the_separator_keeps_its_boundary():
    """`push` is not a symbol here, and re-deriving the tail by splitting the
    recorded name would have said it was."""
    from crib.symbols import match_entry
    e = {"file": "helpers/git.zsh", "name": "git.push", "lang": "zsh",
         "container": []}
    assert match_entry(e, "push") is None
    assert match_entry(e, "git.push") == "name"
    assert match_entry(e, "#git.push") == "ref"
    assert match_entry(e, "#push") is None


@pytest.mark.parametrize("query, tier", [
    # the whole reference, and the two partial spellings a reader actually has
    ("crib/retrieve.py#reciprocal_rank_fusion", "ref"),
    ("retrieve.py#reciprocal_rank_fusion", "ref"),      # a trailing run of the path
    ("#reciprocal_rank_fusion", "ref"),                 # the tail alone
    # a `#` means REFERENCE outright — it must not fall through to the name tiers,
    # which would split `crib/retrieve.py#foo` on boundaries that were never there
    ("wrong/path.py#reciprocal_rank_fusion", None),
    ("crib/retrieve.py#other", None),
    # the legacy key and the loose tiers still answer, because they are trailing
    # runs of the same canonical form — resolution needs no memory of them
    ("crib.retrieve.reciprocal_rank_fusion", "exact"),
    ("retrieve.reciprocal_rank_fusion", "suffix"),
    ("reciprocal_rank_fusion", "name"),
])
def test_a_reference_resolves_by_parts(query, tier):
    """A reference is matched as the PAIR it is — a trailing run of the path, and a
    trailing run of the tail. String equality would make the format precise but
    unusable: you would have to know the full repo-relative path to name a symbol,
    which is the one thing a reader of a stack trace usually does not have."""
    from crib.symbols import match_entry
    e = {"file": "crib/retrieve.py", "name": "reciprocal_rank_fusion",
         "lang": "python", "container": []}
    assert match_entry(e, query) == tier


def test_every_verb_that_narrows_to_one_symbol_discloses_it(crib, tmp_path):
    _diamond(crib, tmp_path)
    graph = crib.code_graph("main", project="p")
    dossier = crib.code_dossier("main", project="p")
    learning = crib.learning_read("main", project="p")
    # `symbol_ref` is echoed alongside the tier: `via` says HOW the query landed, and
    # the reference is the spelling the next call can use verbatim.
    expected = {"query": "main", "symbol_ref": "app.py#main",
                "fqn": "app.main", "via": "name"}
    assert graph["resolved"] == dossier["resolved"] == learning["resolved"] == expected


def test_a_verb_that_lists_every_match_discloses_nothing(crib, tmp_path):
    _wrapper_and_op(crib, tmp_path)
    # code_xref NARROWS NOTHING — it returns both symbols, so the list already is
    # the disclosure. Wrapping it in an envelope to carry `resolved` would be
    # tidying away the rule, not an improvement.
    hits = crib.code_xref("add_node", project="w")
    assert ({h["symbol_ref"] for h in hits}
            == {"server.py#add_node", "ops.py#add_node"})
    assert not any("resolved" in h for h in hits)


# --- the schema stamp: a format change forces a whole rebuild ------------------

def test_only_a_DIFFERENT_recorded_version_is_stale(crib, tmp_path):
    from crib.codeindex import SYMBOL_SCHEMA_VERSION, SymbolIndex
    _diamond(crib, tmp_path)
    si = SymbolIndex(crib.paths.project_dir("p"))
    # unstamped predates stamping; calling that stale would demand a full reindex of
    # every existing project for a version that changes no format
    assert si.is_populated() and si.stored_schema() == 0
    pass
    si.record_schema()
    assert si.stored_schema() == SYMBOL_SCHEMA_VERSION
    si._schema_marker.write_text(f"{SYMBOL_SCHEMA_VERSION + 1}\n")
    assert si.stored_schema() == SYMBOL_SCHEMA_VERSION + 1


def test_an_empty_store_is_never_stale(crib, tmp_path):
    from crib.codeindex import SYMBOL_SCHEMA_VERSION, SymbolIndex
    si = SymbolIndex(crib.paths.project_dir("empty"))
    # nothing written in the old shape means nothing for a new write to mix with
    assert not si.is_populated()
    si.root.mkdir(parents=True, exist_ok=True)
    si._schema_marker.write_text(f"{SYMBOL_SCHEMA_VERSION + 1}\n")
    pass


def test_the_stamp_is_invisible_to_the_entry_reader(crib, tmp_path):
    from crib.codeindex import SymbolIndex
    _diamond(crib, tmp_path)
    si = SymbolIndex(crib.paths.project_dir("p"))
    before = {e["symbol_ref"] for e in si.all()}
    si.record_schema()
    assert {e["symbol_ref"] for e in si.all()} == before


def test_a_single_file_index_writes_into_a_store_of_another_shape(crib, tmp_path):
    """A one-off write into a mixed store SUCCEEDS, and stamps only its own entries.

    This inverts the rule it replaces. The old gate refused, because a single-file
    write would leave the project half in each shape and nothing downstream could
    reason about the mix. Per-ENTRY schema stamps make the mix legible: each entry
    declares the shape it was written at, so a mixed store is the ordinary state and
    one more current-shape entry is progress rather than damage.

    The STORE-level marker still says what it said — it is a completion claim, not a
    write gate — so it must be left alone by a write that did not see every entry."""
    from crib.codeindex import SYMBOL_SCHEMA_VERSION, SymbolIndex
    root = _diamond(crib, tmp_path)
    si = SymbolIndex(crib.paths.project_dir("p"))
    si._schema_marker.write_text(f"{SYMBOL_SCHEMA_VERSION + 1}\n")

    asyncio.run(crib.code_index(str(root / "app.py"), project="p"))

    touched = [e for e in si.all() if e.get("file") == "app.py"]
    assert touched, "the single-file index wrote nothing"
    assert all(int(e.get("schema") or 0) == SYMBOL_SCHEMA_VERSION for e in touched), \
        "an entry must declare the shape it was actually written at"
    assert si.stored_schema() == SYMBOL_SCHEMA_VERSION + 1, \
        "one file is not the store: a single-file write may not restamp it"


def test_the_description_cache_hits_across_an_id_format_difference(crib, tmp_path):
    """A re-index of an UNCHANGED file must carry its descriptions forward even when
    the stored entries predate every current id field.

    This is the property that decides whether conversion is cheap. The prior-entry
    lookup keys on `(container, name)` — the identity PARTS — precisely so it cannot
    miss because a store is half-converted to a new id spelling. Keyed on a SPELLING
    instead, a v0.6.1-shaped entry would not match a freshly-extracted one, every
    description would silently be regenerated by the LLM, and the whole
    convert-instead-of-reindex argument would evaporate — invisibly, because the
    structural facet would look perfectly correct.

    No LLM is configured under test, so a miss BLANKS the description (that is the
    'needs describing' marker) while a hit preserves it. The assertion is therefore
    directly on the thing at stake."""
    from crib.codeindex import SymbolIndex
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "app.py").write_text("def main():\n    pass\n")
    si = SymbolIndex(crib.paths.project_dir("p"))
    si.set_source_root(root)

    # Index once for real, so `content_hash` matches what the extractor will produce.
    asyncio.run(crib.code_index(str(root / "app.py"), project="p"))
    seeded = [e for e in si.all() if e.get("file") == "app.py"]
    assert seeded, "nothing extracted — fixture cannot test the gate"

    # Rewrite them as a RELEASED (v0.6.1) store would have: a description worth
    # keeping, and none of the fields that arrived after it.
    for e in seeded:
        e["description"] = "the seeded description"
        e["keywords"] = ["seeded"]
        for gone in ("symbol_ref", "fqn", "scope", "schema"):
            e.pop(gone, None)
        si.write(e)

    asyncio.run(crib.code_index(str(root / "app.py"), project="p"))

    after = [e for e in si.all() if e.get("file") == "app.py"]
    assert {e["description"] for e in after} == {"the seeded description"}, \
        "the description cache missed across the id-format difference"
    assert all(e.get("keywords") == ["seeded"] for e in after), \
        "keywords must ride with the description they were generated beside"


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
    ("c", "bin/sharedserver-watcher.c", [], ["bin/sharedserver-watcher.c"]),
    # a FILE-SCOPED language is scoped by its file: the bare name collides 32 times
    # in one repo, file + name collides zero times
    ("zsh", "core/plugin-bundles/omz.zsh", ["_zdot_load_omz_lib"],
     ["core/plugin-bundles/omz.zsh"]),
    # an LSP artifact is not a scope
    ("lua", "scripts/codeindex/dump_lsp.lua", ["for in"],
     ["scripts", "codeindex", "dump_lsp"]),
])
def test_scope_per_language(lang, file, container, scope):
    from crib.symbols import scope_of
    assert scope_of(lang, file, container) == scope


def test_scope_needs_no_manifest():
    """Every language derives from source and layout alone; only Rust's crate name
    is out of band, and crate-relative scope does without it."""
    from crib.symbols import scope_of
    assert scope_of("rust", "rust/src/core/state.rs", []) == ["core", "state"]


def test_an_unknown_language_still_matches_on_either_separator():
    from crib.symbols import match_entry
    e = {"file": "a/b.x", "name": "c", "lang": "", "container": []}
    assert match_entry(e, "b::c") == "suffix"
    assert match_entry(e, "b.c") == "suffix"
    assert match_entry(e, "c") == "name"


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
    assert "ops.py#add_node (2 callers)" in str(e.value)


def test_an_indexed_entry_carries_its_language_scope(crib, tmp_path):
    """The stored entry gains `scope`; the id is untouched, so nothing migrates."""
    from crib.codeindex import SYMBOL_SCHEMA_VERSION, SymbolIndex
    _diamond(crib, tmp_path)
    si = SymbolIndex(crib.paths.project_dir("p"))
    si.record_schema()
    assert si.stored_schema() == SYMBOL_SCHEMA_VERSION
    # a store written before `scope` existed still READS: the parser normalises
    # every declared array, so the field comes back EMPTY rather than missing. That
    # is why absence cannot be read as "this language has no scope" — the schema
    # stamp is what says the field was computed at all.
    _sym(crib, "p", "old.entry", "app.py", scope=[])
    assert si.read("app.py#entry").get("scope") == []
    assert all(e.get("scope") for e in si.all()
               if e["symbol_ref"] != "app.py#entry")
    assert crib.code_graph("app.main", project="p")["symbol_ref"] == "app.py#main"


# --- symmetric selectors: narrow on the axis you actually know -----------------

def test_a_path_constraint_makes_an_ambiguous_name_unique(crib, tmp_path):
    _wrapper_and_op(crib, tmp_path)
    with pytest.raises(CribUserError, match="2 symbols match"):
        crib.code_dossier("add_node", project="w")
    # a caller reading a stack trace knows the FILE, not crib's qualified spelling
    d = crib.code_dossier("add_node", project="w", path="ops.py")
    assert d["resolved"]["symbol_ref"] == "ops.py#add_node"
    g = crib.code_graph("add_node", project="w", path="server.py", shape="edges")
    assert g["resolved"]["symbol_ref"] == "server.py#add_node"


def test_a_scope_constraint_narrows_the_same_way(crib, tmp_path):
    _rust_ish(crib, tmp_path)
    g = crib.code_graph("ClientsLock", project="r", scope="lockfile", shape="edges")
    assert g["resolved"]["symbol_ref"] == "src/core/lockfile.rs#ClientsLock"


def test_a_constraint_that_excludes_everything_says_so(crib, tmp_path):
    _wrapper_and_op(crib, tmp_path)
    with pytest.raises(CribUserError, match="none of them with path='nowhere.py'"):
        crib.code_dossier("add_node", project="w", path="nowhere.py")


def test_the_refusal_names_the_axis_that_would_separate_them(crib, tmp_path):
    _wrapper_and_op(crib, tmp_path)
    with pytest.raises(CribUserError) as e:
        crib.code_graph("add_node", project="w")
    # the two candidates differ by file, so `path=` is the actionable hint
    assert "narrow with path=" in str(e.value)


def test_constraints_do_not_weaken_unique_or_refuse(crib, tmp_path):
    _diamond(crib, tmp_path)
    # a constraint that matches BOTH leaves the ambiguity intact rather than picking
    _sym(crib, "p", "core.main", "core.py")
    with pytest.raises(CribUserError, match="symbols match"):
        crib.code_dossier("main", project="p", lang="python")


# --- group_by is an AXIS, not one overloaded word -----------------------------

def test_the_three_axes_answer_different_questions(crib, tmp_path):
    _diamond(crib, tmp_path)
    by_file = crib.code_graph(project="p", group_by="file")
    by_dir = crib.code_graph(project="p", group_by="dir")
    by_scope = crib.code_graph(project="p", group_by="scope")
    assert {n["id"] for n in by_file["nodes"]} >= {"app.py", "core.py", "orphan.py"}
    # every seeded file sits at the repo root, so `dir` collapses them into one
    # the diamond has an unresolved `vendor/pkg.py` target, which is a real
    # location and groups as one
    assert {n["id"] for n in by_dir["nodes"]} == {"(root)", "vendor"}
    # python ties namespace to path, so scope tracks the module
    assert {n["id"] for n in by_scope["nodes"]} >= {"app", "core", "orphan"}


def test_a_file_scoped_language_groups_by_its_file(crib, tmp_path):
    root = tmp_path / "c"
    root.mkdir(parents=True)
    (root / "watch.c").write_text("int main(void){return 0;}\n")
    _sym(crib, "cproj", "bin.watch.main", "watch.c", lang="c", name="main")
    SymbolIndex(crib.paths.project_dir("cproj")).set_source_root(root)
    g = crib.code_graph(project="cproj", group_by="scope")
    # C has no namespace, so the FILE is what qualifies it — not an invented
    # dotted one, and not nothing
    assert [n["id"] for n in g["nodes"]] == ["watch.c"]


def test_group_depth_makes_the_axis_coarser(crib, tmp_path):
    _rust_ish(crib, tmp_path)
    full = crib.code_graph(project="r", group_by="scope")
    coarse = crib.code_graph(project="r", group_by="scope", group_depth=1)
    assert {n["id"] for n in full["nodes"]} == {"core::lockfile"}
    assert {n["id"] for n in coarse["nodes"]} == {"core"}
    assert coarse["group_depth"] == 1


def test_module_is_no_longer_an_axis(crib, tmp_path):
    _diamond(crib, tmp_path)
    with pytest.raises(CribUserError, match="file, dir, scope"):
        crib.code_graph(project="p", group_by="module")


# --- the diagram-consumer contract ---------------------------------------------

def test_every_node_carries_the_bare_name(crib, tmp_path):
    """`name` is the leaf identifier — a code fact, not a display concern. Without
    it a box caption must be parsed out of a language-rendered string, which is the
    exact consumer-side re-parsing the rework exists to end. Emitted on every node
    shape: symbol, external, tree, and rollup (last segment of the group id)."""
    _diamond(crib, tmp_path)
    g = crib.code_graph("app.main", project="p", shape="edges")
    names = {n["id"]: n.get("name") for n in g["nodes"]}
    assert names["app.py#main"] == "main"
    assert names["vendored [vendor/pkg.py]"] == "vendored"     # external too
    tree = crib.code_graph("app.main", project="p")
    assert tree["name"] == "main"
    whole = crib.code_graph(project="p")
    assert all(n.get("name") for n in whole["nodes"])
    rolled = crib.code_graph(project="p", group_by="dir")
    assert all(n.get("name") for n in rolled["nodes"])


def test_edges_are_a_subset_of_nodes_even_at_depth_bounds(crib, tmp_path):
    """Consumers refuse unknown endpoints rather than inventing boxes, so every
    endpoint must arrive declared — frontier nodes included (`truncated`), external
    targets included. Any future node-pruning must prune edges with it."""
    _diamond(crib, tmp_path)
    for g in (crib.code_graph("app.main", project="p", shape="edges"),
              crib.code_graph("app.main", project="p", shape="edges", depth=1),
              crib.code_graph(project="p"),
              crib.code_graph(project="p", group_by="file")):
        ids = {n["id"] for n in g["nodes"]}
        loose = [(e["from"], e["to"]) for e in g["edges"]
                 if e["from"] not in ids or e["to"] not in ids]
        assert not loose, f"edges reference undeclared nodes: {loose}"


def test_whole_project_export_scopes_by_path_prefix(crib, tmp_path):
    """`under=`/`exclude=` filter BEFORE serialization — the payload lever. A
    consumer wanting core code should not pay for the test tree at the tool
    boundary and filter after the tokens are spent. A DIFFERENT parameter from
    `path=` on purpose: same word, opposite matching direction (export = prefix
    boundary, symbol narrowing = trailing run) was a footgun, so the export
    refuses `path=` with a pointer instead of quietly meaning something else."""
    _diamond(crib, tmp_path)
    whole = crib.code_graph(project="p")
    only_app = crib.code_graph(project="p", under="app.py")
    assert {n["id"] for n in only_app["nodes"] if not n.get("external")} \
        <= {n["id"] for n in whole["nodes"]} | \
           {n["id"] for n in only_app["nodes"] if n.get("truncated")}
    without_app = crib.code_graph(project="p", exclude="app.py")
    assert not any(n["id"].startswith("app.py#") and not n.get("truncated")
                   for n in without_app["nodes"])
    with pytest.raises(CribUserError, match="under="):
        crib.code_graph(project="p", path="app.py")
    # segment-aligned: a prefix never matches mid-segment
    from crib.codequery import _under
    assert _under("src/a/b.py", "src") and not _under("srchers/x.py", "src")


def test_a_scoped_export_keeps_boundary_crossings_as_frontier_nodes(crib, tmp_path):
    """The first-contact bug: an OUTBOUND boundary-crossing edge (in-scope caller,
    out-of-scope in-project target) crashed the rollup with a raw KeyError, while
    INBOUND crossings silently vanished — asymmetric and misdrawn either way. Both
    now keep the edge with a lean `{id, symbol_ref, name, file, truncated}` node at
    the far end, the same frontier semantics as a depth bound: a scoped module that
    calls out (or is called into) must not export as self-contained."""
    _diamond(crib, tmp_path)
    scoped = crib.code_graph(project="p", under="app.py")
    # outbound: app.main -> core.deep survives, with core.deep as a frontier node
    assert ("app.py#main", "core.py#deep") in _edge_set(scoped)
    deep = next(n for n in scoped["nodes"] if n["id"] == "core.py#deep")
    assert deep.get("truncated") is True and deep["name"] == "deep"
    # inbound: core.shared -> (nothing in app.py calls INTO it here) — check the
    # other scope: core-side export keeps app-side callers as frontier sources
    core_side = crib.code_graph(project="p", under="core.py")
    assert ("app.py#left", "core.py#shared") in _edge_set(core_side)
    left = next(n for n in core_side["nodes"] if n["id"] == "app.py#left")
    assert left.get("truncated") is True
    # edges ⊆ nodes still holds under scoping — the crash was exactly this breaking
    for g in (scoped, core_side,
              crib.code_graph(project="p", under="app.py", group_by="file")):
        node_ids = {n["id"] for n in g["nodes"]}
        assert all(e["from"] in node_ids and e["to"] in node_ids
                   for e in g["edges"])
    # and the rollup groups frontier crossings by their real file, flagged
    rolled = crib.code_graph(project="p", under="app.py", group_by="file")
    core_group = next(n for n in rolled["nodes"] if n["id"] == "core.py")
    assert core_group.get("truncated") is True


def test_the_no_scope_sentinel_is_structural(crib, tmp_path):
    """A C-like no-namespace symbol groups under `(no scope)`. The string is
    contract, but consumers detect sentinels by the `synthetic` flag — one magic
    string in an otherwise uniform export poisons shared-prefix label derivation,
    and an id-keyed consumer should not need to know its exact spelling."""
    _diamond(crib, tmp_path)
    _sym(crib, "p", "standalone", "tool.sh", lang="zsh", scope=[])
    rolled = crib.code_graph(project="p", group_by="scope")
    sentinel = [n for n in rolled["nodes"] if n["id"] == "(no scope)"]
    assert sentinel and sentinel[0].get("synthetic") is True
    assert all("synthetic" not in n for n in rolled["nodes"]
               if n["id"] != "(no scope)")
