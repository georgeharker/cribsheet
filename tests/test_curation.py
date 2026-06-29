"""Predictable relpaths, created flags, and move/reproject (feedback fixes)."""

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


def test_relpath_is_predictable_then_collision_suffixed(crib):
    a = run(crib.store_note("body one", title="Setup Notes", project="p"))
    assert a["relpath"] == "setup-notes.md"             # no random tail
    b = run(crib.store_note("body two", title="Setup Notes", project="p"))
    assert b["relpath"] == "setup-notes-2.md"           # suffix only on collision


def test_store_reports_created_for_new_project(crib):
    first = run(crib.store_note("x", title="t", project="fresh"))
    assert first["created"] is True
    second = run(crib.store_note("y", title="u", project="fresh"))
    assert second["created"] is False                   # project already existed


def test_move_across_projects_preserves_id(crib):
    out = run(crib.store_note("movable body", title="Movable", project="src"))
    from crib import notes
    id_before = notes.load(crib.abspath("src", out["relpath"])).id

    res = run(crib.move_note(out["relpath"], to_project="dst", project="src"))
    assert res["to"]["project"] == "dst" and res["created"] is True
    assert not crib.abspath("src", out["relpath"]).exists()      # source gone
    moved = crib.abspath("dst", out["relpath"])
    assert moved.exists()
    assert notes.load(moved).id == id_before                     # identity preserved
    # searchable in the new project, gone from the old
    assert any(h.relpath == out["relpath"] for h in crib.lookup("movable", project="dst"))
    assert not crib.lookup("movable", project="src")


def test_move_rejects_clobber_and_noop(crib):
    a = run(crib.store_note("aaa", title="A", project="p"))
    b = run(crib.store_note("bbb", title="B", project="p"))
    with pytest.raises(ValueError):                     # same src/dst
        run(crib.move_note(a["relpath"], project="p"))
    with pytest.raises(ValueError):                     # destination exists
        run(crib.move_note(a["relpath"], to_relpath=b["relpath"], project="p"))


def test_similar_field_present_and_excludes_self(crib):
    res = run(crib.store_note("alpha beta gamma", title="First", project="p"))
    # the just-written note must never appear in its own similar list
    assert all(s["relpath"] != res["relpath"] for s in res["similar"])
