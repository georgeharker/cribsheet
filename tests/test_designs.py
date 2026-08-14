"""Design decisions & plan items (docs/plans/design-plan-tracking.md).

The load-bearing claim is decision 3: staleness is COMPUTED from body hashes on
read, never propagated on write — so a decision edited by ANY route (here: a
direct write to the FILE, which knows nothing about designs — an editor, a git
pull) taints its dependents all the same. `test_taint_survives_a_plain_file_edit`
is that end-to-end; the rest pins the graph machinery it rests on (cycles,
transitive taint, ranks, refs). Design/plan notes live in their own pillar
stores (`projects/<p>/design/`, `plans/`), so the note verbs refuse their paths
— the facet verbs and the file itself are the two ways in.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from crib import notes
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


def _set_deps(crib, ref: str, deps: list[str], kind: str = "plan") -> None:
    """Plant raw dep ids on a node, bypassing `*_dep_add`'s ref resolution — how a
    hand edit or a git pull introduces a dep the graph can't resolve to a node."""
    graph = crib.designs._load_graph("p")
    node = crib.designs._resolve_ref(graph, ref, kind)
    run(crib.designs._save("p", node, {"deps": deps}))


def _node(nid: str, deps: list[str], checked: dict[str, str] | None = None,
          body_hash: str = "h", kind: str = "design", status: str = "active") -> Node:
    return Node(id=nid, kind=kind, relpath=f"{nid}.md", title=nid,
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


def test_taint_survives_a_plain_file_edit(crib):
    """The integration the whole design rests on: add A, add B-dep-A (clean) →
    edit A by writing the FILE directly (an editor, a git pull — no facet verb,
    no crib at all) → B is tainted, with the chain → verify B → clean again."""
    a = run(crib.design_add("Disk is truth", "chroma is a cache", project="p"))
    b = run(crib.design_add("Policies per tool", "three policies",
                            deps=["Disk is truth"], project="p"))
    assert crib.design_check(project="p")["clean"]        # born verified

    path = crib.designstore.abspath("p", a["relpath"])
    path.write_text(path.read_text().replace(
        "chroma is a cache", "chroma is a REBUILDABLE cache"))

    check = crib.design_check(project="p")
    assert not check["clean"] and len(check["tainted"]) == 1
    row = check["tainted"][0]
    assert row["id"] == b["id"] and "changed" in row["reasons"][0]
    assert row["paths"][0]["chain"] == ["Policies per tool"]
    assert crib.design_check(ref=a["relpath"], project="p")["tainted"] == []

    run(crib.design_reaffirm(b["relpath"], project="p"))
    assert crib.design_check(project="p")["clean"]


def test_frontmatter_churn_does_not_taint(crib):
    """The body hash excludes frontmatter, so `updated`/status/rank changes —
    which every write stamps — must not cascade taint."""
    run(crib.design_add("A", "body", project="p"))
    b = run(crib.design_add("B", "body", deps=["A"], project="p"))
    run(crib.design_dep_add(b["relpath"], "A", project="p"))   # no-op re-add
    run(crib.design_reaffirm("A", project="p"))                # rewrites A's fm
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


def _by_rank(rows):
    """Titles in RANK order. `plan_list` renders by working-set group (in-progress
    → ready → blocked), so rank shows through only within a group; these tests are
    about the rank arithmetic, hence the explicit re-sort."""
    return [r["title"] for r in sorted(rows, key=lambda r: r["rank"])]


def test_plan_move_reorders_without_touching_deps(crib):
    run(crib.plan_add("one", "x", project="p"))
    run(crib.plan_add("two", "x", project="p"))
    three = run(crib.plan_add("three", "x", deps=["one"], project="p"))
    assert _by_rank(crib.plan_list(project="p")["items"]) == ["one", "two", "three"]
    moved = run(crib.plan_move("three", before="two", project="p"))
    assert moved["deps"] == three["deps"]                  # untouched
    assert _by_rank(crib.plan_list(project="p")["items"]) == ["one", "three", "two"]
    # …and a move can never break correctness: the dep still orders it. `three`
    # ranks first now, but its dep keeps it BEHIND `one` in execution order.
    run(crib.plan_move("three", before="one", project="p"))
    assert _by_rank(crib.plan_list(project="p")["items"]) == ["three", "one", "two"]
    assert _titles(crib.plan_list(project="p")["items"])[0] == "one"


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


# ── the facet is the interface: dossier, edge-aware writes, facet retrieval ───

def test_design_read_is_a_dossier_not_a_file_fetch(crib):
    """`design_read` answers the question `note_read` can't: what does this rest
    on, what rests on it, and has either moved under me."""
    run(crib.design_add("Base", "the ground", project="p"))
    run(crib.design_add("Middle", "on the ground", deps=["Base"], project="p"))
    run(crib.design_add("Top", "on the middle", deps=["Middle"], project="p"))

    d = crib.design_read("Middle", project="p")
    assert d["title"] == "Middle" and "on the ground" in d["body"]
    assert [x["title"] for x in d["deps"]] == ["Base"]
    assert [x["title"] for x in d["dependents"]] == ["Top"]
    assert d["tainted"] is False and "next" not in d
    assert d["deps"][0]["status"] == "active" and d["deps"][0]["tainted"] is False

    path = crib.designstore.abspath("p", "base.md")
    path.write_text(path.read_text().replace("the ground", "the ground MOVED"))
    d = crib.design_read("Middle", project="p")
    assert d["tainted"] and d["causes"][0]["change_kind"] == "dep-edited"
    assert d["causes"][0]["dep_title"] == "Base"
    # the dep itself is CLEAN (it is what moved); the DEPENDENT is flagged in
    # place, so the dossier shows the blast radius without a second call
    assert d["deps"][0]["tainted"] is False
    assert d["dependents"][0]["tainted"] is True
    assert "design_reaffirm" in d["next"]           # never a flag with no verb


def test_design_edit_reports_what_it_tainted(crib):
    """The edge-aware write: the answer to "I changed this" is "…and here is what
    that just put out of date", computed against the PRE-edit state."""
    run(crib.design_add("Base", "the ground", project="p"))
    run(crib.design_add("Middle", "on the ground", deps=["Base"], project="p"))
    run(crib.design_add("Top", "on the middle", deps=["Middle"], project="p"))
    assert crib.design_check(project="p")["clean"]

    out = run(crib.design_edit("Base", "the ground, rewritten", project="p"))
    # direct dependent AND the transitive one, each with its explaining chain
    assert {n["title"] for n in out["newly_tainted"]} == {"Middle", "Top"}
    middle = next(n for n in out["newly_tainted"] if n["title"] == "Middle")
    assert middle["via"] == ["Middle"]
    top = next(n for n in out["newly_tainted"] if n["title"] == "Top")
    assert top["via"] == ["Top → Middle"]
    assert "design_reaffirm" in out["next"]
    assert crib.designstore.read("p", "base.md").endswith(
        "the ground, rewritten\n")

    # already-tainted dependents are not re-reported as NEWLY tainted
    again = run(crib.design_edit("Base", "rewritten twice", project="p"))
    assert again["newly_tainted"] == [] and "next" not in again


def test_design_append_extends_and_is_edge_aware(crib):
    run(crib.design_add("Base", "first para", project="p"))
    run(crib.design_add("Leaf", "builds on base", deps=["Base"], project="p"))
    out = run(crib.design_append("Base", "second para", project="p"))
    body = crib.designstore.read("p", "base.md")
    assert "first para" in body and "second para" in body
    assert [n["title"] for n in out["newly_tainted"]] == ["Leaf"]


def test_design_list_tables_and_filters_to_the_stale(crib):
    run(crib.design_add("Base", "ground", project="p"))
    run(crib.design_add("Leaf", "on base", deps=["Base"], project="p"))
    listed = crib.design_list(project="p")
    assert [r["title"] for r in listed["designs"]] == ["Base", "Leaf"]
    assert listed["total"] == 2 and listed["tainted"] == 0
    base = next(r for r in listed["designs"] if r["title"] == "Base")
    assert base["deps"] == 0 and base["dependents"] == 1

    run(crib.design_edit("Base", "ground moved", project="p"))
    stale = crib.design_list(tainted=True, project="p")
    assert [r["title"] for r in stale["designs"]] == ["Leaf"]
    assert stale["total"] == 2 and stale["filtered"] is True


def test_design_add_probes_for_near_duplicate_decisions(crib):
    """A near-duplicate DECISION forks the graph, so `design_add` runs the same
    dedupe probe `note_store` does."""
    run(crib.design_add("Chroma is a cache",
                        "the vector store is derived and rebuildable", project="p"))
    out = run(crib.design_add("Chroma is a cache, restated",
                              "the vector store is derived and rebuildable",
                              project="p"))
    assert [s["relpath"] for s in out["similar"]] == ["chroma-is-a-cache.md"]


def test_a_decision_must_carry_its_rationale(crib):
    with pytest.raises(ValueError, match="needs a body"):
        run(crib.design_add("Bare", "   ", project="p"))
    run(crib.plan_add("Bare item", project="p"))     # a plan item may be title-only
    assert _titles(crib.plan_list(project="p")["items"]) == ["Bare item"]


def test_facet_lookup_annotates_hits_with_status_and_taint(crib):
    run(crib.design_add("Vector store", "chroma holds the vectors", project="p"))
    run(crib.plan_add("Swap the vector store", "chroma holds the vectors",
                      project="p"))

    designs = crib.design_lookup("chroma vectors", project="p")
    assert [h["relpath"] for h in designs] == ["vector-store.md"]
    assert designs[0]["status"] == "active" and designs[0]["tainted"] is False
    assert designs[0]["deps"] == 0 and designs[0]["kind"] == "design"

    plans = crib.plan_lookup("chroma vectors", project="p")
    assert [h["relpath"] for h in plans] == ["swap-the-vector-store.md"]
    assert plans[0]["status"] == "todo"


def test_a_stale_decision_says_so_on_every_retrieval_hit(crib):
    """The ambient marker: the agent retrieving a stale decision is told at the
    moment it is reasoning from it — not only if it thinks to run `design_check`."""
    run(crib.design_add("Ground", "chroma holds the vectors", project="p"))
    run(crib.design_add("Built on it", "chroma holds the vectors, so we cache",
                        deps=["Ground"], project="p"))
    clean = crib.lookup("chroma vectors", project="p", store="design")
    assert clean and not any(h.tainted for h in clean)

    run(crib.design_edit("Ground", "postgres holds the vectors now", project="p"))
    hits = {h.relpath: h.tainted
            for h in crib.lookup("chroma vectors", project="p", store="design")}
    assert hits["built-on-it.md"] is True            # its ground moved
    assert hits["ground.md"] is False                # it IS the ground
    # …and the same flag rides the facet-scoped lookup verb
    assert any(h["tainted"] for h in crib.design_lookup("chroma vectors",
                                                        project="p"))


def test_status_counts_stale_decisions_per_project(crib):
    run(crib.design_add("Ground", "ground", project="p"))
    run(crib.design_add("Leaf", "on ground", deps=["Ground"], project="p"))
    row = next(r for r in crib.status()["projects"] if r["project"] == "p")
    assert "design_tainted" not in row               # silent when nothing is stale
    run(crib.design_edit("Ground", "ground moved", project="p"))
    row = next(r for r in crib.status()["projects"] if r["project"] == "p")
    assert row["design_tainted"] == 1


# ── check output prescribes its follow-up ─────────────────────────────────────

def test_check_names_the_change_kind_the_date_and_the_next_verb(crib):
    run(crib.design_add("Ground", "ground", project="p"))
    run(crib.design_add("Leaf", "on ground", deps=["Ground"], project="p"))
    run(crib.design_edit("Ground", "ground moved", project="p"))

    row = crib.design_check(project="p")["tainted"][0]
    cause = row["causes"][0]
    assert cause["change_kind"] == "dep-edited"
    assert cause["dep_title"] == "Ground" and cause["dep_updated"]
    assert "design_reaffirm leaf.md" in row["next"]
    assert "design_supersede" in row["next"]         # …and the other outcome

    # a superseded dep and a deleted one are DIFFERENT kinds, not one "changed"
    run(crib.design_reaffirm("Leaf", project="p"))
    run(crib.design_supersede("Ground", project="p"))
    assert crib.design_check(project="p")["tainted"][0]["causes"][0]["change_kind"] \
        == "dep-superseded"
    run(crib.design_forget("Ground", force=True, project="p"))
    assert crib.design_check(project="p")["tainted"][0]["causes"][0]["change_kind"] \
        == "dep-deleted"


def test_a_new_edge_is_its_own_change_kind(crib):
    run(crib.design_add("Ground", "ground", project="p"))
    run(crib.design_add("Leaf", "standalone", project="p"))
    run(crib.design_dep_add("Leaf", "Ground", project="p"))
    cause = crib.design_check(project="p")["tainted"][0]["causes"][0]
    assert cause["change_kind"] == "new-unverified-edge"


# ── plan: mixed deps, batches, and edge-aware completion ──────────────────────

def test_plan_next_mixed_dep_matrix(crib):
    """The three dep kinds a plan item can carry, and what each does to it: a plan
    dep gates until done, a design dep gates only while TAINTED, a plain note dep
    never gates at all."""
    run(crib.design_add("Ground", "stable ground", project="p"))
    run(crib.design_add("Origin", "what ground rests on", project="p"))
    note = run(crib.store_note("just a reference", title="Ref", project="p"))
    note_id = notes.load(crib.abspath("p", note["relpath"])).frontmatter["id"]

    run(crib.plan_add("earlier", "x", project="p"))
    run(crib.plan_add("on a plan dep", "x", deps=["earlier"], project="p"))
    run(crib.plan_add("on a design dep", "x", deps=["Ground"], project="p"))
    run(crib.plan_add("on a note dep", "x", project="p"))
    # a note dep can only arrive by hand (or a git pull): `plan_dep_add` resolves
    # against the graph, and a plain note isn't in it. That it can arrive at all is
    # exactly why the rule has to be stated.
    _set_deps(crib, "on a note dep", [note_id])

    # the design dep is UNTAINTED — stable ground, so it does not block
    ready = _titles(crib.plan_next(project="p")["items"])
    assert ready == ["earlier", "on a design dep", "on a note dep"]
    row = next(r for r in crib.plan_list(project="p")["items"]
               if r["title"] == "on a note dep")
    assert row["note_deps"] == [note_id] and row["missing_deps"] == []

    # …taint that decision and the item built on it stops being actionable
    run(crib.design_dep_add("Ground", "Origin", project="p"))
    ready = _titles(crib.plan_next(project="p")["items"])
    assert ready == ["earlier", "on a note dep"]
    blocked = next(r for r in crib.plan_list(project="p")["items"]
                   if r["title"] == "on a design dep")
    assert blocked["blocked"] and blocked["blocked_by"][0]["kind"] == "design"

    # a plan dep gates until it is done, as it always did
    run(crib.plan_status("earlier", "done", project="p"))
    assert "on a plan dep" in _titles(crib.plan_next(project="p")["items"])


def test_a_dangling_dep_is_visible_but_never_wedges_the_plan(crib):
    run(crib.plan_add("item", "x", project="p"))
    _set_deps(crib, "item", ["01GONEGONEGONEGONEGONEGONE"])
    row = crib.plan_list(project="p")["items"][0]
    assert row["missing_deps"] and not row["blocked"]


def test_plan_next_excludes_claimed_items_and_prescribes_the_loop(crib):
    run(crib.plan_add("one", "x", project="p"))
    run(crib.plan_add("two", "x", project="p"))
    run(crib.plan_status("one", "in-progress", project="p"))
    nxt = crib.plan_next(project="p")
    assert _titles(nxt["items"]) == ["two"]          # claimed items are taken
    assert nxt["claimed"] == 1
    assert "in-progress" in nxt["items"][0]["next"] and "done" in nxt["items"][0]["next"]


def test_completing_an_item_names_what_it_unblocked(crib):
    run(crib.plan_add("first", "x", project="p"))
    run(crib.plan_add("second", "x", deps=["first"], project="p"))
    run(crib.plan_add("third", "x", deps=["second"], project="p"))
    out = run(crib.plan_status("first", "done", project="p"))
    assert [u["title"] for u in out["unblocked"]] == ["second"]   # not "third"
    assert out["unblocked"][0]["ref"] == "second.md"
    # a status that isn't a completion doesn't claim to have unblocked anything
    assert run(crib.plan_status("second", "in-progress", project="p"))["unblocked"] == []


def test_plan_list_groups_the_working_set(crib):
    run(crib.plan_add("blocker", "x", project="p"))
    run(crib.plan_add("blocked one", "x", deps=["blocker"], project="p"))
    run(crib.plan_add("claimed", "x", project="p"))
    run(crib.plan_add("done one", "x", project="p"))
    run(crib.plan_status("claimed", "in-progress", project="p"))
    run(crib.plan_status("done one", "done", project="p"))

    listed = crib.plan_list(project="p")
    assert [r["group"] for r in listed["items"]] == ["in-progress", "ready", "blocked"]
    assert _titles(listed["items"]) == ["claimed", "blocker", "blocked one"]
    assert listed["groups"] == {"in-progress": 1, "ready": 1, "blocked": 1}
    blocked = listed["items"][-1]
    assert blocked["blocked_by"][0]["title"] == "blocker"
    assert blocked["blocked_by"][0]["status"] == "todo"      # named inline
    assert crib.plan_list(all=True, project="p")["items"][-1]["group"] == "done"


def test_plan_add_takes_a_batch_with_intra_batch_deps(crib):
    out = run(crib.plan_add(items=[
        {"title": "scaffold", "content": "x"},
        {"title": "wire it up", "deps": ["#1"]},
        {"title": "test it", "deps": ["#2"]}], project="p"))
    assert out["added"] == 3
    assert [r["title"] for r in out["items"]] == ["scaffold", "wire it up", "test it"]
    # they land contiguously, in order, with the batch deps resolved to real ids
    assert _by_rank(crib.plan_list(project="p")["items"]) == ["scaffold", "wire it up",
                                                             "test it"]
    assert _titles(crib.plan_next(project="p")["items"]) == ["scaffold"]
    wired = crib.plan_list(project="p")["items"][1]
    assert wired["deps"] == [out["items"][0]["id"]]


def test_a_batch_dep_can_only_point_backwards(crib):
    with pytest.raises(ValueError, match="not an EARLIER item"):
        run(crib.plan_add(items=[{"title": "a", "deps": ["#2"]},
                                 {"title": "b"}], project="p"))


def test_a_single_item_add_keeps_its_shape(crib):
    out = run(crib.plan_add("one", "body", project="p"))
    assert out["title"] == "one" and out["relpath"] == "one.md"
    assert out["added"] == 1 and out["items"][0]["id"] == out["id"]


# ── the backend is the shared store impl, in a sibling pillar ─────────────────

def test_store_reaches_chunk_metadata_and_scopes_retrieval(crib):
    """No cross-pollution in either direction: identical text stored as a note
    and as a decision, and each pillar's lookup sees only its own."""
    run(crib.design_add("Vector store", "chroma holds the vectors", project="p"))
    run(crib.store_note("chroma holds the vectors", title="Plain", project="p"))
    metas = crib.store.get_meta({"project": "p"}).values()
    assert {m.get("type") for m in metas} == {"design", ""}
    assert {m.get("store") for m in metas} == {"design", "notes"}

    note_hits = crib.lookup("chroma vectors", project="p", k=1)
    assert [h.store for h in note_hits] == ["notes"]     # even at k=1: no design
    assert all(h.relpath == "plain.md" for h in note_hits)
    design_hits = crib.lookup("chroma vectors", project="p", k=1, store="design")
    assert [(h.store, h.relpath) for h in design_hits] == [("design",
                                                            "vector-store.md")]
    # apropos rides lookup, so it inherits the exclusion
    assert all(h["store"] == "notes"
               for h in crib.apropos("chroma vectors", project="p"))


def test_note_verbs_refuse_facet_store_paths(crib):
    """The pre-split spelling `design/x.md` no longer names a note — the refusal
    points at the facet verbs instead of 404ing or recreating a legacy subtree."""
    run(crib.design_add("Base", "the ground", project="p"))
    with pytest.raises(ValueError, match="design_read"):
        crib.read_note("design/base.md", project="p")
    with pytest.raises(ValueError, match="own store"):
        run(crib.edit_note("design/base.md", "sneaky", project="p"))
    with pytest.raises(ValueError, match="plan"):
        run(crib.forget("plans/anything.md", project="p"))


# --- the facet graph: the symbol graph's consumer contract, for decisions/plans --

def test_design_graph_speaks_the_diagram_contract(crib):
    a = run(crib.design_add("Ground truth", "the base decision", project="p"))
    b = run(crib.design_add("Built on it", "rests on A", deps=[a["relpath"]],
                            project="p"))
    c = run(crib.design_add("Replacement", "supersedes A", project="p"))
    run(crib.design_supersede(a["relpath"], c["relpath"], project="p"))

    g = crib.design_graph(project="p")
    assert g["shape"] == "edges" and g["kind"] == "design"
    ids = {n["id"] for n in g["nodes"]}
    # ids are the pasteable pillar-qualified refs; every node carries name+status
    assert f"design:{b['relpath']}" in ids
    assert all(n.get("name") and n.get("id") for n in g["nodes"])
    # edges ⊆ nodes, and both kinds present
    assert all(e["from"] in ids and e["to"] in ids for e in g["edges"])
    kinds = {(e["kind"]) for e in g["edges"]}
    assert kinds == {"dep", "superseded_by"}
    # supersession points old → new
    assert {"from": f"design:{a['relpath']}", "to": f"design:{c['relpath']}",
            "kind": "superseded_by"} in g["edges"]
    # B's ground (A) was superseded → B reads tainted, live
    bn = next(n for n in g["nodes"] if n["id"] == f"design:{b['relpath']}")
    assert bn["tainted"] is True and bn["kind"] == "design"


def test_plan_graph_includes_the_decisions_items_rest_on(crib):
    d = run(crib.design_add("Keying decision", "keys are refs", project="p"))
    run(crib.plan_add(project="p", items=[
        {"title": "implement it", "deps": [d["relpath"]]},
        {"title": "unrelated chore"}]))
    g = crib.plan_graph(project="p")
    kinds = {n["kind"] for n in g["nodes"]}
    assert kinds == {"plan", "design"}          # the gating decision is IN the graph
    ids = {n["id"] for n in g["nodes"]}
    assert all(e["from"] in ids and e["to"] in ids for e in g["edges"])
    dep_edges = [e for e in g["edges"] if e["kind"] == "dep"]
    assert any(e["to"] == f"design:{d['relpath']}" for e in dep_edges)
    # …but only decisions items REST ON — not the whole decision map
    run(crib.design_add("Unrelated decision", "nothing plans on this", project="p"))
    g2 = crib.plan_graph(project="p")
    assert not any(n["name"] == "Unrelated decision" for n in g2["nodes"])


# --- the three asks from live facet use ------------------------------------------

def test_plan_reaffirm_clears_a_benign_taint_without_the_dep_dance(crib):
    """Plan items record the hash of each design dep as it read when the edge was
    made (`checked`); when the decision's body moves, the ITEM is tainted — the
    ⚠︎-stale flag lookups show — and there was no plan-side reaffirm: the only
    'fix' was dep_remove + dep_add per edge, a workaround wearing a verb costume.
    This IS reaffirm: 'I re-read the moved decision; this item still stands
    against it; re-record the baseline.'"""
    d = run(crib.design_add("Ground", "the decision", project="p"))
    run(crib.plan_add(project="p", title="build on it", deps=[d["relpath"]]))
    run(crib.design_append(d["relpath"], "the ground shifted", project="p"))

    designs = crib.designs
    proj_graph = designs._load_graph("p")
    item_id = crib.plan_list(project="p")["items"][0]["id"]
    assert item_id in designs._taint(proj_graph)          # the ⚠︎ the session hit

    item_rel = crib.plan_list(project="p")["items"][0]["relpath"]
    out = run(crib.plan_reaffirm(item_rel, project="p"))
    assert d["id"] in out["verified"] and not out["missing"]

    after = designs._load_graph("p")
    assert item_id not in designs._taint(after)           # baseline re-recorded
    assert after.nodes[item_id].checked[d["id"]] \
        == after.nodes[d["id"]].body_hash
    # …and status was NOT touched: a ground-claim, not a work-claim
    assert crib.plan_list(project="p")["items"][0]["status"] == "todo"


def test_design_append_adds_citations_post_hoc_keeping_old_hashes(crib, tmp_path):
    """The real sequence is node-first-doc-later: the decision exists, then the
    doc of record grows. `sources` on append ADDS the wire (deduped); the
    citation already recorded keeps its capture-time hash — that hash IS its
    meaning — while only the new section is hashed as it reads now."""
    docs = tmp_path / "proj"
    docs.mkdir(parents=True)
    (docs / ".crib").write_text("project: p\ndocs:\n  - '*.md'\n")
    doc = docs / "DESIGN.md"
    doc.write_text("# Spec\n\n## Early\nfirst section\n")
    run(crib.project_setup(cwd=docs))
    d = run(crib.design_add("Node first", "written before the doc grew",
                            project="p", sources=["DESIGN.md#Early"]))
    doc.write_text("# Spec\n\n## Early\nfirst section\n\n## Later\ngrown after\n")

    out = run(crib.design_append(d["relpath"], "now wired to the grown doc",
                                 project="p", sources=["DESIGN.md#Later"]))
    got = crib.design_read(d["relpath"], project="p")
    cites = {s["label"] if isinstance(s, dict) else str(s)
             for s in got.get("sources") or []}
    assert any("Later" in c for c in cites) and any("Early" in c for c in cites)
    # appending the SAME citation again is a no-op, not a duplicate
    again = run(crib.design_append(d["relpath"], "again", project="p",
                                   sources=["DESIGN.md#Later"]))
    got2 = crib.design_read(d["relpath"], project="p")
    assert len(got2.get("sources") or []) == len(got.get("sources") or [])


def test_the_wrong_facet_miss_names_the_right_verb(crib):
    """design_read on a ref that exists as a PLAN item used to answer 'no design
    note matches' — which reads as store corruption and cost a real session a
    real detour. The miss now says which aisle it is on."""
    run(crib.plan_add(project="p", title="a plan item"))
    rel = crib.plan_list(project="p")["items"][0]["relpath"]
    with pytest.raises(ValueError, match="PLAN item.*plan_"):
        crib.design_read(rel, project="p")
    d = run(crib.design_add("a decision", "body", project="p"))
    with pytest.raises(ValueError, match="DESIGN.*design_"):
        run(crib.plan_status(d["relpath"], "done", project="p"))
