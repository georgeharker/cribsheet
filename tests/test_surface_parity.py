"""The CLI⇄MCP surface is one contract, and it is checked mechanically.

`crib/cli.py`'s VERBS registry declares, per verb, its MCP twin's wire signature
(`mcp`) and the project-resolution policy (`policy`) the server must carry. These
tests walk that registry against FastMCP's introspected tool schemas and against
`crib.server.TOOL_POLICY`, so the whole audit class — a param renamed on one face,
a default that drifted, a verb that exists on only one surface, a resolver quietly
swapped inside a tool body — fails here instead of in a user's session.

Break-it-by-construction (how to see each test bite):
  • `test_registry_matches_mcp_tool_schemas` — change `note_apropos`'s `k: int = 8`
    to 5 in server.py: the schema default no longer matches the registry's `k=8`.
  • `test_cli_defaults_match_the_declared_mcp_defaults` — set the CLI parser's
    `note apropos -k` default back to 5 (the shipped bug: the SAME rendered view
    returned 5 hits spelled `note apropos` and 8 spelled `note lookup --render`):
    the CLI-built call no longer matches the declared MCP default.
  • `test_every_mcp_tool_declares_a_resolution_policy` — register a tool with a
    bare `@mcp.tool()` instead of `@crib_tool(...)`: it carries no declaration.
  • `test_registry_policy_matches_the_server_declaration` — flip `code_index` back
    to the `read` policy (the P1.1 bug: a sticky session captured another repo's
    symbols): the server declaration no longer matches the registry's `source`.
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path

import pytest

from crib.app import Crib
from crib.cli import VERBS, _dispatch, build_parser
from crib.config import Config
from crib.paths import Paths
from crib.server import TOOL_POLICY, _POLICIES, build_server
from crib.store import InMemoryStore

_CRIB_PY = sorted((Path(__file__).resolve().parent.parent / "crib").glob("*.py"))


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


@pytest.fixture()
def tools(crib):
    """The served tool set, keyed by name (what a client actually sees)."""
    mcp = build_server(crib)
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def _schema_params(tool) -> dict[str, object]:
    """A tool's wire params as {name: default}, `...` when required."""
    schema = tool.to_mcp_tool().inputSchema
    required = set(schema.get("required") or ())
    return {name: (... if name in required else spec.get("default", ...))
            for name, spec in (schema.get("properties") or {}).items()}


# ── The registry is the single source of truth ────────────────────────────────

def test_registry_and_mcp_surface_cover_each_other(tools):
    # every MCP tool is CLI-reachable and every CLI verb has its MCP twin — a verb
    # added to one face only is the drift this catches.
    assert {v.tool for v in VERBS.values()} == set(tools)


def test_every_registry_row_declares_its_mcp_signature_and_policy():
    for key, verb in VERBS.items():
        assert verb.mcp is not None, f"{key}: no `mcp=` signature declared"
        assert verb.policy in _POLICIES, f"{key}: policy {verb.policy!r} not declared"


def test_registry_matches_mcp_tool_schemas(tools):
    for key, verb in VERBS.items():
        assert verb.tool in tools, f"{key}: no MCP tool {verb.tool!r}"
        assert _schema_params(tools[verb.tool]) == verb.mcp_params(), (
            f"{key} ⇄ {verb.tool}: params/defaults drifted")


def test_registry_policy_matches_the_server_declaration(tools):
    assert {v.tool: v.policy for v in VERBS.values()} == TOOL_POLICY


# ── The server declares a policy for every tool (P5.9) ────────────────────────

def test_every_mcp_tool_declares_a_resolution_policy(tools):
    undeclared = sorted(set(tools) - set(TOOL_POLICY))
    assert not undeclared, f"registered without a policy declaration: {undeclared}"
    assert all(p in _POLICIES for p in TOOL_POLICY.values())


def test_no_tool_body_picks_a_resolver_itself():
    """The policy is wired by the decorator; a body that calls a resolver directly
    is the invisible choice that let `code_index` file another repo's symbols under
    the sticky session project. The helpers may appear only in the wiring."""
    src = (Path(__file__).resolve().parent.parent / "crib" / "server.py").read_text()
    body = src.split("def crib_tool(", 1)[1].split("def register(", 1)[1]
    for helper in ("_resolve", "_project", "_source_project", "_write_project"):
        # boundary-matched, so `crib.use_project(` isn't read as `_project(`
        assert not re.search(rf"(?<![\w.]){helper}\(", body), \
            f"a tool body calls {helper}() directly"


# ── Defaults agree on BOTH faces (the apropos k=5/8 class) ────────────────────

