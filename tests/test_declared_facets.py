"""Declared facets: a facet named in a system facets file is a real facet.

The registry is read once per process, at import, so each case runs crib in a
subprocess with its own CRIB_FACETS_FILE (tests/conftest.py keeps the suite's
own process on the built-ins). Facet registry step 2b; later steps add the
declared facet's verbs (2c/2d) and exercise them end to end (2e).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

WIDGET = """
[widget]
plural = "widgets"
aspects = ["enriched"]

[widget.fields]
color = "VARCHAR"
size = "BIGINT"
"""


def _run(tmp: Path, facets_file: str | None, body: str) -> dict:
    """Run `body` (Python, assigning `out`) in a fresh crib process; return `out`."""
    env = dict(os.environ)
    ff = tmp / "facets.toml"
    if facets_file is not None:
        ff.write_text(facets_file)
    env["CRIB_FACETS_FILE"] = str(ff)
    for k, d in (
        ("CRIB_CONFIG_DIR", "config"),
        ("CRIB_DATA_DIR", "data"),
        ("CRIB_INDEX_DIR", "index"),
    ):
        env[k] = str(tmp / d)
    script = (
        "import json\nout = {}\n" + textwrap.dedent(body) + "\nprint(json.dumps(out))\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp,
    )
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    out["_stderr"] = r.stderr
    return out


def test_a_declared_facet_joins_the_registry_and_everything_derived_from_it(tmp_path):
    out = _run(
        tmp_path,
        WIDGET,
        """
        from crib import chunk, facets, items, notestore, watch
        from crib.store_duckdb import _FACET_TABLES
        out["nouns"] = list(facets.nouns())
        out["error"] = facets.LOAD_ERROR
        out["kind"] = chunk.PILLAR_KIND.get("widgets")
        out["fields"] = dict(items.FACETS["widget"])
        out["table"] = "widget" in _FACET_TABLES
        out["spec"] = [notestore.STORE_SPECS["widget"].name,
                       notestore.STORE_SPECS["widget"].facet]
        out["reserved"] = "widgets/" in notestore.NOTES_SPEC.reserved
        out["watched"] = "widgets" in watch.PILLAR_SEGMENTS
    """,
    )
    assert out["nouns"] == ["note", "design", "plan", "learning", "widget"]
    assert out["error"] is None
    assert out["kind"] == "widget"
    assert out["fields"] == {"color": "VARCHAR", "size": "BIGINT"}
    assert out["table"] is True
    assert out["spec"] == ["widgets", "widget"]
    assert out["reserved"] is True  # notes refuse a `widgets/` relpath
    assert out["watched"] is True


def test_a_declared_facets_items_live_in_its_own_store_and_search_scope(tmp_path):
    out = _run(
        tmp_path,
        WIDGET,
        """
        import asyncio
        from crib.app import Crib
        from crib.config import Config
        from crib.paths import Paths
        from crib.store import InMemoryStore
        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
        res = asyncio.run(crib.store_note(
            "the sprocket assembly torque spec", "Sprocket torque", "p", facet="widget"))
        rel = res["relpath"]
        out["path"] = crib.locate(rel, "p", facet="widget")
        out["widget_hits"] = [h.relpath for h in crib.lookup(
            "sprocket torque", "p", store="widgets")]
        out["note_hits"] = [h.relpath for h in crib.lookup("sprocket torque", "p")]
        out["kinds"] = sorted({it.kind for it in crib.store.items({"project": "p"}).values()})
    """,
    )
    assert "/widgets/" in out["path"]
    assert out["widget_hits"] and out["widget_hits"][0].endswith(".md")
    assert out["note_hits"] == []  # a declared facet can't leak into notes ranking
    assert out["kinds"] == ["widget"]


def test_an_existing_duckdb_store_gains_the_declared_facets_table_at_open(tmp_path):
    pytest.importorskip("duckdb")
    db = tmp_path / "crib.duckdb"
    first = _run(
        tmp_path,
        None,
        f"""
        from crib.store_duckdb import DuckDBStore
        s = DuckDBStore.embedded({str(db)!r})
        out["tables"] = sorted(t for (t,) in s._conn.execute(
            "SELECT table_name FROM duckdb_tables()").fetchall())
        s.close()
    """,
    )
    assert "crib_widget_meta" not in first["tables"]
    after = _run(
        tmp_path,
        WIDGET,
        f"""
        from crib.store_duckdb import DuckDBStore
        s = DuckDBStore.embedded({str(db)!r})
        rows = s._conn.execute(
            "SELECT table_name FROM duckdb_tables()").fetchall()
        out["tables"] = sorted(t for (t,) in rows)
        out["cols"] = [c for (c,) in s._conn.execute(
            "SELECT column_name FROM duckdb_columns() "
            "WHERE table_name = 'crib_widget_meta'").fetchall()]
        s.close()
    """,
    )
    assert "crib_widget_meta" in after["tables"]
    assert after["cols"] == ["id", "color", "size"]
    assert set(first["tables"]) <= set(after["tables"])  # additive: nothing dropped


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("[widget\n", "not valid TOML"),
        ('[note]\nplural = "notes"\n', "built in"),
        ('[routine]\nplural = "plans"\n', "already 'plan'"),
        ('[routine]\nplural = "routines"\naspects = ["magic"]\n', "unknown aspects"),
    ],
)
def test_a_bad_facets_file_warns_and_serves_the_builtins(tmp_path, text, match):
    out = _run(
        tmp_path,
        text,
        """
        from crib import facets
        out["nouns"] = list(facets.nouns())
        out["error"] = facets.LOAD_ERROR
    """,
    )
    assert out["nouns"] == ["note", "design", "plan", "learning"]
    assert match in out["error"]
    assert "[crib] ignoring" in out["_stderr"]


def test_a_declared_facet_gets_its_own_generated_verb_family(tmp_path):
    out = _run(
        tmp_path,
        WIDGET,
        """
        import asyncio
        from crib.app import Crib
        from crib.config import Config
        from crib.paths import Paths
        from crib.server import TOOL_POLICY, build_server
        from crib.store import InMemoryStore
        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
        tools = {t.name: t for t in asyncio.run(build_server(crib).list_tools())}
        out["widget"] = sorted(n for n in tools if n.startswith("widget_"))
        out["lookup"] = tools["widget_lookup"].description
        out["add"] = tools["widget_add"].description
        out["policy"] = TOOL_POLICY["widget_add"]
        out["schemas_match"] = all(
            tools[f"widget_{v}"].parameters == tools[f"note_{v}"].parameters
            for v in ("read", "edit", "lookup", "versions", "move"))
    """,
    )
    assert out["widget"] == sorted(
        f"widget_{v}"
        for v in (
            "lookup",
            "apropos",
            "read",
            "locate",
            "add",
            "append",
            "edit",
            "forget",
            "reindex",
            "versions",
            "restore",
            "distill",
            "elaborate",
            "summarize",
            "import",
            "move",
        )
    )
    assert "Semantic search over widgets" in out["lookup"]
    assert "`widget_locate`" in out["lookup"]
    assert "memory" not in out["lookup"]  # notes' own wording stays with notes
    assert "`widget_append`/`widget_edit`" in out["add"]
    assert out["policy"] == "write"
    assert out["schemas_match"] is True


def test_the_cli_and_mcp_halves_of_the_templates_cover_the_same_verbs():
    from crib import verbs
    from crib.cli import (
        _ANCHORED_CLI,
        _ANCHORED_CLI_ORDER,
        _FACET_CLI,
        _FACET_CLI_ORDER,
    )

    anchored = {t.verb for t in verbs.TEMPLATES if t.aspect == "anchored"}
    other = {t.verb for t in verbs.TEMPLATES if t.aspect != "anchored"}
    assert set(_FACET_CLI) == set(_FACET_CLI_ORDER) == other
    assert set(_ANCHORED_CLI) == set(_ANCHORED_CLI_ORDER) == anchored


def test_a_declared_facet_gets_cli_subcommands_paired_with_its_tools(tmp_path):
    out = _run(
        tmp_path,
        WIDGET,
        """
        import asyncio
        from crib.app import Crib
        from crib.cli import VERBS, _dispatch, build_parser
        from crib.config import Config
        from crib.paths import Paths
        from crib.store import InMemoryStore
        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
        out["rows"] = sorted(k for k in VERBS if k.startswith("widget "))
        out["tools"] = sorted({VERBS[k].tool for k in out["rows"]})
        parser = build_parser()

        def run(argv):
            args = parser.parse_args(argv)
            entry, call = _dispatch(args)
            call.update(entry.inproc)
            m = getattr(crib, entry.crib_method())
            return asyncio.run(m(**call)) if entry.is_async else m(**call)

        res = run(["widget", "add", "the flange gasket clearance", "--title",
                   "Flange gasket", "-p", "p"])
        rel = res["relpath"]
        out["path"] = run(["widget", "locate", rel, "-p", "p"])
        out["read"] = run(["widget", "read", rel, "-p", "p"])
        out["hits"] = [h.relpath for h in run(["widget", "search", "flange gasket", "-p", "p"])]
        out["rendered"] = [h["relpath"] for h in run(
            ["widget", "lookup", "flange gasket", "-p", "p", "--render"])]
        out["note_hits"] = [h.relpath for h in run(["note", "lookup", "flange gasket", "-p", "p"])]
        out["help"] = parser.parse_args(["widget", "read", "x.md"]).cmd
    """,
    )
    assert "widget add" in out["rows"] and "widget store" not in out["rows"]
    assert out["tools"] == sorted(
        f"widget_{v}"
        for v in (
            "lookup",
            "apropos",
            "read",
            "locate",
            "add",
            "append",
            "edit",
            "forget",
            "reindex",
            "versions",
            "restore",
            "distill",
            "elaborate",
            "summarize",
            "import",
            "move",
        )
    )
    assert "/widgets/" in out["path"]
    assert "the flange gasket clearance" in out["read"]
    assert out["hits"] == [out["path"].rsplit("/", 1)[1]]
    assert out["rendered"] == out["hits"]  # --render goes to widget apropos
    assert out["note_hits"] == []
    assert out["help"] == "widget"


def test_every_core_and_enriched_verb_works_on_a_declared_facet(tmp_path):
    """Facet registry step 2e: the declared facet's own MCP tools, end to end,
    through FastMCP's in-memory client; generation stubbed (no LLM)."""
    src = tmp_path / "spec-sheet.md"
    src.write_text("# Spec sheet\n\nimported flange tolerances\n")
    out = _run(
        tmp_path,
        WIDGET,
        f"""
        import asyncio
        from fastmcp import Client
        from crib import generate
        from crib.app import Crib
        from crib.config import Config
        from crib.paths import Paths
        from crib.server import build_server
        from crib.store import InMemoryStore

        async def fake(cfg, prompt, text, **kw):
            return "COMPRESSED " + text.split()[0] if kw.get("purpose") == "distill" \\
                else "sprocketology\\ntorqueish"
        async def fake_structured(*a, **kw):
            return None  # bulk declines: the per-section path runs
        generate.agenerate = fake
        generate.agenerate_structured = fake_structured

        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
        mcp = build_server(crib)

        async def main():
            async with Client(mcp) as c:
                async def call(name, **args):
                    return (await c.call_tool(name, args)).data
                a = await call("widget_add", content="sprocket torque spec", title="Sprocket",
                               project="p")
                rel = a["relpath"]
                # a note with the SAME relpath, to prove the facet stays its own
                await crib.store_note("sprocket torque spec", "Sprocket", "p")
                out["add"] = rel
                out["read"] = await call("widget_read", relpath=rel, project="p")
                out["locate"] = await call("widget_locate", relpath=rel, project="p")
                await call("widget_append", relpath=rel, content="appended line",
                           heading="More", project="p")
                await call("widget_edit", relpath=rel, new_content="edited sprocket body",
                           project="p")
                out["after_edit"] = await call("widget_read", relpath=rel, project="p")
                hits = await call("widget_lookup", query="edited sprocket", project="p")
                out["lookup"] = [(h["relpath"], h["store"]) for h in hits]
                ap = await call("widget_apropos", query="edited sprocket", project="p")
                out["apropos"] = [h["section"] for h in ap]
                vs = await call("widget_versions", relpath=rel, project="p")
                out["versions"] = len(vs)
                await call("widget_restore", relpath=rel, version=vs[-1]["version"],
                           project="p")
                out["restored"] = await call("widget_read", relpath=rel, project="p")
                out["reindex"] = (await call("widget_reindex", project="p"))["files"]
                imp = await call("widget_import", paths=[{str(src)!r}], project="p")
                out["import"] = imp["files"]
                d = await call("widget_distill", relpath=rel, project="p")
                out["distill"] = [d["changed"], await call("widget_read", relpath=rel,
                                                           project="p")]
                e = await call("widget_elaborate", label="keywords", relpath=rel,
                               project="p")
                s = await call("widget_summarize", label="summary", relpath=rel,
                               project="p")
                out["enrich"] = [e["written"], e["total"], s["written"], s["total"]]
                m = await call("widget_move", relpath=rel, to_project="q",
                               to_relpath="moved.md", project="p")
                out["move"] = m["to"]
                await call("widget_forget", relpath="moved.md", project="q")
                out["gone"] = not (crib.pillars["widgets"].root("q") / "moved.md").exists()
                out["note_intact"] = "sprocket torque spec" in crib.read_note(rel, "p")
        asyncio.run(main())
    """,
    )
    rel = out["add"]
    assert "sprocket torque spec" in out["read"]
    assert "/widgets/" in out["locate"]
    assert "edited sprocket body" in out["after_edit"]
    assert out["lookup"] and all(st == "widgets" for _, st in out["lookup"])
    assert out["lookup"][0][0] == rel
    assert any("edited sprocket body" in s for s in out["apropos"])
    assert out["versions"] == 2  # prior versions: before the append, before the edit
    assert "sprocket torque spec" in out["restored"]
    assert out["reindex"] >= 1
    assert out["import"] == ["imported/spec-sheet.md"]
    assert out["distill"][0] is True and "COMPRESSED" in out["distill"][1]
    # the facet's ONE section, not the same-named note's
    assert out["enrich"] == [1, 1, 1, 1]
    assert out["move"] == {"project": "q", "relpath": "moved.md"}
    assert out["gone"] is True
    assert out["note_intact"] is True  # nothing done to the widget touched the note


