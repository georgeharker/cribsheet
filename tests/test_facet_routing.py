"""Crib's note methods route to the facet they're given (facet registry step 2a).

Notes stay the default, so every existing caller is unchanged; a `facet=` names
another facet's store. A built-in facet stands in here for the declared ones
that come later (tests/test_declared_facets.py exercises a real new facet).
"""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
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


def run(coro):
    return asyncio.run(coro)


def test_writes_reads_and_history_land_in_the_named_facets_store(crib):
    f = "learning"
    res = run(crib.store_note("first body", "Routing probe", "p", facet=f))
    rel = res["relpath"]
    path = crib.pillars["learnings"].root("p") / rel
    assert path.exists()
    assert not (crib.pillars["notes"].root("p") / rel).exists()
    assert crib.locate(rel, "p", facet=f) == str(path)
    assert "first body" in crib.read_note(rel, "p", facet=f)
    with pytest.raises((CribUserError, FileNotFoundError, OSError)):
        crib.read_note(rel, "p")  # not a note

    run(crib.append_note(rel, "more", "Extra", "p", facet=f))
    run(crib.edit_note(rel, "rewritten", "p", facet=f))
    assert "rewritten" in crib.read_note(rel, "p", facet=f)
    versions = crib.list_versions(rel, "p", facet=f)
    assert len(versions) >= 2
    run(crib.restore(rel, versions[-1]["version"], "p", facet=f))
    assert "first body" in crib.read_note(rel, "p", facet=f)

    moved = run(crib.move_note(rel, "q", "moved.md", "p", facet=f))
    assert moved["to"] == {"project": "q", "relpath": "moved.md"}
    assert (crib.pillars["learnings"].root("q") / "moved.md").exists()
    run(crib.forget("moved.md", "q", facet=f))
    assert not (crib.pillars["learnings"].root("q") / "moved.md").exists()


def test_import_and_reindex_use_the_facets_store(crib, tmp_path):
    src = tmp_path / "src.md"
    src.write_text("imported body\n")
    run(crib.import_files([str(src)], "p", facet="learning"))
    assert (crib.pillars["learnings"].root("p") / "imported" / "src.md").exists()
    r = run(crib.reindex(None, "p", facet="learning"))
    assert r["files"] >= 1


def test_unknown_facet_is_refused(crib):
    with pytest.raises(KeyError, match="no facet"):
        crib.read_note("x.md", "p", facet="routine")
