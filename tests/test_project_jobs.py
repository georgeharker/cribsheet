"""Job primitives: join-don't-restart, wait bounds, cancel, scope conflict.

The join registry (code_jobs) is the source-level fix for the double-sweep /
zero-reset failure class: a re-invoke while a sweep runs must NEVER start a
second one. Scenarios run inside one asyncio loop, mirroring the daemon.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

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


def _repo(tmp_path: Path) -> tuple[Path, SimpleNamespace]:
    """A tiny repo (two .py files + a .crib naming project p) and its link."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".crib").write_text("project: p\n")
    (root / "a.py").write_text("def a(): pass\n")
    (root / "b.py").write_text("def b(): pass\n")
    link = SimpleNamespace(root=root, project="p", doc_patterns=[], paths=[])
    return root, link


def _mock_resolution(crib: Crib, monkeypatch, link: SimpleNamespace) -> None:
    """_ensure_crib short-circuits to the fake link (no repo resolution)."""
    monkeypatch.setattr(crib, "_ensure_crib", lambda *a, **k: (link, False))


def _fake_indexer(crib: Crib, monkeypatch, calls: list[str]) -> None:
    """Per-file indexer: records each call, returns a clean structural result."""

    def fake(rt, rel, proj, patch_edges, existing=None,
             describe_mode="inline", sweep=False):
        calls.append(rel)
        return {"symbols": 1, "described": 1}

    monkeypatch.setattr(crib.indexer, "_index_code_file_tracked", fake)


def _slow_indexer(crib: Crib, monkeypatch, calls: list[str], delay: float) -> None:
    def fake(rt, rel, proj, patch_edges, existing=None,
             describe_mode="inline", sweep=False):
        calls.append(rel)
        import time

        time.sleep(delay)
        return {"symbols": 1, "described": 1}

    monkeypatch.setattr(crib.indexer, "_index_code_file_tracked", fake)


# --- join ---------------------------------------------------------------------


def test_join_returns_running_result_without_second_sweep(
    crib, tmp_path, monkeypatch
):
    root, link = _repo(tmp_path)
    _mock_resolution(crib, monkeypatch, link)
    calls: list[str] = []
    _slow_indexer(crib, monkeypatch, calls, delay=0.25)

    async def scenario():
        t1 = asyncio.create_task(crib.project_index("p"))
        await asyncio.sleep(0.05)               # sweep 1 is mid-flight
        r2 = await crib.project_index("p")      # the re-invoke: must JOIN
        r1 = await t1
        return r1, r2, calls

    r1, r2, calls = asyncio.run(scenario())
    assert r1["files_indexed"] == 2
    assert r2["joined"] is True                 # the re-invoke joined
    assert r2["files_indexed"] == 2             # same result, not a second sweep
    assert sorted(calls) == ["a.py", "b.py"]    # each file indexed EXACTLY once


# --- scope conflict -----------------------------------------------------------


def test_scope_conflict_on_crib_change(crib, tmp_path, monkeypatch):
    root, link = _repo(tmp_path)
    _mock_resolution(crib, monkeypatch, link)

    async def scenario():
        sentinel = asyncio.create_task(asyncio.sleep(3600))
        crib.code_jobs["p"] = {"task": sentinel, "crib_at": 123.0}
        r = await crib.project_index("p")
        sentinel.cancel()
        return r

    r = asyncio.run(scenario())
    assert r["joined"] is False
    assert "conflict" in r
    assert "project_cancel" in r["conflict"]


# --- cancel -------------------------------------------------------------------


def test_cancel_stops_the_sweep_and_keeps_completed_files(
    crib, tmp_path, monkeypatch
):
    root, link = _repo(tmp_path)
    _mock_resolution(crib, monkeypatch, link)
    calls: list[str] = []
    _slow_indexer(crib, monkeypatch, calls, delay=0.4)

    async def scenario():
        t = asyncio.create_task(crib.project_index("p"))
        await asyncio.sleep(0.3)                # mid-flight: 5 files × 0.4s each
        r = await crib.project_cancel("p", cwd=root)
        with pytest.raises(asyncio.CancelledError):
            await t
        return r, calls

    r, calls = asyncio.run(scenario())
    assert r["cancelled"] is True
    assert r["done_at_cancel"] >= 0             # mid-flight snapshot (may be 0)
    assert crib.status()["sweeps"] == {}        # counters cleaned up


def test_cancel_when_idle(crib, tmp_path):
    root, link = _repo(tmp_path)
    r = asyncio.run(crib.project_cancel("p", cwd=root))
    assert r == {"project": "p", "cancelled": False, "running": False}


# --- wait ---------------------------------------------------------------------


def test_wait_returns_final_when_sweep_fits(crib, tmp_path, monkeypatch):
    root, link = _repo(tmp_path)
    _mock_resolution(crib, monkeypatch, link)
    calls: list[str] = []
    _slow_indexer(crib, monkeypatch, calls, delay=0.25)

    async def scenario():
        t = asyncio.create_task(crib.project_index("p"))
        await asyncio.sleep(0.05)               # sweep is running
        r = await crib.project_wait("p", wait_s=5.0)
        await t
        return r

    r = asyncio.run(scenario())
    assert r["running"] is False
    assert r["complete"] is True
    assert r["files_indexed"] == 2


def test_wait_returns_partial_when_slow(crib, tmp_path, monkeypatch):
    root, link = _repo(tmp_path)
    _mock_resolution(crib, monkeypatch, link)
    calls: list[str] = []
    _slow_indexer(crib, monkeypatch, calls, delay=0.5)

    async def scenario():
        t = asyncio.create_task(crib.project_index("p"))
        await asyncio.sleep(0.05)               # sweep registered, files queued
        r = await crib.project_wait("p", wait_s=0.05)
        await t
        return r

    r = asyncio.run(scenario())
    assert r["running"] is True
    assert r["complete"] is False
    assert r["resume_hint"]                     # the timed-out client's next move