def test_a_declared_facet_is_its_own_tool_set(tmp_path):
    out = _run(
        tmp_path,
        WIDGET,
        """
        import asyncio
        from crib.app import Crib
        from crib.config import Config
        from crib.paths import Paths
        from crib.server import READ_ONLY_TOOLS, TOOL_SET_NAMES, build_server, tool_set_of
        from crib.store import InMemoryStore
        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())

        def names(tools):
            return sorted(t.name for t in asyncio.run(build_server(crib, tools=tools).list_tools()))

        out["sets"] = list(TOOL_SET_NAMES)
        out["set_of"] = tool_set_of("widget_add")
        out["ro"] = sorted(READ_ONLY_TOOLS["widgets"])
        out["notes_only"] = [n for n in names(["notes"]) if n.startswith("widget_")]
        out["widgets_ro"] = [n for n in names(["widgets:ro"]) if n.startswith("widget_")]
        out["widgets"] = [n for n in names(["widgets"]) if n.startswith("widget_")]
        out["no_notes"] = [n for n in names(["widgets"]) if n.startswith("note_")]
    """,
    )
    assert out["sets"][-1] == "widgets"
    assert out["set_of"] == "widgets"
    ro = [
        "widget_apropos",
        "widget_locate",
        "widget_lookup",
        "widget_read",
        "widget_versions",
    ]
    assert out["ro"] == ro
    assert (
        out["notes_only"] == []
    )  # a declared facet never rides along with another set
    assert out["widgets_ro"] == ro
    assert len(out["widgets"]) == 16
    assert out["no_notes"] == []


