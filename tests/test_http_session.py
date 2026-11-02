"""Sticky session project across REAL streamable-HTTP calls (DESIGN §15).

The per-connection session state is keyed on the per-client mcp `Connection`
(``session._connection``), because MCP SDK v2 constructs a fresh `ServerSession`
for EVERY inbound request — keying on it would forget an adopted project between
calls, silently: no error, no type error, just every selector-less call resolving
as `seed`/`unanchored` and elicited adoptions evaporating. Only an e2e round-trip
over HTTP with one client session spanning several calls can see it, so this file
exists for that; it pins the fastmcp 4 / mcp 2 era contract end to end.

Three halves:
  1. ADOPTION PERSISTS — `project_use` then selector-less reads on the SAME
     connection stay `alpha` with `resolved_via: "session"`. On the ServerSession
     keying (the bug) call 2 answers `default` here.
  2. ELICITED ADOPTION PERSISTS — the unanchored read → elicit → adopt path also
     sticks for the next selector-less call.
  3. NO LEAK BETWEEN CONNECTIONS — a fresh connection must not see session 1's
     pointer.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import pytest

from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.server import build_server
from crib.store import InMemoryStore


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


@pytest.fixture()
def http_url(crib):
    """A REAL daemon: the FastMCP app on streamable HTTP via uvicorn, on an
    ephemeral port. The in-memory transport reuses one session object, so the
    ServerSession-per-request failure mode cannot reproduce there — the failure
    is only visible over a real client session spanning several calls."""
    import uvicorn

    app = build_server(crib).http_app(transport="streamable-http")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 20.0
    while not server.started:
        if time.monotonic() > deadline or not t.is_alive():
            server.should_exit = True
            t.join(timeout=5)
            raise RuntimeError("uvicorn did not start for the HTTP session test")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    t.join(timeout=5)


def _data(res: Any) -> Any:
    return getattr(res, "data", res)


def test_adopted_project_persists_across_http_calls(crib, http_url):
    """`project_use` then selector-less `project_current` twice on the SAME
    connection: `alpha`, with `resolved_via: "session"`. On the per-request keying
    (fresh ServerSession each call) the second read answers `default`/`seed` —
    the exact silent regression the brief warned that no type check would ever
    surface."""
    from fastmcp import Client

    async def session():
        async with Client(http_url, mode="legacy") as c:
            await c.call_tool("project_use", {"project": "alpha"})
            one = await c.call_tool("project_current", {})
            two = await c.call_tool("project_current", {})
            return _data(one), _data(two)

    one, two = asyncio_run(session())
    for r in (one, two):
        assert r.get("current_project") == "alpha"
        assert r.get("resolved_via") == "session"  # ← sticky, not re-seeded


def test_elicited_adopt_persists_across_http_calls(crib, http_url):
    """The elicit → adopt path too, exercised on an ASYNC tool (sync tools refuse
    to elicit by design — see `_wire.run_sync`). An unanchored READ (`note_reindex`
    with no selector) ELICITS the project; the named one is adopted as the
    session's, so the next selector-less `project_current` must resolve
    `session`, not `seed`/`default` — on the bug, the elicited adoption
    evaporates between the two calls."""
    from fastmcp import Client

    # the elicited NAME must be a REAL project: create 'alpha' up front (via the
    # Crib API — the fixture process has no MCP session, so this touches only the
    # project tree, never any connection's session state)
    import asyncio as _asyncio

    _asyncio.run(crib.project_create("alpha"))

    async def accept_alpha(message, response_type, params, ctx):  # noqa: ANN001
        return "alpha"

    async def session():
        async with Client(
            http_url, mode="legacy", elicitation_handler=accept_alpha
        ) as c:
            # an unanchored read → the server ELICITS (legacy era carries the
            # back-channel) → the human names the project → session adopts
            await c.call_tool("note_reindex", {})
            two = await c.call_tool("project_current", {})
            return _data(two)

    cur = asyncio_run(session())
    assert cur.get("current_project") == "alpha"
    assert cur.get("resolved_via") == "session"


def test_sessions_do_not_leak_between_connections(crib, http_url):
    """A second connection must not see session 1's pointer."""
    from fastmcp import Client

    async def session():
        async with Client(http_url, mode="legacy") as c:
            await c.call_tool("project_use", {"project": "alpha"})

    async def second_session():
        async with Client(http_url, mode="legacy") as c:
            return _data(await c.call_tool("project_current", {}))

    asyncio_run(session())
    fresh = asyncio_run(second_session())
    assert fresh.get("current_project") != "alpha"
    assert fresh.get("resolved_via") != "session"
