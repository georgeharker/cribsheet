"""Repo-scoped ops must resolve their project from `project_path`'s `.crib`, not
the sticky session project. Regression: a project_index(project_path=/other/repo)
with a sticky current project once indexed the OTHER repo INTO the current one."""

from __future__ import annotations

import pytest

from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.server import _source_project, _write_project
from crib.session import session_state
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def test_explicit_project_wins(crib):
    assert _source_project(crib, "chosen", "/some/repo") == "chosen"


def test_project_path_defers_to_crib_not_sticky(crib):
    # sticky session is on some project…
    session_state().current_project = "cribsheet"
    # …but a repo-scoped call names a DIFFERENT repo via project_path → return None
    # so crib.project_* reads link.project from THAT repo's .crib (never the sticky).
    assert _source_project(crib, None, "/Users/me/other-repo") is None


def test_no_path_falls_back_to_session(crib):
    session_state().current_project = "sticky-proj"
    assert _source_project(crib, None, None) == "sticky-proj"


def test_write_tools_carry_project_or_path_anyof(crib):
    # the wire schema declares "project OR project_path required" (anyOf), so a
    # validating client enforces it up front — not only the runtime guard.
    import asyncio

    from crib.server import build_server
    mcp = build_server(crib)

    async def schema(name):
        return (await mcp.get_tool(name)).to_mcp_tool().inputSchema

    want = [{"required": ["project"]}, {"required": ["project_path"]}]
    for w in ("note_store", "note_append", "note_edit", "note_forget", "note_move"):
        assert asyncio.run(schema(w)).get("anyOf") == want, w
    for r in ("note_lookup", "note_read", "code_lookup"):   # reads are unconstrained
        assert asyncio.run(schema(r)).get("anyOf") is None, r


def test_write_project_elicits_when_target_omitted(crib):
    import asyncio

    from crib.server import _write_project_elicit

    class _Accepted:                       # mimics fastmcp AcceptedElicitation
        def __init__(self, data): self.data = data

    class _Ctx:
        def __init__(self, behaviour): self.behaviour = behaviour
        async def elicit(self, message, response_type=None):
            if self.behaviour == "accept":
                return _Accepted("chosen-proj")
            if self.behaviour == "decline":
                return object()            # no .data → treated as declined
            raise RuntimeError("client has no elicitation capability")

    run = asyncio.run
    # explicit project short-circuits (never elicits)
    assert run(_write_project_elicit(crib, "shuck", None, _Ctx("accept"))) == "shuck"
    # omitted → elicited value is used
    assert run(_write_project_elicit(crib, None, None, _Ctx("accept"))) == "chosen-proj"
    # declined or unsupported → falls back to the hard error
    for b in ("decline", "unsupported"):
        with pytest.raises(ValueError, match="explicit target"):
            run(_write_project_elicit(crib, None, None, _Ctx(b)))


def test_write_project_requires_explicit_target(crib):
    # writes never inherit the sticky session — a fact belongs to its subject's project
    session_state().current_project = "some-repo-im-browsing"
    with pytest.raises(ValueError, match="explicit target"):
        _write_project(crib, None, None)
    # explicit project wins
    assert _write_project(crib, "shuck", None) == "shuck"
    # project_path resolves via that repo's .crib (here: no .crib → default)
    assert _write_project(crib, None, str(crib.paths.data_dir)) == crib.config.default_project
