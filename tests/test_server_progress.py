"""MCP `project_index` wrapper — the progress loop must not tax quick calls, must
scope progress to THIS call's sweep, and must pass `budget_s` through.

Uses fastmcp's in-memory client against `build_server(crib)` with a faked
`Crib.project_index`, so no LSP/LLM/model work runs.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastmcp import Client

from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.server import build_server
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def test_quick_index_returns_without_interval_lag(crib, monkeypatch):
    """A fast (all-cached) reindex must return immediately — the old loop slept a
    full `_PROGRESS_EVERY_S` interval even when the task finished in milliseconds."""
    calls: dict = {}

    async def fake_index(project=None, cwd=None, budget_s=None):
        calls["budget_s"] = budget_s
        return {"project": project, "created": False, "complete": True}

    monkeypatch.setattr(crib, "project_index", fake_index)
    mcp = build_server(crib)

    async def body():
        async with Client(mcp) as c:
            t0 = time.monotonic()
            res = await c.call_tool("project_index", {"project": "p", "budget_s": 7.5})
            return time.monotonic() - t0, res

    elapsed, res = asyncio.run(body())
    assert elapsed < 1.0                       # well under the 2s progress interval
    assert calls["budget_s"] == 7.5            # budget reaches Crib.project_index


def test_progress_reports_only_this_calls_sweep(crib, monkeypatch):
    """Progress must read THIS call's sweep — not sum every project's (a concurrent
    index of another project used to bleed into the numbers)."""
    from crib import server as server_mod

    monkeypatch.setattr(server_mod, "_PROGRESS_EVERY_S", 0.05)
    crib.code.sweeps["other"] = {"done": 90, "total": 100}   # someone else's sweep

    async def fake_index(project=None, cwd=None, budget_s=None):
        crib.code.sweeps[project] = {"done": 3, "total": 9}
        await asyncio.sleep(0.2)               # a few progress ticks fire meanwhile
        crib.code.sweeps.pop(project, None)
        return {"project": project, "created": False, "complete": True}

    monkeypatch.setattr(crib, "project_index", fake_index)
    mcp = server_mod.build_server(crib)
    seen: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    async def body():
        async with Client(mcp) as c:
            await c.call_tool("project_index", {"project": "p"},
                              progress_handler=on_progress)

    asyncio.run(body())
    assert seen                                # progress was streamed
    assert all(t == 9 for _, t, _m in seen)    # ours (9 files) — not ours+other (109)
