"""Design decisions & plan items (docs/plans/design-plan-tracking.md).

The load-bearing claim is decision 3: staleness is COMPUTED from body hashes on
read, never propagated on write — so a decision edited by ANY route (here: a
plain `note_edit`, which knows nothing about designs) taints its dependents all
the same. `test_taint_survives_a_plain_note_edit` is that end-to-end; the rest
pins the graph machinery it rests on (cycles, transitive taint, ranks, refs).
"""

from __future__ import annotations

import asyncio
import random

import pytest

from crib.app import Crib
from crib.config import Config
from crib.designs import Node, _cycles, _rank_between
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


def _node(nid: str, deps: list[str], checked: dict[str, str] | None = None,
          body_hash: str = "h", kind: str = "design", status: str = "active") -> Node:
    return Node(id=nid, kind=kind, relpath=f"{kind}/{nid}.md", title=nid,
                status=status, deps=deps, checked=checked or {}, rank="",
                body_hash=body_hash, frontmatter={})


# ── lexorank (decision 5) ─────────────────────────────────────────────────────

def test_rank_between_is_strictly_between():
    assert "" < _rank_between() < "z"
    a = _rank_between()
    assert a < _rank_between(a, None)
    assert _rank_between(None, a) < a
    assert a < _rank_between(a, "n") < "n"


def test_rank_between_property_over_random_insertions():
    """The invariant that makes insert-between free: a < rank_between(a,b) < b,
    for every pair reachable by repeatedly splitting gaps."""
    rng = random.Random(20260805)
    ranks = [_rank_between(), _rank_between(_rank_between(), None)]
    ranks.sort()
    for _ in range(300):
        i = rng.randrange(len(ranks) + 1)
        lo = ranks[i - 1] if i > 0 else None
        hi = ranks[i] if i < len(ranks) else None
        r = _rank_between(lo, hi)
        assert (lo is None or lo < r) and (hi is None or r < hi), (lo, r, hi)
        assert not r.endswith("a"), f"{r} leaves no gap below it"
        ranks.insert(i, r)
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks)


def test_rank_between_rejects_reversed_or_adjacent_bounds():
    with pytest.raises(ValueError, match="out of order"):
        _rank_between("t", "m")
    with pytest.raises(ValueError, match="adjacent"):
        _rank_between("m", "ma")        # hand-written: nothing sorts between them


# ── cycles ────────────────────────────────────────────────────────────────────

def test_cycle_detection_finds_each_cycle_once():
    nodes = {n.id: n for n in (_node("A", ["B"]), _node("B", ["C"]),
                               _node("C", ["A"]), _node("D", ["A"]))}
    cycles = _cycles(nodes)
    assert len(cycles) == 1 and set(cycles[0]) == {"A", "B", "C"}
    assert _cycles({n.id: n for n in (_node("A", ["B"]), _node("B", []))}) == []


def test_dangling_dep_is_not_a_cycle():
    assert _cycles({"A": _node("A", ["GONE"])}) == []


def test_dep_add_refuses_to_create_a_cycle(crib):
    a = run(crib.design_add("A", "a", project="p"))
    b = run(crib.design_add("B", "b", deps=[a["relpath"]], project="p"))
    with pytest.raises(ValueError, match="cycle"):
        run(crib.design_dep_add(a["relpath"], b["relpath"], project="p"))
    with pytest.raises(ValueError, match="itself"):
        run(crib.design_dep_add(a["relpath"], a["relpath"], project="p"))


# ── taint (decision 3) ────────────────────────────────────────────────────────

def _taint(crib, nodes):
    from crib.designs import Graph
    graph = Graph(nodes={n.id: n for n in nodes})
    for n in nodes:
        for d in n.deps:
            graph.dependents.setdefault(d, []).append(n.id)
    return crib.designs._taint(graph)


