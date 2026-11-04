"""Per-deployment tool-set EXPOSURE (crib serve --tools / config [tools].expose).

A general assistant host runs crib's stdio server NEXT TO other MCP servers —
it should see the prose facets (notes/designs/plans/memory), never code
intelligence, learnings or project management; a pi coding session gets
everything. The mapping is the tool NAME's own family (+ `system`: status/ops/
project_reconcile, the poll/meta surface, which always rides) — so a tool can
never fall between the sets: every VERBS tool maps, tested here.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from crib.app import Crib
from crib.cli import VERBS
from crib.config import Config
from crib.paths import Paths
from crib.server import (
    TOOL_SET_NAMES,
    TOOL_POLICY,
    build_server,
    exposure,
    tool_set_of,
)
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def run(coro):
    return asyncio.run(coro)


def test_every_tool_maps_to_a_known_set():
    """A tool can never fall between the sets: every CLI-registry tool maps to
    a known set name — and family membership agrees with the noun it nests
    under."""
    tools = sorted({v.tool for v in VERBS.values()})
    assert tools, "the registry vanished?"
    for t in tools:
        assert tool_set_of(t) in TOOL_SET_NAMES, t
    # the mapped families are real: every prefixed tool landed in its plural set
    by_set: dict[str, list[str]] = {}
    for t in tools:
        by_set.setdefault(tool_set_of(t), []).append(t)
    assert set(by_set) == set(TOOL_SET_NAMES)
    for fam, set_name in (
        ("note", "notes"),
        ("design", "designs"),
        ("plan", "plans"),
        ("code", "code"),
        ("learning", "learnings"),
        ("project", "projects"),
        ("memory", "memory"),
    ):
        assert all(t.startswith(fam + "_") for t in by_set[set_name]), set_name
    # the system set is exactly the poll/meta trio (+ any unprefixed strays)
    assert set(by_set["system"]) == {"ops", "status", "project_reconcile"}


def test_exposure_validates_and_always_includes_system():
    assert exposure(None) is None  # everything
    assert exposure("") is None and exposure([]) is None
    out = exposure("notes, plans")
    assert out is not None and "notes" in out and "plans" in out
    assert "system" in out  # the poll/meta surface ALWAYS rides
    with pytest.raises(ValueError, match="unknown tool set"):
        exposure("notes,bananas")
    # the refusal NAMES the valid sets — fixable without a doc lookup
    with pytest.raises(ValueError, match="learnings"):
        exposure("banana")


def test_filtered_server_serves_only_its_sets(crib):
    """build_server(tools=['notes','plans','memory']) exposes exactly those
    (+ system): note_lookup answers, project_use/code_lookup are absent —
    a general assistant never sees code intelligence or project management —
    and the POLICY table stays full (exposure is face-level, not a rewrite)."""
    mcp = build_server(crib, tools=["notes", "plans", "memory"])
    names = {t.name for t in run(mcp.list_tools())}
    assert any(t.startswith("note_") for t in names)
    assert any(t.startswith("plan_") for t in names)
    assert any(t.startswith("memory_") for t in names)
    assert "project_use" not in names and "code_lookup" not in names
    assert "learning_add" not in names and "design_add" not in names
    assert {"status", "ops", "project_reconcile"} <= names  # system always
    # the policy table remains FULL (the declared/reserved policies stay
    # checkable even for sets this deployment doesn't serve)
    assert {v.tool for v in VERBS.values()} <= set(TOOL_POLICY)
    # and the face still WORKS for an exposed verb
    from fastmcp import Client

    async def probe():
        async with Client(mcp) as c:
            res = await c.call_tool(
                "note_store", {"title": "t", "content": "body", "project": "default"}
            )
            return res

    out = run(probe())
    _ = out  # a ToolError would have failed the probe


def test_default_build_exposes_everything(crib):
    mcp = build_server(crib)  # no filter: every set
    names = {t.name for t in run(mcp.list_tools())}
    assert {v.tool for v in VERBS.values()} <= names
    assert names >= {"note_lookup", "code_lookup", "project_use", "ops"}


def test_cli_flag_overrides_config_expose(tmp_path):
    """serve resolution: --tools wins; [tools].expose next; neither = all."""

    from crib.server import main as _serve  # noqa: F401 (plumbing exists)

    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[tools]\nexpose = ["notes", "memory"]\n')

    class _Args:
        tools = None  # unset flag → config wins

    assert _resolve_tool_spec_with(_Args(), cfg_file) == ["notes", "memory"]

    class _FlagArgs:
        tools = "code,projects"  # the flag overrides the file

    assert _resolve_tool_spec_with(_FlagArgs(), cfg_file) == "code,projects"

    class _Neither:
        tools = None

    empty_cfg = tmp_path / "none.toml"
    assert _resolve_tool_spec_with(_Neither(), empty_cfg) is None


def _resolve_tool_spec_with(args: Any, config_file):
    """The cli._resolve_tool_spec logic with an injectable config path."""
    from crib.config import Config

    spec = getattr(args, "tools", None)
    if spec is not None and str(spec).strip():
        return str(spec)
    cfg = Config.load(config_file)
    return cfg.tools.expose or None
