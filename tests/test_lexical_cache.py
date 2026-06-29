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
