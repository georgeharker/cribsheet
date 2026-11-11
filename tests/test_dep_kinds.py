"""Dependency kinds: follows / rests_on / informed_by.

Each kind gates differently and every kind taints: `follows` blocks until the
target is done, `rests_on` while it is stale or proposed, `informed_by` never.
With no kind written, the target decides (plan -> follows, decision ->
rests_on, note -> informed_by), so `deps:` keeps its meaning and only a
non-default choice is written under its kind's own key.
"""

from __future__ import annotations

import asyncio

import pytest

from crib import notes
from crib.app import Crib
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


def _fm(crib, pillar, relpath):
    return notes.load(crib.paths.project_dir("p") / pillar / relpath).frontmatter


def _blocked(crib, relpath):
    return {b["ref"] for b in crib.plan_read(relpath, project="p")["blocked_by"]}


def test_defaults_come_from_the_target_and_are_reported(crib):
    d = run(crib.design_add("Ground", "A decision.", project="p"))
    p1 = run(crib.plan_add("First", project="p"))
    p2 = run(crib.plan_add("Second", deps=[p1["relpath"], d["relpath"]], project="p"))
    assert p2["dep_kinds"] == {p1["id"]: "follows", d["id"]: "rests_on"}
    fm = _fm(crib, "plans", p2["relpath"])
    assert fm["deps"] == [p1["id"], d["id"]]  # defaults stay in `deps:`
    assert not any(k in fm for k in ("follows", "rests_on", "informed_by"))
    assert _blocked(crib, p2["relpath"]) == {p1["relpath"]}  # follows: not done


def test_an_explicit_kind_is_written_under_its_own_key(crib):
    p1 = run(crib.plan_add("Background", project="p"))
    p2 = run(crib.plan_add("Uses it", informed_by=[p1["relpath"]], project="p"))
    assert p2["dep_kinds"] == {p1["id"]: "informed_by"}
    fm = _fm(crib, "plans", p2["relpath"])
    assert fm["deps"] == [] and fm["informed_by"] == [p1["id"]]
    # informed_by never blocks, though p1 is not done
    assert _blocked(crib, p2["relpath"]) == set()


def test_informed_by_still_taints(crib):
    d1 = run(crib.design_add("Upstream", "The original.", project="p"))
    d2 = run(
        crib.design_add(
            "Downstream",
            "Built in light of it.",
            informed_by=[d1["relpath"]],
            project="p",
        )
    )
    out = run(crib.design_edit(d1["relpath"], "The original, changed.", project="p"))
    assert d2["id"] in {t["id"] for t in out["newly_tainted"]}


def test_dep_add_takes_a_kind_and_dep_remove_clears_its_key(crib):
    p1 = run(crib.plan_add("Before", project="p"))
    p2 = run(crib.plan_add("After", project="p"))
    out = run(
        crib.plan_dep_add(p2["relpath"], p1["relpath"], project="p", kind="informed_by")
    )
    assert out["dep_kind"] == "informed_by"
    assert _fm(crib, "plans", p2["relpath"])["informed_by"] == [p1["id"]]
    run(crib.plan_dep_remove(p2["relpath"], p1["relpath"], project="p"))
    fm = _fm(crib, "plans", p2["relpath"])
    assert "informed_by" not in fm and fm["deps"] == []


def test_dep_add_without_a_kind_uses_the_default(crib):
    d = run(crib.design_add("Ground", "A decision.", project="p"))
    p = run(crib.plan_add("Work", project="p"))
    out = run(crib.plan_dep_add(p["relpath"], d["relpath"], project="p"))
    assert out["dep_kind"] == "rests_on"
    assert _fm(crib, "plans", p["relpath"])["deps"] == [d["id"]]


def test_an_unknown_kind_is_refused(crib):
    p1 = run(crib.plan_add("A", project="p"))
    p2 = run(crib.plan_add("B", project="p"))
    from crib.errors import CribUserError

    with pytest.raises(CribUserError, match="unknown dependency kind"):
        run(crib.plan_dep_add(p2["relpath"], p1["relpath"], project="p", kind="after"))


def test_a_batch_item_can_carry_a_kind_by_position(crib):
    out = run(
        crib.plan_add(
            items=[
                {"title": "One"},
                {"title": "Two", "informed_by": ["#1"]},
            ],
            project="p",
        )
    )
    one, two = out["items"]
    assert two["dep_kinds"] == {one["id"]: "informed_by"}


def test_the_cli_spells_the_kinds(crib):
    from crib.cli import _dispatch, build_parser

    _, call = _dispatch(
        build_parser().parse_args(
            ["plan", "add", "T", "--follows", "a", "--informed-by", "b", "-p", "p"]
        )
    )
    assert call["follows"] == ["a"] and call["informed_by"] == ["b"]
    _, call = _dispatch(
        build_parser().parse_args(
            ["plan", "dep-add", "x", "y", "--as", "rests_on", "-p", "p"]
        )
    )
    assert call["kind"] == "rests_on"


def test_a_note_can_be_named_as_a_dependency_and_is_checked_never_gating(crib):
    n = run(crib.store_note("Background reading.", title="Background", project="p"))
    p = run(crib.plan_add("Work", deps=[n["relpath"]], project="p"))
    (note_id,) = p["dep_kinds"]
    assert p["dep_kinds"] == {note_id: "informed_by"}  # a note's default kind
    assert _blocked(crib, p["relpath"]) == set()
    # born verified: not stale until the note changes
    assert not crib.plan_read(p["relpath"], project="p")["tainted"]
    run(crib.edit_note(n["relpath"], "Background reading, revised.", project="p"))
    assert crib.plan_read(p["relpath"], project="p")["tainted"]
    assert _blocked(crib, p["relpath"]) == set()  # stale, but never gates


def test_a_graph_read_brings_the_store_in_line_with_files_edited_outside_crib(crib):
    """The read-and-execute sweep: a hand edit (no watcher here) reaches the item
    rows and edges on the next graph read, and a hand-deleted file's row goes."""
    d = run(crib.design_add("Ground", "A decision.", project="p"))
    p1 = run(crib.plan_add("First", project="p"))
    p2 = run(crib.plan_add("Second", project="p"))
    path = crib.paths.project_dir("p") / "plans" / p2["relpath"]
    note = notes.load(path)
    note.frontmatter["deps"] = [p1["id"]]
    note.frontmatter["rests_on"] = [d["id"]]
    note.frontmatter["status"] = "in-progress"
    notes.save_atomic(note)  # behind crib's back
    (crib.paths.project_dir("p") / "plans" / p1["relpath"]).unlink()
    crib.plan_list(project="p")  # any graph read
    item = crib.store.items({"id": p2["id"]})[p2["id"]]
    assert item.facet["status"] == "in-progress"
    assert {(dst, k) for _s, dst, k, _h in crib.store.edges({"src": p2["id"]})} == {
        (p1["id"], "informed_by"),  # target gone: no kind to default from
        (d["id"], "rests_on"),
    }
    assert crib.store.items({"id": p1["id"]}) == {}
