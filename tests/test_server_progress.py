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


def test_progress_streams_on_the_join_face(crib, monkeypatch):
    """Progress was streamed in-call on project_index — now the SWEEP runs
    daemon-owned and index calls return handles; the streaming lives on the
    JOIN primitive (project_wait), the one call that legitimately holds the
    client while waiting: report_progress ticks (only THIS call's sweep — not
    summing every project's: a concurrent index of another project must not
    bleed into the numbers)."""
    from crib import server as server_mod

    monkeypatch.setattr(server_mod, "_PROGRESS_EVERY_S", 0.05)
    # the STREAM loop lives in the app (server imports app, never the reverse)
    monkeypatch.setattr("crib.app._PROGRESS_EVERY_S", 0.05)
    crib.code.sweeps["other"] = {"done": 90, "total": 100}  # someone else's sweep

    async def fake_index(project=None, cwd=None, budget_s=None, wait_s=None):
        # a real sweep: registers its code_jobs entry (what project_wait joins)
        # and (the polling contract) returns a handle at once unless blocke�
        async def sweep():
            crib.code.sweeps[project] = {"done": 3, "total": 9}
            # Run until the join has streamed a tick (not a fixed 0.2 s: under a
            # loaded, parallel test run no tick may land in that window).
            for _ in range(200):
                if seen:
                    break
                await asyncio.sleep(0.05)
            crib.code.sweeps.pop(project, None)
            return {"project": project, "created": False, "complete": True}

        task = asyncio.ensure_future(sweep())
        crib.code_jobs[project] = {"task": task, "crib_at": None}
        if wait_s is None:
            return {"project": project, "started": True, "running": True,
                    "resume_hint": "poll project_status"}
        return await task

    monkeypatch.setattr(crib, "project_index", fake_index)
    mcp = server_mod.build_server(crib)
    seen: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    async def body():
        async with Client(mcp) as c:
            await c.call_tool("project_index", {"project": "p"})  # KICK: handle
            await c.call_tool("project_wait", {"project": "p", "wait_s": 5.0},
                              progress_handler=on_progress)  # JOIN: streams

    asyncio.run(body())
    assert seen                                # progress was streamed
    assert all(t == 9 for _, t, _m in seen)    # ours (9 files) — not ours+other (109)