def test_taint_is_direct_transitive_and_missing_dep(crib):
    #  C → B → A ; A's body moved on since B recorded it, so B *and* C are stale
    a = _node("A", [], body_hash="new")
    b = _node("B", ["A"], checked={"A": "old"})
    c = _node("C", ["B"], checked={"B": "h"})
    d = _node("D", ["GONE"], checked={"GONE": "x"})
    e = _node("E", [])
    t = _taint(crib, [a, b, c, d, e])
    assert set(t) == {"B", "C", "D"}                     # A and E are clean
    assert "changed" in t["B"]["reasons"][0]
    assert t["C"]["paths"][0]["chain"] == ["C", "B"]     # the explaining chain
    assert "missing" in t["D"]["reasons"][0]


def test_an_unverified_dep_edge_is_tainted(crib):
    """`dep_add` deliberately doesn't seed `checked` — the new edge shows up in
    `check` as the nudge to actually reconsider the decision."""
    t = _taint(crib, [_node("A", []), _node("B", ["A"])])
    assert "never verified" in t["B"]["reasons"][0]


def test_taint_survives_a_plain_note_edit(crib):
    """The integration the whole design rests on: add A, add B-dep-A (clean) →
    edit A by the ORDINARY note verb → B is tainted, with the chain → verify B →
    clean again."""
    a = run(crib.design_add("Disk is truth", "chroma is a cache", project="p"))
    b = run(crib.design_add("Policies per tool", "three policies",
                            deps=["Disk is truth"], project="p"))
    assert crib.design_check(project="p")["clean"]        # born verified

    run(crib.edit_note(a["relpath"], "chroma is a REBUILDABLE cache", project="p"))

    check = crib.design_check(project="p")
    assert not check["clean"] and len(check["tainted"]) == 1
    row = check["tainted"][0]
    assert row["id"] == b["id"] and "changed" in row["reasons"][0]
    assert row["paths"][0]["chain"] == ["Policies per tool"]
    assert crib.design_check(ref=a["relpath"], project="p")["tainted"] == []

    run(crib.design_verify(b["relpath"], project="p"))
    assert crib.design_check(project="p")["clean"]


def test_frontmatter_churn_does_not_taint(crib):
    """The body hash excludes frontmatter, so `updated`/status/rank changes —
    which every write stamps — must not cascade taint."""
    run(crib.design_add("A", "body", project="p"))
    b = run(crib.design_add("B", "body", deps=["A"], project="p"))
    run(crib.design_dep_add(b["relpath"], "A", project="p"))   # no-op re-add
    run(crib.design_verify("A", project="p"))                  # rewrites A's fm
    assert crib.design_check(project="p")["clean"]


def test_supersede_taints_dependents_and_forget_blocks(crib):
    a = run(crib.design_add("A", "body", project="p"))
    run(crib.design_add("B", "body", deps=["A"], project="p"))
    with pytest.raises(ValueError, match="dependent"):
        run(crib.design_forget("A", project="p"))
    out = run(crib.design_supersede("A", by_ref="B", project="p"))
    assert out["status"] == "superseded" and out["tainted_dependents"]
    assert "superseded" in crib.design_check(project="p")["tainted"][0]["reasons"][0]
    forced = run(crib.design_forget(a["relpath"], force=True, project="p"))
    assert forced["forced"] and forced["recoverable_id"] == a["id"]
    assert "missing" in crib.design_check(project="p")["tainted"][0]["reasons"][0]


# ── ref resolution ────────────────────────────────────────────────────────────

def test_refs_resolve_by_id_prefix_relpath_and_title(crib):
    a = run(crib.design_add("Disk is truth", "body", project="p"))
    graph = crib.designs._load_graph("p")
    for ref in (a["id"], a["id"][:8], a["relpath"], "disk-is-truth.md",
                "disk-is-truth", "Disk is truth"):
        assert crib.designs._resolve_ref(graph, ref).id == a["id"], ref
    with pytest.raises(ValueError, match="no design/plan note matches"):
        crib.designs._resolve_ref(graph, "nope")


