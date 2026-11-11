"""The indexer keeps one item row per indexed file, in step with its chunks."""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore


@pytest.fixture(params=["memory", "duckdb"])
def crib(request, tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    paths = Paths.resolve().ensure()
    if request.param == "duckdb":
        from crib.store_duckdb import DuckDBStore

        store = DuckDBStore.embedded(paths.duckdb_file)
        request.addfinalizer(store.close)
    else:
        store = InMemoryStore()
    return Crib(paths, Config(), store)


def run(coro):
    return asyncio.run(coro)


def test_a_stored_note_gets_an_item_with_both_vectors(crib):
    out = run(
        crib.store_note(
            "Widgets are keyed by serial.", title="Widgets", project="p", tags=["alpha"]
        )
    )
    items = crib.store.items({"project": "p"})
    (item,) = items.values()
    assert item.relpath == out["relpath"]
    assert item.kind == "note" and item.title == "Widgets"
    assert item.facet["tags"] == ["alpha"]
    assert item.title_vec and item.body_vec and item.title_vec != item.body_vec


def test_facets_land_in_their_own_kind(crib):
    run(crib.design_add("Serial keys", "Widgets are keyed by serial.", project="p"))
    run(crib.plan_add("Index by serial", "Build the index.", project="p"))
    kinds = {i.kind: i for i in crib.store.items({"project": "p"}).values()}
    assert kinds["design"].facet["status"] == "active"
    assert kinds["plan"].facet["status"] == "todo" and kinds["plan"].facet["rank"]


def test_an_unchanged_body_keeps_its_vectors_and_a_status_change_lands(crib):
    out = run(crib.plan_add("Index by serial", "Build the index.", project="p"))
    before = crib.store.items({"id": out["id"]})[out["id"]]
    run(crib.plan_status(out["relpath"], "in-progress", project="p"))
    after = crib.store.items({"id": out["id"]})[out["id"]]
    assert after.facet["status"] == "in-progress"
    assert after.title_vec == before.title_vec and after.body_vec == before.body_vec


def test_forgetting_a_note_drops_its_item(crib):
    out = run(crib.store_note("Gone soon.", title="Temp", project="p"))
    run(crib.forget(out["relpath"], project="p"))
    assert crib.store.items({"project": "p"}) == {}


def test_a_full_reindex_fills_items_for_notes_indexed_before_items_existed(crib):
    run(crib.store_note("Older note.", title="Old", project="p"))
    (item_id,) = crib.store.items({"project": "p"})
    crib.store.apply([], [], {}, None, [item_id])  # as if indexed by 0.22
    assert crib.store.items({"project": "p"}) == {}
    run(crib.reindex(project="p"))
    assert set(crib.store.items({"project": "p"})) == {item_id}
