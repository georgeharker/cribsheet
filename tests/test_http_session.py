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
  4. THE ERA PIN — a client whose default is `mode="auto"` (fastmcp 4's own
     default: it probes `server/discover` and negotiates the modern era when
     it can) must land on the HANDSHAKE era and keep sticky sessions working.
     The daemon pins that server-side (`_pin_regression_era`), because on the
     2026-07-28 stateless era every Connection is per-request and the sticky
     session project + elicitation silently die.
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
    asyncio.run(crib.project_create("alpha"))
    # PRECONDITION, on the record: the flake investigation (2026-11-02) found a
    # one-off failure with `projects()` empty at the elicit check on an IN-FLIGHT
    # working tree (another session was rewriting the port mid-run). Assert the
    # precondition so a repeat names the broken invariant directly instead of
    # surfacing as a mysterious resolution error.
    assert "alpha" in crib.projects(), "precondition failed: project_create didn't land"

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


def test_a_default_auto_client_negotiates_the_handshake_era(crib, http_url):
    """The era pin: a DEFAULT fastmcp client (mode="auto" — probes server/discover
    and adopts the modern era when the server advertises it) must end up on the
    handshake era, because the daemon advertises supported_versions=[] there.
    Regression case: before _pin_legacy_era this client negotiated 2026-07-28
    (measured: current_project answered 'default'/'seed' on the call right
    after project_use — no error, just no session)."""
    from fastmcp import Client

    async def session():
        async with Client(http_url) as c:  # mode defaults to "auto"
            await c.call_tool("project_use", {"project": "alpha"})
            one = await c.call_tool("project_current", {})
            two = await c.call_tool("project_current", {})
            era = getattr(c, "protocol_version", None)
            return era, _data(one), _data(two)

    era, one, two = asyncio_run(session())
    # handshake-era negotiation — no stateless 2026-07-28 connection
    assert era is not None and era != "2026-07-28"
    for r in (one, two):
        assert r.get("current_project") == "alpha"  # sticky state persisted
        assert r.get("resolved_via") == "session"


# ── 4b. SYNC TOOLS ASK — the obs-1 fix (combiner live test, 2026-11-03) ───────
# FastMCP runs sync tool bodies in a threadpool, where an imperative elicit
# cannot block on a client round-trip — so crib's sync path REFUSED instead of
# asking (note_lookup, design_lookup, plan_next and 20 more sync reads all
# refused while an async code_lookup asked live). The sync READ bodies now run
# their unanchored branch through the elicit-capable path; the refusal stays for
# clients that can't take elicitation.


# ── 5. THE PIN SERVES 2026 (direct) — it only STEERS auto-clients ──────────────
# Combiner-era correction (2026-11-02): the discover pin exists for `mode="auto"`
# probing. A client that SKIPS discovery and speaks 2026-07-28 directly must still
# be served (that is how the combiner's per-server `protocol: "2026"` opt-in works
# — no pin-dropping switch needed). On that era the one anchor gate applies:
# nothing anchors between calls, and every answer is a visible per-call result.


def test_sync_tools_ask_and_the_answer_sticks(crib, http_url):
    """The obs-1 fix, end to end: a SYNC tool (note_lookup) whose body used to
    refuse on unanchored now ASKS over the back-channel, and the human's answer
    sticks for that chat (project_use-free flow) — the same contract the async
    reads (code_lookup) already had."""
    from fastmcp import Client

    async def accept_alpha(message, response_type, params, ctx):  # noqa: ANN001
        return "alpha"

    # the elicited NAME must be a REAL project (same precondition as the async
    # elicited-adopt test — and the same self-explaining assert)
    asyncio.run(crib.project_create("alpha"))
    assert "alpha" in crib.projects(), "precondition failed: project_create didn't land"

    async def session():
        async with Client(
            http_url, mode="legacy", elicitation_handler=accept_alpha
        ) as c:
            r = await c.call_tool("note_lookup", {"query": "anything"})
            cur = await c.call_tool("project_current", {})
            return _data(r), _data(cur)

    lookup, cur = asyncio_run(session())
    assert lookup is not None  # the ask was ANSWERED — the lookup ran
    assert cur.get("current_project") == "alpha"
    assert cur.get("resolved_via") == "session"  # the answer stuck


def test_sync_tools_still_refuse_without_a_handler(crib, http_url):
    """The refusal path is intact: a client that can't take elicitation gets the
    actionable refusal, never a silent default answer."""
    from fastmcp import Client

    async def session():
        async with Client(http_url, mode="legacy") as c:  # no elicitation handler
            return await c.call_tool("note_lookup", {"query": "anything"})

    with pytest.raises(Exception, match="no project is anchored"):
        asyncio_run(session())


def test_pin_serves_a_direct_2026_client(crib, http_url):
    """A `mode="2026-07-28"` client (no discover) initializes against the PINNED
    daemon and works — but per call: project_use refuses to anchor (reports so),
    and a selector-less read lands on seed, never on a session."""
    from fastmcp import Client

    async def session():
        async with Client(http_url, mode="2026-07-28") as c:
            use = await c.call_tool("project_use", {"project": "alpha"})
            # selector-less read on 2026: the fresh per-request state is empty —
            # it must answer without erroring and WITHOUT any “session” anchor
            cur = await c.call_tool("project_current", {})
            # explicit selector: the only way state is carried on 2026
            lookup = await c.call_tool(
                "note_lookup", {"project": "alpha", "query": "x", "k": 3}
            )
            return _data(use), _data(cur), _data(lookup)

    use, cur, lookup = asyncio_run(session())
    # project_use does not pretend: nothing anchored, it says what to do instead
    assert use.get("anchored") is False
    assert "STATELESS" in use.get("note", "").upper() or "stateless" in use.get(
        "note", ""
    )
    # and the next call confirms nothing stuck
    assert cur.get("resolved_via") != "session"
    assert cur.get("current_project") != "alpha"
    # per-call selectors are how 2026 works, and they work fully
    assert lookup is not None


def test_stateless_selector_less_read_refuses_actionably(crib, http_url):
    """A selector-less READ on 2026 elicits per call — but ctx.elicit has no
    channel on a stateless connection, so it falls through to the REFUSAL, which
    must name the stateless fact and the recovery (pass project= every call).
    Never a silent `default` answer, never a hang."""
    from fastmcp import Client

    async def session():
        async with Client(http_url, mode="2026-07-28") as c:
            return await c.call_tool("note_lookup", {"query": "anything"})

    with pytest.raises(Exception, match="STATELESS") as exc:
        asyncio_run(session())
    msg = str(exc.value)
    assert "nothing anchors between calls" in msg
    assert "project=<name>" in msg or "project_path=" in msg
