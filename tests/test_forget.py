"""`forget` removes a note from disk + index, but keeps it recoverable (§5, §8)."""

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
    paths = Paths.resolve().ensure()
    return Crib(paths, Config(), InMemoryStore())


def run(coro):
    return asyncio.run(coro)


def test_forget_removes_file_and_index_but_stashes_version(crib):
    out = run(crib.store_note("secret plans about the moon base", title="m",
                              project="p"))
    rel = out["relpath"]
    note_id = notes.load(crib.abspath("p", rel)).id

    assert crib.lookup("moon base", project="p")          # present before

    res = run(crib.forget(rel, project="p"))
    assert res["removed"] >= 1
    assert res["recoverable_id"] == note_id

    assert not crib.abspath("p", rel).exists()            # gone from disk
    assert not crib.lookup("moon base", project="p")      # gone from index
    # but recoverable: the version ring still holds the content by id
    versions = crib.versions.list(note_id)
    assert versions and "moon base" in crib.versions.read(note_id, versions[0].name)


def test_forget_missing_note_is_noop(crib):
    res = run(crib.forget("nope.md", project="p"))
    assert res["removed"] == 0 and res["recoverable_id"] is None