def test_ambiguous_ref_lists_candidates(crib):
    run(crib.design_add("Same", "one", project="p"))
    run(crib.design_add("Same", "two", project="p"))       # → same-2.md
    graph = crib.designs._load_graph("p")
    with pytest.raises(ValueError, match="ambiguous"):
        crib.designs._resolve_ref(graph, "Same")
    assert crib.designs._resolve_ref(graph, "same-2.md").title == "Same"


def test_a_ref_does_not_leak_across_facets(crib):
    run(crib.design_add("Shared name", "d", project="p"))
    run(crib.plan_add("Shared name", "p", project="p"))
    graph = crib.designs._load_graph("p")
    assert crib.designs._resolve_ref(graph, "Shared name", "plan").kind == "plan"
    assert crib.designs._resolve_ref(graph, "Shared name", "design").kind == "design"


# ── plan ordering (decision 5/6) ──────────────────────────────────────────────

def _titles(rows):
    return [r["title"] for r in rows]


def test_plan_order_is_topological_with_rank_as_tie_breaker(crib):
    run(crib.plan_add("one", "x", project="p"))
    run(crib.plan_add("two", "x", project="p"))
    run(crib.plan_add("three", "x", project="p"))
    # rank order is one/two/three; the dep forces three BEFORE two regardless
    run(crib.plan_dep_add("two", "three", project="p"))
    listed = crib.plan_list(project="p")
    assert _titles(listed["items"]) == ["one", "three", "two"]
    assert [r["blocked"] for r in listed["items"]] == [False, False, True]
    assert _titles(crib.plan_next(project="p")["items"]) == ["one", "three"]

    run(crib.plan_status("three", "done", project="p"))
    assert _titles(crib.plan_next(project="p")["items"]) == ["one", "two"]
    assert _titles(crib.plan_list(project="p")["items"]) == ["one", "two"]
    assert crib.plan_list(all=True, project="p")["hidden"] == 0
    assert crib.plan_list(project="p")["hidden"] == 1


def test_plan_move_reorders_without_touching_deps(crib):
    run(crib.plan_add("one", "x", project="p"))
    run(crib.plan_add("two", "x", project="p"))
    three = run(crib.plan_add("three", "x", deps=["one"], project="p"))
    assert _titles(crib.plan_list(project="p")["items"]) == ["one", "two", "three"]
    moved = run(crib.plan_move("three", before="two", project="p"))
    assert moved["deps"] == three["deps"]                  # untouched
    assert _titles(crib.plan_list(project="p")["items"]) == ["one", "three", "two"]
    # …and a move can never break correctness: the dep still orders it
    run(crib.plan_move("three", before="one", project="p"))
    assert _titles(crib.plan_list(project="p")["items"]) == ["one", "three", "two"]


def test_plan_add_places_relative_to_neighbours(crib):
    run(crib.plan_add("one", "x", project="p"))
    run(crib.plan_add("three", "x", project="p"))
    run(crib.plan_add("two", "x", after="one", before="three", project="p"))
    run(crib.plan_add("zero", "x", before="one", project="p"))
    assert _titles(crib.plan_list(project="p")["items"]) == ["zero", "one", "two",
                                                            "three"]


def test_plan_status_validates_and_warns_on_open_deps(crib):
    run(crib.plan_add("one", "x", project="p"))
    run(crib.plan_add("two", "x", deps=["one"], project="p"))
    with pytest.raises(ValueError, match="unknown status"):
        run(crib.plan_status("one", "blocked", project="p"))
    out = run(crib.plan_status("two", "done", project="p"))   # warns, doesn't block
    assert out["status"] == "done" and out["warnings"]


# ── the notes are ordinary notes ──────────────────────────────────────────────

def test_type_reaches_chunk_metadata_so_lookup_can_filter(crib):
    run(crib.design_add("Vector store", "chroma holds the vectors", project="p"))
    run(crib.store_note("chroma holds the vectors", title="Plain", project="p"))
    metas = crib.store.get_meta({"project": "p"}).values()
    assert {m.get("type") for m in metas} == {"design", ""}
    hits = crib.lookup("chroma vectors", project="p", tags=["design"])
    assert [h.relpath for h in hits] == ["design/vector-store.md"]