REVIEW = """
[review]
plural = "reviews"
aspects = ["anchored"]

[review.fields]
severity = "VARCHAR"
"""


def test_a_declared_anchored_facet_attaches_to_code_beside_learnings(tmp_path):
    """Facet registry step 2f: a declared anchored facet (a review agent's
    findings) gets the anchored family, its items attach to a code symbol
    under their own noun, beside learnings, and it is its own tool set."""
    out = _run(
        tmp_path,
        REVIEW,
        """
        import asyncio
        from fastmcp import Client
        from crib.app import Crib
        from crib.codeindex import SymbolIndex
        from crib.config import Config
        from crib.items import FACETS
        from crib.paths import Paths
        from crib.server import READ_ONLY_TOOLS, build_server
        from crib.store import InMemoryStore

        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
        SymbolIndex(crib.paths.project_dir("p")).write({
            "fqname": "pkg.Mod.foo", "name": "foo", "kind": "function",
            "lang": "python", "content_hash": "aaaa1111", "file": "pkg/mod.py",
            "line": 10, "signature": "def foo():", "description": "does foo",
            "container": [], "calls": [], "called_by": [], "name_terms": ["foo"],
        })
        out["fields"] = list(FACETS["review"])
        out["ro"] = sorted(READ_ONLY_TOOLS["reviews"])
        mcp = build_server(crib)

        async def main():
            async with Client(mcp) as c:
                names = [t.name for t in await c.list_tools()]
                out["tools"] = sorted(n for n in names if n.startswith("review_"))

                async def call(name, **args):
                    return (await c.call_tool(name, args)).data

                a = await call("review_add", symbol="pkg.Mod.foo",
                               text="foo swallows the timeout error", project="p")
                out["add_rel"] = a["relpath"]
                await call("learning_add", symbol="pkg.Mod.foo",
                           text="foo is the retry wrapper", project="p")
                r = await call("review_read", symbol="pkg.Mod.foo", project="p")
                out["read"] = r["body"]
                d = await call("code_dossier", symbol="pkg.Mod.foo", project="p")
                out["dossier"] = {k: (d.get(k) or {}).get("body", "") for k in ("learning", "review")}
                await call("review_edit", symbol="pkg.Mod.foo",
                           new_content="resolved: timeout now re-raised", project="p")
                out["edited"] = (await call("review_read", symbol="pkg.Mod.foo",
                                            project="p"))["body"]
                rep = await call("review_report", project="p")
                out["report"] = [row.get("status") for row in rep]
                await call("review_reaffirm", symbol="pkg.Mod.foo", project="p")
                await call("review_forget", symbol="pkg.Mod.foo", project="p")
                out["after_forget"] = (await call("review_read", symbol="pkg.Mod.foo",
                                                  project="p")).get("found")
                out["learning_kept"] = (await call("learning_read", symbol="pkg.Mod.foo",
                                                   project="p"))["body"]
        asyncio.run(main())
        out["where"] = str(crib.pillars["reviews"].root("p"))
    """,
    )
    assert out["fields"] == [
        "symbol_ref",
        "lang",
        "file",
        "signature",
        "symbol_was",
        "severity",
    ]
    assert out["tools"] == sorted(
        f"review_{v}"
        for v in (
            "add",
            "edit",
            "forget",
            "read",
            "reaffirm",
            "migrate",
            "report",
            "rehome",
        )
    )
    assert out["ro"] == ["review_read", "review_report"]
    assert "foo swallows the timeout error" in out["read"]
    assert "foo swallows the timeout error" in out["dossier"]["review"]
    assert "foo is the retry wrapper" in out["dossier"]["learning"]
    assert "timeout now re-raised" in out["edited"]
    assert out["report"] == ["ok"]
    assert out["after_forget"] is False
    assert "foo is the retry wrapper" in out["learning_kept"]
    assert out["where"].endswith("/reviews")
