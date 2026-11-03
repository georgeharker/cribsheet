"""The ONE anchor gate for the protocol era (session.py, crib/server.py).

The stateless era (mcp 2026-07-28) builds a Connection per request, so an
anchor that looked like it took would die after the call — the same silent
forgetting the fastmcp 4 port exists to prevent (tests/test_http_session.py).
Everything that anchors a session goes through ONE gate: `adopt_into_session`,
and the era itself is classified in one place (`_era_of_version`) from the
client's `mcp-protocol-version` header. This file pins both in units; the e2e
handshake-era behavior stays in tests/test_http_session.py (a stateless e2e is
impossible while the daemon pins `supported_versions=[]` — that gate is tested
here at the unit level, and an unpin later adds the e2e alongside it).

The tests below monkeypatch `crib.session.request_era` — the same module-global
the resolution code reads, so the patch exercises the REAL call sites.
"""

from __future__ import annotations

import pytest

from crib import session as sessionmod
from crib.session import (
    ERA_HANDSHAKE,
    ERA_STATELESS,
    STATELESS_HINT,
    SessionState,
    _era_of_version,
    resolve_session_project,
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


# ── the gate: handshake anchors, stateless refuses and writes nothing ─────────


def test_adopt_anchors_on_the_handshake_era():
    st = SessionState()
    # in-process tests have no HTTP request → request_era() is handshake; pin it
    # explicitly anyway so the test states its premise, not its environment
    import unittest.mock as mock

    with mock.patch.object(sessionmod, "request_era", return_value=ERA_HANDSHAKE):
        assert adopt_via(st, "alpha") is True
    assert st.current_project == "alpha"


def adopt_via(st: SessionState, project: str) -> bool:
    """`adopt_into_session` targets the ambient context's session, not a passed
    state — so this drives the REAL gate through the state the way the callers
    do: with a SessionState substituted for the ambient one."""
    orig = sessionmod.session_state

    def fake_state():
        return st

    sessionmod.session_state = fake_state  # type: ignore[assignment]
    try:
        return sessionmod.adopt_into_session(project)
    finally:
        sessionmod.session_state = orig


def test_adopt_refuses_on_the_stateless_era_and_writes_nothing():
    st = SessionState()
    import unittest.mock as mock

    with mock.patch.object(sessionmod, "request_era", return_value=ERA_STATELESS):
        assert adopt_via(st, "alpha") is False
    assert st.current_project is None  # nothing stuck — not even a corpse


# ── resolution: the stateless path branch must not claim it stuck ─────────────


def seed_to(cwd: str) -> str:
    """A seed callable mimicking a cwd whose `.crib` decides: names a real
    project distinct from the bare default."""
    return "alpha"


def test_path_adoption_on_the_stateless_era_never_sets_session_set():
    st = SessionState()
    import unittest.mock as mock

    with mock.patch.object(sessionmod, "request_era", return_value=ERA_STATELESS):
        res = resolve_session_project(st, None, "some/repo", seed_to, default="default")
    assert res.via == "path" and res.session_set is False
    assert st.current_project is None  # no anchor written


def test_path_adoption_on_the_handshake_era_adopts_once_and_says_so():
    st = SessionState()
    import unittest.mock as mock

    with mock.patch.object(sessionmod, "request_era", return_value=ERA_HANDSHAKE):
        first = resolve_session_project(st, None, "some/repo", seed_to, "default")
        assert first.via == "path" and first.session_set is True
        assert st.current_project == "alpha"
        # a second call does NOT re-adopt (DESIGN §15: adopt once)
    second = resolve_session_project(st, None, "some/repo", seed_to, "default")
    # a later PATH-bearing call still resolves `path` (rule 1 outranks the session)
    assert second.via == "path" and second.session_set is False
    third = resolve_session_project(st, None, None, seed_to, "default")
    # …and a SELECTOR-LESS call lands on the adopted one (the whole point)
    assert third.via == "session" and third.session_set is False


def test_stateless_hint_names_the_recovery():
    # the hint must be actionable by a bare string read: name the params.
    assert "project=<name>" in STATELESS_HINT and "project_path=" in STATELESS_HINT
