"""The protocol era decides HOW crib asks, nothing else (crib/session.py,
crib/server.py `_ask`).

There is no per-connection state to anchor (design: "Retire use_project and
implicit anchoring (all eras)"). The era is classified in one place
(`_era_of_version`) from the client's `mcp-protocol-version` header. On the
handshake era an ask is a server-initiated `ctx.elicit`; on the 2026-07-28
stateless era it is RETURNED (`InputRequiredResult`) and the answer read off the
client's retry (`_ask_per_call`). The retry's `request_state` is sealed by the
SDK and bound to the tool + arguments; crib's own state only names the ask.
"""

from __future__ import annotations

from types import SimpleNamespace

import mcp.types as mt
import pytest

from crib import server
from crib.session import (
    ERA_HANDSHAKE,
    ERA_STATELESS,
    NAME_THE_PROJECT,
    _era_of_version,
)

# ── era classification: only a well-formed 2026-07-28+ ISO date is stateless ──


@pytest.mark.parametrize(
    "version, want",
    [
        ("2026-07-28", ERA_STATELESS),  # the stateless era itself
        ("2026-11-30", ERA_STATELESS),  # any later dated revision, also stateless
        ("2025-11-25", ERA_HANDSHAKE),  # the handshake era fastmcp clients pin
        ("2025-06-18", ERA_HANDSHAKE),  # older dated revision
        (None, ERA_HANDSHAKE),  # no header (stdio, in-memory, first request)
        ("", ERA_HANDSHAKE),
        ("  2026-07-28  ", ERA_STATELESS),  # whitespace tolerance
        ("garbage", ERA_HANDSHAKE),  # malformed values must never misclassify
        ("9999999-99-99", ERA_HANDSHAKE),  # right prefix, not an ISO date
    ],
)
def test_era_of_version(version, want):
    assert _era_of_version(version) == want


# ── the stateless ask: returned, answered on the retry ───────────────────────


def _context(monkeypatch, answers=None, state=None):
    ctx = SimpleNamespace(input_responses=answers, request_state=state)
    monkeypatch.setattr("fastmcp.server.dependencies.get_context", lambda: ctx)
    return ctx


def _accept(value):
    return mt.ElicitResult(action="accept", content={"value": value})


def test_the_first_call_returns_the_question(monkeypatch):
    _context(monkeypatch)
    with pytest.raises(server.NeedsInput) as ask:
        server._ask_per_call("project", "Which project?", "read-project")
    irr = ask.value.result
    assert isinstance(irr, mt.InputRequiredResult)
    assert irr.request_state == "read-project"
    assert irr.input_requests is not None
    req = irr.input_requests["project"]
    assert isinstance(req, mt.ElicitRequest)
    assert isinstance(req.params, mt.ElicitRequestFormParams)
    assert req.params.message == "Which project?"
    assert req.params.requested_schema["required"] == ["value"]


def test_the_retry_reads_the_answer(monkeypatch):
    _context(monkeypatch, {"project": _accept("  cribsheet ")}, "read-project")
    assert server._ask_per_call("project", "?", "read-project") == "cribsheet"


@pytest.mark.parametrize("answer", [
    mt.ElicitResult(action="decline"),
    mt.ElicitResult(action="cancel"),
    _accept("   "),
])
def test_a_declined_or_blank_answer_is_no_answer(monkeypatch, answer):
    _context(monkeypatch, {"project": answer}, "read-project")
    assert server._ask_per_call("project", "?", "read-project") is None


def test_an_answer_to_another_ask_asks_again(monkeypatch):
    """A retry carrying a different ask's state (or none) doesn't count."""
    _context(monkeypatch, {"project": _accept("p")}, "write-project")
    with pytest.raises(server.NeedsInput):
        server._ask_per_call("project", "?", "read-project")


def _server(tmp_path, monkeypatch):
    import asyncio

    from crib.app import Crib
    from crib.config import Config
    from crib.paths import Paths
    from crib.store import InMemoryStore

    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "i"))
    app = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
    app.paths.project_dir("p").mkdir(parents=True, exist_ok=True)
    mcp = server.build_server(app)
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_a_stateless_read_naming_no_project_returns_the_question(tmp_path, monkeypatch):
    """A sync read can ask on the stateless era (asking is returning); the
    tool boundary turns the raised ask into the tool's result, and the retry
    carrying the answer runs against the named project."""
    import asyncio

    from fastmcp.tools.base import InputRequiredToolResult

    tools = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "request_era", lambda: ERA_STATELESS)
    _context(monkeypatch)
    out = asyncio.run(tools["learning_report"].run({}))
    assert isinstance(out, InputRequiredToolResult)
    _context(monkeypatch, {"project": _accept("p")}, "read-project")
    done = asyncio.run(tools["learning_report"].run({}))
    assert not isinstance(done, InputRequiredToolResult)


def test_a_stateless_decline_refuses_actionably(tmp_path, monkeypatch):
    import asyncio

    from fastmcp.exceptions import ToolError

    tools = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "request_era", lambda: ERA_STATELESS)
    _context(monkeypatch, {"project": mt.ElicitResult(action="decline")}, "read-project")
    with pytest.raises(ToolError, match="named no project"):
        asyncio.run(tools["learning_report"].run({}))


def test_the_refusal_names_the_recovery():
    assert "project=<name>" in NAME_THE_PROJECT and "project_path=" in NAME_THE_PROJECT
