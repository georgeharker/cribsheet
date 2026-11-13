"""Write-time warnings (`similar`, Stage E4): a just-written note, decision or
plan item compared on its stored title and body vectors against those three
kinds, each match with a verdict and the verb that answers it."""

from __future__ import annotations

import asyncio

import pytest

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


BODY = "The relay orders every frame; forks never reorder what they forward."


def test_the_same_body_again_is_a_duplicate_across_kinds(crib):
    d = run(crib.design_add("Relay ordering", BODY, project="p"))
    out = run(crib.store_note(BODY, title="relay note", project="p"))
    hit = next(r for r in out["similar"] if r["relpath"] == d["relpath"])
    assert hit["kind"] == "design" and hit["verdict"] == "duplicate"
    assert "design_append / design_edit" in hit["next"]


def test_alike_titles_over_different_bodies_are_confusable(crib):
    first = run(crib.design_add("Session adoption", BODY, project="p"))
    out = run(
        crib.design_add(
            "Session adoption",
            "Colours come from the theme file and nothing else.",
            project="p",
        )
    )
    hit = next(r for r in out["similar"] if r["relpath"] == first["relpath"])
    assert hit["verdict"] == "confusable" and "retitle" in hit["next"]


def test_a_twin_of_finished_work_is_settled(crib):
    p1 = run(crib.plan_add("Build the relay", BODY, project="p"))
    run(crib.plan_status(p1["relpath"], "done", project="p"))
    again = run(crib.plan_add("Build relay ordering", BODY, project="p"))
    hit = next(r for r in again["similar"] if r["relpath"] == p1["relpath"])
    assert hit["verdict"] == "settled" and hit["status"] == "done"
    assert hit["verdict"] == again["similar"][0]["verdict"]  # settled ranks first


def test_unrelated_writes_answer_nothing_and_the_item_is_never_its_own_match(crib):
    run(crib.design_add("Relay ordering", BODY, project="p"))
    out = run(
        crib.store_note(
            "Colours come from the theme file and nothing else.",
            title="theme colours",
            project="p",
        )
    )
    assert out["similar"] == []
    first = run(crib.store_note(BODY, title="once", project="p"))
    assert all(r["relpath"] != first["relpath"] for r in first["similar"])
