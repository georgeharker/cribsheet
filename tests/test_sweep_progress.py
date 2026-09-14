"""Live-sweep progress: the shared `format_sweep` renderer + the `started` stamp.

The line shape is a CONTRACT between three surfaces — the CLI ticker (both the
daemon progress feed and the in-process watcher) and the MCP progress message —
so they never drift apart. These tests pin the shape.
"""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.codestore import format_sweep
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def test_counts_only_without_started():
    """Older sweep entries (and test fixtures) may carry no `started` — the line
    degrades to counts, never errors."""
    assert format_sweep({"done": 3, "total": 10}) == "3/10 files (30%)"


def test_rate_and_eta_once_warm():
    sw = {"done": 20, "total": 100, "started": 1000.0}
    assert format_sweep(sw, now=1010.0) == "20/100 files (20%) · 2.0/s · eta 0:40"


def test_no_rate_before_warmup():
    """Under ~2s elapsed the rate is startup noise — suppressed, not shown."""
    sw = {"done": 5, "total": 100, "started": 1000.0}
    assert format_sweep(sw, now=1001.0) == "5/100 files (5%)"


def test_no_rate_before_first_file():
    """done=0 divides by zero if unguarded — the line stays counts-only."""
    sw = {"done": 0, "total": 100, "started": 1000.0}
    assert format_sweep(sw, now=1005.0) == "0/100 files (0%)"


def test_zero_total_is_empty():
    assert format_sweep({"done": 0, "total": 0}) == ""


def test_eta_floors_never_rounds_up():
    """A 99.9s eta reads 1:39, not 1:40 — floor-then-format."""
    sw = {"done": 1, "total": 11, "started": 0.0}  # 10 left, 1/9.99 per s
    assert "eta 1:39" in format_sweep(sw, now=9.99)


def test_sweep_registration_stamps_started(crib, tmp_path, monkeypatch):
    """`_index_project_code` stamps `started` at registration so every consumer
    (ticker, MCP message) can compute elapsed/rate/eta from the entry itself."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def a(): pass\n")
    (root / "b.py").write_text("def b(): pass\n")
    seen: list[dict] = []

    def fake(
        rt, rel, proj, patch_edges, existing=None, describe_mode="inline", sweep=False
    ):
        seen.append(dict(crib.status()["sweeps"].get("p", {})))
        return {"symbols": 1, "described": 1}

    monkeypatch.setattr(crib.indexer, "_index_code_file_tracked", fake)
    out = asyncio.run(crib._index_project_code("p", root, ["**/*.py"]))
    assert out["files_seen"] == 2
    assert seen and all(s.get("started") for s in seen)
    assert crib.status()["sweeps"] == {}  # gone when finished, as before
