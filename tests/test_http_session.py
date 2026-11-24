"""Per-call projects across REAL streamable-HTTP calls, on both protocol eras.

There is no per-connection current project (design: "Retire use_project and
implicit anchoring (all eras)"): every call names its project, or is ASKED for
it, and the answer is for that call only. How crib asks depends on the era:

  1. HANDSHAKE (what the daemon advertises): a server-initiated
     `elicitation/create` on the same connection; a client without an
     elicitation handler gets the actionable refusal.
  2. 2026-07-28 (a client that speaks it directly; the pin only steers
     `mode="auto"` probing): the ask is RETURNED as an `InputRequiredResult` and
     the client retries with the answer (crib/server.py `_ask_per_call`), for
     the unanchored read and the destructive confirm alike.

Only an e2e round-trip over HTTP shows both: the in-memory transport has neither
the per-request era header nor a real retry.
"""

from __future__ import annotations

import asyncio
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
    ephemeral port, prepared the PRODUCTION way (_prepare_http_serve — the same
    function _serve_async's http branch calls) so the era/idle behavior under
    test is the daemon's actual behavior. The in-memory transport reuses one
    session object, so the ServerSession-per-request failure mode cannot
    reproduce there — the failure is only visible over a real client session
    spanning several calls."""
    import uvicorn

    from crib.server import _prepare_http_serve

    mcp = build_server(crib)
    _prepare_http_serve(mcp)
    app = mcp.http_app(transport="streamable-http")
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


def _counting(answer):
    asked: list[str] = []

    async def handler(message, response_type, params, ctx):  # noqa: ANN001
        asked.append(message)
        return answer

    return handler, asked


def test_handshake_asks_per_call_and_nothing_sticks(crib, http_url):
    """A read naming no project is ASKED (sync and async tools alike), the
    answer runs that call, and the NEXT selector-less call is asked again."""
    from fastmcp import Client

    asyncio.run(crib.project_create("alpha"))
    assert "alpha" in crib.projects(), "precondition failed: project_create didn't land"
    handler, asked = _counting("alpha")

    async def session():
        async with Client(http_url, mode="legacy", elicitation_handler=handler) as c:
            one = await c.call_tool("note_lookup", {"query": "anything"})  # sync
            two = await c.call_tool("note_reindex", {})                    # async
            three = await c.call_tool("note_lookup", {"query": "again"})
            return one, two, three

    asyncio_run(session())
    assert len(asked) == 3  # every selector-less call asked; nothing carried over
    assert all("named no project" in m for m in asked)


def test_handshake_refuses_without_a_handler(crib, http_url):
    from fastmcp import Client

    async def session():
        async with Client(http_url, mode="legacy") as c:  # no elicitation handler
            return await c.call_tool("note_lookup", {"query": "anything"})

    with pytest.raises(Exception, match="named no project") as exc:
        asyncio_run(session())
    assert "project=<name>" in str(exc.value)


def test_project_use_and_current_are_gone(crib, http_url):
    from fastmcp import Client

    async def session():
        async with Client(http_url, mode="legacy") as c:
            return {t.name for t in await c.list_tools()}

    names = asyncio_run(session())
    assert not names & {"project_use", "project_current"}
    assert "note_lookup" in names


def test_a_default_auto_client_negotiates_the_handshake_era(crib, http_url):
    """The era pin still steers a DEFAULT fastmcp client (mode="auto", which
    probes server/discover) onto the handshake era; lifting it is its own step."""
    from fastmcp import Client

    asyncio.run(crib.project_create("alpha"))

    async def session():
        async with Client(http_url) as c:  # mode defaults to "auto"
            out = await c.call_tool("note_lookup", {"project": "alpha", "query": "x"})
            return getattr(c, "protocol_version", None), out

    era, out = asyncio_run(session())
    assert era is not None and era != "2026-07-28"
    assert out is not None


# ── 2026-07-28: the ask is RETURNED and answered on the retry ────────────────


def test_2026_an_explicit_project_needs_no_ask(crib, http_url):
    from fastmcp import Client

    asyncio.run(crib.project_create("alpha"))
    handler, asked = _counting("alpha")

    async def session():
        async with Client(http_url, mode="2026-07-28",
                          elicitation_handler=handler) as c:
            return await c.call_tool("note_lookup", {"project": "alpha", "query": "x"})

    assert asyncio_run(session()) is not None
    assert asked == []


def test_2026_asks_per_call_by_returning_the_question(crib, http_url):
    """A selector-less read on 2026: crib returns an InputRequiredResult, the
    client asks its human and retries with the answer, the call runs. Every
    such call asks again."""
    from fastmcp import Client

    asyncio.run(crib.project_create("alpha"))
    handler, asked = _counting("alpha")

    async def session():
        async with Client(http_url, mode="2026-07-28",
                          elicitation_handler=handler) as c:
            one = await c.call_tool("note_lookup", {"query": "anything"})
            two = await c.call_tool("note_lookup", {"query": "again"})
            return one, two

    one, two = asyncio_run(session())
    assert one is not None and two is not None
    assert len(asked) == 2


def test_2026_a_declined_ask_refuses_actionably(crib, http_url):
    from fastmcp import Client
    from fastmcp.client.elicitation import ElicitResult

    async def decline(message, response_type, params, ctx):  # noqa: ANN001
        return ElicitResult(action="decline")

    async def session():
        async with Client(http_url, mode="2026-07-28",
                          elicitation_handler=decline) as c:
            return await c.call_tool("note_lookup", {"query": "anything"})

    with pytest.raises(Exception, match="named no project"):
        asyncio_run(session())


def test_2026_destructive_confirm_is_asked_per_call(crib, http_url):
    """project_delete confirms by asking; on 2026 the confirmation round-trips
    as a returned ask, sealed to this call, and a wrong echo aborts."""
    from fastmcp import Client

    asyncio.run(crib.project_create("doomed"))
    asyncio.run(crib.project_create("kept"))
    wrong, _ = _counting("not-it")
    right, asked = _counting("doomed")

    async def delete(project, handler):
        async with Client(http_url, mode="2026-07-28",
                          elicitation_handler=handler) as c:
            return await c.call_tool("project_delete", {"project": project})

    with pytest.raises(Exception, match="did not echo"):
        asyncio_run(delete("kept", wrong))
    assert "kept" in crib.projects()
    asyncio_run(delete("doomed", right))
    assert len(asked) == 1 and "doomed" not in crib.projects()


# ── the CLI's delete, run BY the daemon (its warm state goes with it) ────────


def test_the_cli_client_answers_the_delete_confirmation(crib, http_url):
    """`crib project delete` with a daemon up: the human confirmed in the
    terminal, so the CLI's client answers the daemon's confirmation (echo the
    name) and the daemon deletes the project itself; any other question is
    declined."""
    from urllib.parse import urlparse

    from crib import cli
    from crib.client import DaemonClient
    from crib.config import DaemonConfig

    asyncio.run(crib.project_create("doomed"))
    asyncio.run(crib.project_create("kept"))
    u = urlparse(http_url)
    client = DaemonClient(DaemonConfig(host=u.hostname, port=u.port))
    asyncio.run(client._call("project_delete", {"project": "doomed"}, None,
                             cli._delete_confirmation("doomed")))
    assert "doomed" not in crib.projects()
    # answering for a different project echoes nothing: the delete is refused
    with pytest.raises(Exception, match="did not echo|aborted|confirmation"):
        asyncio.run(client._call("project_delete", {"project": "kept"}, None,
                                 cli._delete_confirmation("doomed")))
    assert "kept" in crib.projects()


def test_the_delete_confirmation_answers_only_its_own_question():
    from crib import cli

    ans = cli._delete_confirmation("p")
    assert ans("Confirm: DELETE crib project 'p' ... Type the project name 'p'") == "p"
    assert ans("Which crib project should this fact be stored in?") is None
    assert ans("Type the project name 'other'") is None
