"""Per-call project resolution (crib/session.py `resolve_call_project`).

Design: "Retire use_project and implicit anchoring (all eras)". A read's project
comes from what THAT call names; nothing is remembered between calls."""

from crib.session import resolve_call_project


def _seed(mapping):
    return lambda cwd: mapping.get(cwd, "default")


def test_an_explicit_project_wins():
    res = resolve_call_project("p", "/repo", _seed({"/repo": "repo"}), "default")
    assert (res.project, res.via) == ("p", "explicit")
    assert not res.unanchored and not res.worth_echoing


def test_a_path_decides_through_its_crib():
    res = resolve_call_project(None, "/repo", _seed({"/repo": "repo"}), "default")
    assert (res.project, res.via) == ("repo", "path")
    assert not res.worth_echoing


def test_nothing_carries_over_between_calls():
    """A path-bearing call used to make its repo the session's current project;
    now the next call that names nothing is unanchored, as if it were the first."""
    seed = _seed({"/repo": "repo"})
    assert resolve_call_project(None, "/repo", seed, "default").project == "repo"
    later = resolve_call_project(None, None, seed, "default")
    assert later.unanchored and later.via == "seed"


def test_a_path_deciding_nothing_lands_on_default_and_says_so():
    """A cwd with no `.crib` lands on the bare default: its documented meaning
    (the CLI standing in ~), not a guess, but flagged for MCP callers."""
    res = resolve_call_project(None, "/nowhere", _seed({}), "default")
    assert (res.project, res.via) == ("default", "seed")
    assert res.path_unmatched and not res.unanchored


def test_only_the_no_cwd_fallthrough_is_unanchored():
    """No selector and no cwd: the one branch where any answer is a guess. The
    server layer asks (elicitation) or refuses instead of answering."""
    res = resolve_call_project(None, None, _seed({}), "default")
    assert res.via == "seed" and res.unanchored
    assert "UNANCHORED" in res.echo().get("warning", "")
    assert "every call" in res.echo()["warning"]
    assert res.worth_echoing


def test_an_unanchored_read_refuses_with_the_recovery_in_the_message(
    tmp_path, monkeypatch
):
    """The descriptive exception IS the mechanism: CribUserError rides the MCP
    face verbatim, the LLM reads it, and re-calling with project= is the
    recovery. So the error carries both selector spellings and the projects."""
    import asyncio

    import pytest
    from fastmcp.exceptions import ToolError

    from crib.app import Crib
    from crib.config import Config
    from crib.paths import Paths
    from crib.server import build_server
    from crib.store import InMemoryStore

    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "i"))
    app = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
    mcp = build_server(app)
    tool = {t.name: t for t in asyncio.run(mcp.list_tools())}["learning_report"]
    with pytest.raises(ToolError) as e:
        asyncio.run(tool.run({}))  # names no project
    msg = str(e.value)
    assert "named no project" in msg
    assert "project=" in msg and "project_path=" in msg and "every call" in msg
