"""Startup reconcile catches changes made while the watcher was down (DESIGN §4)."""

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
    paths = Paths.resolve().ensure()
    return Crib(paths, Config(), InMemoryStore())


def run(coro):
    return asyncio.run(coro)


def test_reconcile_picks_up_offline_edit_and_delete(crib):
    a = run(crib.store_note("alpha content about turbines", title="a", project="p"))
    b = run(crib.store_note("beta content about gardening", title="b", project="p"))
    nd = crib.notes_dir("p")

    # Simulate edits made while crib was DOWN (no watcher, no tools):
    # 1) rewrite note a's body directly on disk
    pa = nd / a["relpath"]
    pa.write_text(pa.read_text().replace("turbines", "helicopters"))
    # 2) delete note b off disk entirely
    (nd / b["relpath"]).unlink()

    # Before reconcile: stale index still matches old content / orphan present.
    assert crib.lookup("turbines", project="p")          # stale hit
    assert crib.lookup("gardening", project="p")          # orphan hit

    rec = run(crib.reconcile_all())
    assert rec["changed"] >= 1 and rec["removed"] >= 1

    # After: new content searchable, stale + orphaned content gone.
    assert crib.lookup("helicopters", project="p")
    assert not crib.lookup("turbines", project="p")
    assert not crib.lookup("gardening", project="p")


def test_reconcile_is_noop_when_nothing_changed(crib):
    run(crib.store_note("stable content", title="s", project="p"))
    rec = run(crib.reconcile_all())
    assert rec["changed"] == 0 and rec["removed"] == 0
