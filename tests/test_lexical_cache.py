"""The warm BM25 cache: reused across queries, invalidated on write."""

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


def test_cache_is_reused_until_a_write(crib):
    run(crib.store_note("restart the backing server now", title="ops", project="p"))
    crib.lookup("restart server", project="p")           # builds the cache
    cache = crib.index.lexical
    first = cache.get("p")
    assert cache.get("p") is first                        # same object, no rebuild


def test_write_invalidates_then_rebuilds(crib):
    run(crib.store_note("alpha widget config", title="a", project="p"))
    crib.lookup("widget", project="p")                    # warm the cache
    before = crib.index.lexical.get("p")

    run(crib.store_note("beta gadget reference", title="b", project="p"))
    after = crib.index.lexical.get("p")
    assert after is not before                            # invalidated + rebuilt
    # the freshly written chunk is now lexically findable
    ids, docs, _ = after
    assert any("gadget" in docs[i][0] for i in ids)


def test_cache_is_per_project(crib):
    run(crib.store_note("project one note", title="x", project="one"))
    run(crib.store_note("project two note", title="y", project="two"))
    crib.lookup("note", project="one")
    crib.lookup("note", project="two")
    cache = crib.index.lexical
    one_ids = cache.get("one")[0]
    two_ids = cache.get("two")[0]
    assert one_ids and two_ids and set(one_ids).isdisjoint(two_ids)


def test_corpora_are_per_pillar_store(crib):
    # A term unique to a design-store chunk must not exist in the notes-pillar
    # BM25 corpus at all — ranking isolation, not just result filtering.
    run(crib.store_note("plain zebra note body", title="n", project="p"))
    nd = crib.notestore.dir("p")
    (nd / "quagga.md").write_text("quagga decision body\n")
    run(crib.index.index_file("p", nd, "quagga.md", store="design"))
    cache = crib.index.lexical
    notes_ids, notes_docs, _ = cache.get("p")
    design_ids, design_docs, _ = cache.get("p", store="design")
    assert notes_ids and design_ids
    assert set(notes_ids).isdisjoint(design_ids)
    assert not any("quagga" in notes_docs[i][0] for i in notes_ids)
    assert not any("zebra" in design_docs[i][0] for i in design_ids)


def test_chunks_without_a_store_key_count_as_notes(crib):
    # Pre-split chunks lack the `store` metadata key; the absence rule keeps
    # them visible to the notes pillar until the sweep re-stamps them.
    run(crib.store_note("gamma rhino body", title="g", project="p"))
    metas = crib.store.get_meta({"project": "p"})
    crib.store.set_meta({cid: {k: v for k, v in m.items() if k != "store"}
                         for cid, m in metas.items()})
    crib.index.invalidate_caches("p")
    ids, docs, _ = crib.index.lexical.get("p")
    assert any("rhino" in docs[i][0] for i in ids)
    assert crib.index.lexical.get("p", store="design")[0] == []


def test_a_store_wipe_invalidates_every_projects_cache(crib):
    # A dim change recreates the collection — every project's chunks go, not just
    # the reindexed one's. The derived caches are built FROM the store, so a warm
    # daemon kept serving BM25 hits for chunks that no longer exist (ids the dense
    # side can't even score).
    run(crib.store_note("alpha widget config", title="a", project="p"))
    run(crib.store_note("beta gadget reference", title="b", project="q"))
    for proj in ("p", "q"):
        crib.lookup("widget gadget", project=proj)        # warm both
    assert crib.index.lexical.get("p")[0] and crib.index.lexical.get("q")[0]

    crib.store.recreate()
    crib.index.invalidate_all_caches()
    assert crib.index.lexical.get("p")[0] == []           # rebuilt from an empty store
    assert crib.index.lexical.get("q")[0] == []