_CLI_INVOCATIONS = {
    "note lookup": ["note", "lookup", "q"],
    "note apropos": ["note", "apropos", "q"],
    "code lookup": ["code", "lookup", "q"],
    "code graph": ["code", "graph", "sym"],
    "note elaborate": ["note", "elaborate", "keywords"],
    "learning report": ["learning", "report"],
    "project forget": ["project", "forget"],
    # the design/plan facet, including the two bare-noun defaults (`crib design`
    # → list) — a default that drifts on one face is the same bug class
    "design lookup": ["design", "lookup", "q"],
    "design list": ["design"],
    "design check": ["design", "check"],
    "design tree": ["design", "tree"],
    # the import tier + attribution: `--proposed` defaults must agree on both
    # faces, and the import/promote verbs must exist on both at all
    "design add": ["design", "add", "T", "body"],
    "design promote": ["design", "promote", "ref"],
    "design import": ["design", "import", "DESIGN.md"],
    "plan import": ["plan", "import", "plan.md"],
    "plan lookup": ["plan", "lookup", "q"],
    "plan list": ["plan"],
    "plan next": ["plan", "next"],
}


@pytest.mark.parametrize("key", sorted(_CLI_INVOCATIONS))
def test_cli_defaults_match_the_declared_mcp_defaults(key):
    argv = _CLI_INVOCATIONS[key]
    verb, call = _dispatch(build_parser().parse_args(argv))
    declared = VERBS[key].mcp_params()
    for param, value in call.items():
        if declared.get(param, ...) is ... or value is None or value in argv:
            continue                      # required param, "not given" on the CLI, or
                                          # supplied BY the invocation (a positional
                                          # that is optional on the MCP face) — none of
                                          # those three is a default to compare
        assert value == declared[param], (
            f"{key}: CLI default {param}={value!r} ≠ MCP default {declared[param]!r}")


# ── Imports must name a source (P2.4) ─────────────────────────────────────────

@pytest.mark.parametrize("tool_name, args", [
    ("note_import", {"paths": ["/tmp/whatever.md"]}),
    ("note_import_memory", {}),
])
def test_imports_refuse_to_fall_through_to_the_default_project(crib, tools, tool_name,
                                                               args):
    # an import is ABOUT a repo; with no source it used to file the copies in the
    # config default project silently. Declared `source` + needs_target → it errors.
    with pytest.raises(Exception, match="needs a SOURCE"):
        asyncio.run(tools[tool_name].run(args))
    # …and the requirement is on the wire too, not just at runtime
    schema = tools[tool_name].to_mcp_tool().inputSchema
    assert schema.get("anyOf") == [{"required": ["project"]},
                                   {"required": ["project_path"]}]


# ── Retired command names can't creep back (P3 grep-guard) ────────────────────
# The noun-verb rename left stale spellings in help text, error messages and tool
# docstrings for a release — text nobody re-greps. These two sweeps fail the build
# the next time a rename leaves one behind.

_RETIRED_COMMANDS = {                       # in ANY user-facing string in crib/*.py
    "crib code-": "the noun-verb form (`crib code lookup`, `crib code index`)",
    "crib note-": "the noun-verb form (`crib note lookup`)",
    "crib learning-": "the noun-verb form (`crib learning add`)",
    "crib project-": "the noun-verb form (`crib project index`)",
    "crib note setup": "`crib memory setup`",
    "crib setup": "`crib memory setup`",
    "crib sync": "`crib memory sync`",
    "crib design verify": "`crib design reaffirm`",
    # not "crib snapshot": `GitBacking` uses that exact text as its default git
    # COMMIT MESSAGE, which is not a command reference.
}
_RETIRED_TOOLS = {                          # anywhere an agent or reader can see
    "code_append": "learning_add", "code_edit": "learning_edit",
    "code_forget": "learning_forget", "code_rehome": "learning_rehome",
    "code_learnings": "learning_report", "code_reaffirm": "learning_reaffirm",
    "code_read": "learning_read", "code-append": "learning-add",
    "code-forget": "learning-forget", "reindex(": "note_reindex(",
    # the design facet renamed `verify` → `reaffirm` (one vocabulary with
    # `learning_reaffirm`, and no collision with plan's `verified` STATUS).
    # Surface was a day old: renamed outright, no alias, so the old spelling must
    # not survive in a docstring or an error message either.
    "design_verify": "design_reaffirm",
}


def _strings(tree) -> list[str]:
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


@pytest.mark.parametrize("path", _CRIB_PY, ids=lambda p: p.name)
def test_no_retired_command_names_in_user_facing_strings(path):
    tree = ast.parse(path.read_text())
    for text in _strings(tree):
        for retired, use in _RETIRED_COMMANDS.items():
            assert retired not in text, (
                f"{path.name}: retired command {retired!r} in a user-facing string "
                f"— say {use}:\n    {text.strip()[:160]}")


@pytest.mark.parametrize("path", _CRIB_PY, ids=lambda p: p.name)
def test_no_retired_tool_names_in_strings(path):
    # the whole `learning_*` facet now speaks ONE vocabulary — MCP tool, Crib method
    # and CLI emitter key alike — so the old `code_*` spellings may not reappear at
    # all, not even as an internal key that later leaks into a message.
    for text in _strings(ast.parse(path.read_text())):
        for retired, use in _RETIRED_TOOLS.items():
            # word-boundary-ish: `note_reindex(` must not read as `reindex(`
            assert not re.search(rf"(?<![\w.]){re.escape(retired)}", text), (
                f"{path.name}: retired name {retired!r} in a docstring — say "
                f"{use!r}:\n    {text.strip()[:160]}")
