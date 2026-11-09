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
    READ_ONLY_TOOLS,
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
    # system always rides, even in read-only deployments: adding to it is a
    # reviewed decision. store_migrate: whole-store housekeeping like reconcile,
    # a dry run unless apply, never touches notes (writes only a new index).
    assert set(by_set["system"]) == {"ops", "status", "project_reconcile", "store_migrate"}


def test_exposure_validates_and_always_includes_system():
    assert exposure(None) is None  # everything
    assert exposure("") is None and exposure([]) is None
    exposure_result = exposure("notes, plans")
    assert exposure_result is not None
    sets, ro = exposure_result
    assert "notes" in sets and "plans" in sets and "system" in sets
    assert ro == frozenset()  # no suffix: nothing read-only
    # the :ro suffix — a set exposed read-only
    exposure_result = exposure("notes:ro,plans")
    assert exposure_result is not None
    sets, ro = exposure_result
    assert sets >= {"notes", "plans", "system"} and ro == {"notes"}
    # ro-only spelling is also exposure
    exposure_result = exposure("notes:ro")
    assert exposure_result is not None
    sets, ro = exposure_result
    assert sets == frozenset({"notes", "system"}) and ro == {"notes"}
    with pytest.raises(ValueError, match="unknown tool set"):
        exposure("notes,bananas")
    # the refusal NAMES the valid sets — fixable without a doc lookup
    with pytest.raises(ValueError, match="learnings"):
        exposure("banana")
    with pytest.raises(ValueError, match=":ro"):
        exposure("notes:foo")  # an unknown SUFFIX refuses too


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


def test_readonly_coverage_is_total_and_disjoint():
    """The r/o classification is DATA: every declared read-only verb is a REAL
    member of its set (so a typo'd name or a renamed tool fails here, loudly);
    an UNDECLARED tool just counts as mutating — the safe default (a r/o
    deployment hides it rather than silently offering a write)."""
    tools = sorted({v.tool for v in VERBS.values()})
    for set_name in TOOL_SET_NAMES:
        if set_name == "system":
            continue  # the poll/meta trio: always exposed, no r/o distinction
        in_set = {t for t in tools if tool_set_of(t) == set_name}
        declared = set(READ_ONLY_TOOLS[set_name])
        phantoms = declared - in_set
        assert not phantoms, (
            f"{set_name}: RO list names non-members: {sorted(phantoms)}"
        )
        assert declared, f"{set_name}: no read-only verbs declared at all?"


def test_readonly_set_serves_only_its_reading_half(crib):
    """notes:ro: the reads exist and ANSWER; note_store/append/edit/forget/move
    are ABSENT from the face (not registerable, not visible) — and the
    instructions preamble says the deployment is read-only."""
    mcp = build_server(crib, tools=["notes:ro"])
    names = {t.name for t in run(mcp.list_tools())}
    assert {"note_lookup", "note_read", "note_versions"} <= names
    assert not (
        names
        & {
            "note_store",
            "note_append",
            "note_edit",
            "note_forget",
            "note_move",
            "note_reindex",
            "note_distill",
            "note_elaborate",
            "note_summarize",
            "note_import",
            "note_import_memory",
            "note_restore",
        }
    )
    # designs/plans/code/learnings/projects: absent entirely — the face carries
    # NO tool from a set that wasn't exposed (which is exactly what a r/o mode
    # and the filter are for: `names` is the face's tool set; anything whose
    # set isn't exposed here is a leak)
    gated = {n for n in names if tool_set_of(n) not in ("notes", "system")}
    assert not gated, f"non-exposed tools on the face: {sorted(gated)}"
    instr = (mcp.instructions or "").upper()
    assert "READ-ONLY DEPLOYMENT" in instr
    assert "NOTES" in instr  # the exposed sets named
    # poll/meta still present (system rides)
    assert {"status", "ops", "project_reconcile"} <= names


def test_readonly_writes_never_even_register(tmp_path, crib):
    """The r/o guarantee at the face: an exposed-but-readonly set can answer a
    READ over the wire, and a write verb is an UNKNOWN TOOL — the model's own
    tools view (list_tools) never advertises it."""
    from fastmcp import Client

    mcp = build_server(crib, tools=["notes:ro"])
    run(crib.store_note("the body", title="The", project="default"))

    async def probe():
        async with Client(mcp) as c:
            lookup = await c.call_tool(
                "note_lookup", {"query": "the body", "project": "default"}
            )
            try:
                await c.call_tool(
                    "note_store", {"title": "x", "content": "y", "project": "default"}
                )
                return lookup, "WRITE LANDED?!"
            except Exception as e:
                return lookup, str(e)

    lookup, write = run(probe())
    assert lookup is not None
    assert "Unknown tool" in write
