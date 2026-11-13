"""`project_related` (crib.related): one item, its links both ways with their
kinds, and its nearest items of every kind."""

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


def test_links_both_ways_with_their_kinds(crib):
    d = run(
        crib.design_add("Relay owns ordering", "The relay orders frames.", project="p")
    )
    p1 = run(crib.plan_add("Build the relay", deps=[d["relpath"]], project="p"))
    p2 = run(crib.plan_add("Then test it", deps=[p1["relpath"]], project="p"))
    out = crib.project_related(p1["relpath"], project="p")
    assert out["item"]["title"] == "Build the relay"
    assert {(r["link"], r["title"]) for r in out["links_out"]} == {
        ("rests_on", "Relay owns ordering")
    }
    assert {(r["link"], r["title"], r["status"]) for r in out["links_in"]} == {
        ("follows", "Then test it", "todo")
    }
    # by title, by id: the same item
    assert (
        crib.project_related("Build the relay", project="p")["item"]["id"] == p1["id"]
    )
    assert (
        crib.project_related(p2["id"], project="p")["item"]["relpath"] == p2["relpath"]
    )


def test_nearest_items_of_every_kind_exclude_the_item_itself(crib):
    body = "Sessions are adopted once per chat and the adoption is announced."
    d = run(crib.design_add("Adopt sessions once", body, project="p"))
    run(
        crib.plan_add(
            "Implement session adoption", body + " Implement it.", project="p"
        )
    )
    run(crib.store_note(body + " A note about it.", title="adoption", project="p"))
    out = crib.project_related(d["relpath"], project="p", k=2)
    assert set(out["similar"]) >= {"note", "plan"}
    assert all(r["id"] != d["id"] for rows in out["similar"].values() for r in rows)
    assert out["similar"]["plan"][0]["title"] == "Implement session adoption"


def test_an_unknown_ref_says_what_it_accepts(crib):
    with pytest.raises(CribUserError, match="give an id, a relpath"):
        crib.project_related("nothing-like-this", project="p")
