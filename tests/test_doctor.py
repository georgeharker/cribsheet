"""`project_doctor` (crib.doctor): report-only health, each finding naming the
verb that fixes it."""

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


def _checks(out):
    return {f["check"] for f in out["findings"]}


def test_a_clean_project_is_healthy(crib):
    d = run(crib.design_add("Ground", "A decision.", project="p"))
    run(crib.plan_add("Do it", deps=[d["relpath"]], project="p"))
    out = crib.project_doctor("p")
    assert out["healthy"] and out["findings"] == []


def test_a_dep_on_a_superseded_decision_names_its_replacement(crib):
    old = run(crib.design_add("Old way", "Do it the old way.", project="p"))
    new = run(crib.design_add("New way", "Do it the new way.", project="p"))
    plan = run(crib.plan_add("Build it", deps=[old["relpath"]], project="p"))
    run(crib.design_supersede(old["relpath"], new["relpath"], project="p"))
    f = next(
        f
        for f in crib.project_doctor("p")["findings"]
        if f["check"] == "depends on a superseded design"
    )
    assert f["item"] == plan["relpath"] and f["replaced_by"] == new["relpath"]
    assert f"plan_dep_add {plan['relpath']} {new['relpath']}" in f["fix"]
    # a plan that is already done is history: not reported
    run(crib.plan_status(plan["relpath"], "done", project="p"))
    assert "depends on a superseded design" not in _checks(crib.project_doctor("p"))


def test_a_dangling_dep_and_a_declined_dep(crib):
    p1 = run(crib.plan_add("Never", project="p"))
    p2 = run(crib.plan_add("After", deps=[p1["relpath"]], project="p"))
    run(crib.plan_status(p1["relpath"], "declined", project="p"))
    assert "depends on a declined plan" in _checks(crib.project_doctor("p"))
    path = crib.designs._stores["plan"].root("p") / p2["relpath"]
    note = notes.load(path)
    note.frontmatter["deps"] = ["01NOSUCHTARGET0000000000000"]
    notes.save_atomic(note)
    f = next(
        f
        for f in crib.project_doctor("p")["findings"]
        if f["check"] == "dangling dependency"
    )
    assert f["dep"] == "01NOSUCHTARGET0000000000000" and "plan_dep_remove" in f["fix"]


def test_near_duplicates_and_doctor_changes_nothing(crib):
    body = "The relay owns session ordering; the fork never reorders frames. " * 5
    run(crib.design_add("Ordering", body, project="p"))
    run(crib.design_add("Ordering again", body, project="p"))
    run(
        crib.design_add(
            "Unrelated", "Colours come from the theme file only.", project="p"
        )
    )
    before = sorted(
        p.read_text() for p in crib.designs._stores["design"].root("p").glob("*.md")
    )
    out = crib.project_doctor("p")
    dups = [f for f in out["findings"] if f["check"] == "near-duplicate"]
    assert len(dups) == 1 and dups[0]["similarity"] >= 0.9
    assert "design_supersede" in dups[0]["fix"]
    after = sorted(
        p.read_text() for p in crib.designs._stores["design"].root("p").glob("*.md")
    )
    assert before == after  # report-only
