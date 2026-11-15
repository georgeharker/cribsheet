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
color = "str"
size = "int"
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


def test_an_existing_duckdb_store_gains_a_declared_facets_table_only_by_sync(tmp_path):
    """A declared facet is conformed EXPLICITLY: an open leaves it pending (no
    table), `facet_sync` creates its table, and nothing else is touched."""
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
    after = _run(
        tmp_path,
        WIDGET,
        f"""
        from crib.store_duckdb import DuckDBStore
        def tables(s):
            return sorted(t for (t,) in s._conn.execute(
                "SELECT table_name FROM duckdb_tables()").fetchall())
        s = DuckDBStore.embedded({str(db)!r})
        out["opened"] = tables(s)
        out["pending"] = s.facet_pending()
        out["plan"] = [p["status"] for p in s.facet_plan()]
        out["synced"] = [p["status"] for p in s.facet_sync()]
        out["tables"] = tables(s)
        out["cols"] = [c for (c,) in s._conn.execute(
            "SELECT column_name FROM duckdb_columns() "
            "WHERE table_name = 'crib_widget_meta'").fetchall()]
        out["after"] = s.facet_pending()
        s.close()
    """,
    )
    assert "crib_widget_meta" not in after["opened"]  # an open created nothing
    assert set(after["pending"]) == {"widget"} and after["pending"][
        "widget"
    ].startswith("new")
    assert after["plan"] == ["new"] and after["synced"] == ["new"]
    assert "crib_widget_meta" in after["tables"]
    assert after["cols"] == ["id", "color", "size"]
    assert set(first["tables"]) <= set(after["tables"])  # additive: nothing dropped
    assert after["after"] == {}


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
            # the verbs without field parameters (widget declares fields)
            for v in ("read", "locate", "forget", "versions", "move"))
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
            "list",  # widget declares fields
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
            "list",  # widget declares fields
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
        "widget_list",
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
    assert len(out["widgets"]) == 17  # the 16 core + enriched verbs, and list
    assert out["no_notes"] == []


REVIEW = """
[review]
plural = "reviews"
aspects = ["anchored"]

[review.fields]
severity = "str"
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
            "list",  # review declares a field
        )
    )
    assert out["ro"] == ["review_list", "review_read", "review_report"]
    assert "foo swallows the timeout error" in out["read"]
    assert "foo swallows the timeout error" in out["dossier"]["review"]
    assert "foo is the retry wrapper" in out["dossier"]["learning"]
    assert "timeout now re-raised" in out["edited"]
    assert out["report"] == ["ok"]
    assert out["after_forget"] is False
    assert "foo is the retry wrapper" in out["learning_kept"]
    assert out["where"].endswith("/reviews")


TICKET = """
[ticket]
plural = "tickets"

[ticket.fields]
changeset_id = "str"
severity = "int"
due = "date"
"""


def test_a_declared_facets_fields_are_set_and_queried_through_its_verbs(tmp_path):
    """Facet registry step 2h: typed field parameters on add/edit, filters on
    lookup, the list verb, and the same through the CLI."""
    out = _run(
        tmp_path,
        TICKET,
        """
        import asyncio
        from fastmcp import Client
        from crib.app import Crib
        from crib.cli import _dispatch, build_parser
        from crib.config import Config
        from crib.paths import Paths
        from crib.server import build_server
        from crib.store import InMemoryStore

        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
        mcp = build_server(crib)

        async def main():
            async with Client(mcp) as c:
                tools = {t.name: t for t in await c.list_tools()}
                props = tools["ticket_add"].inputSchema["properties"]
                out["types"] = {k: props[k]["anyOf"][0]["type"] for k in
                                ("changeset_id", "severity", "due")}
                out["list_props"] = sorted(tools["ticket_list"].inputSchema["properties"])

                async def call(name, **args):
                    return (await c.call_tool(name, args)).data

                a = await call("ticket_add", content="the flange gasket leaks under load",
                               title="Fix flange gasket", project="p",
                               changeset_id="abc123", severity=3, due="2026-12-01")
                b = await call("ticket_add", content="the sprocket squeaks at idle",
                               title="Sprocket noise", project="p",
                               changeset_id="def456", severity=1)
                out["read"] = await call("ticket_read", relpath=a["relpath"], project="p")
                try:
                    await call("ticket_add", content="x", project="p", severity="high")
                    out["refused"] = None
                except Exception as e:
                    out["refused"] = str(e)
                try:
                    await call("ticket_list", project="p",
                               where=[{"field": "changeset_id", "op": "<", "value": "b"}])
                    out["bad_op"] = None
                except Exception as e:
                    out["bad_op"] = str(e)

                def rels(res):
                    return [r["relpath"] for r in res["items"]]

                out["eq"] = rels(await call("ticket_list", project="p", changeset_id="abc123"))
                out["ge"] = rels(await call("ticket_list", project="p", where=[
                    {"field": "severity", "op": ">=", "value": 2}]))
                out["date"] = rels(await call("ticket_list", project="p", where=[
                    {"field": "due", "op": "<", "value": "2027-01-01"}]))
                out["re"] = rels(await call("ticket_list", project="p", where=[
                    {"field": "changeset_id", "op": "re", "value": "^def"}]))
                out["all"] = (await call("ticket_list", project="p"))["total"]
                hits = await call("ticket_lookup", query="gasket sprocket", project="p",
                                  severity=1)
                out["lookup"] = sorted({h["relpath"] for h in hits})
                await call("ticket_edit", relpath=a["relpath"],
                           new_content="the flange gasket leaks under load; reseat it",
                           project="p", severity=5)
                row = (await call("ticket_list", project="p", changeset_id="abc123"))["items"][0]
                out["edited"] = [row["severity"], row["changeset_id"], row["due"]]
                out["a"], out["b"] = a["relpath"], b["relpath"]
        asyncio.run(main())

        parser = build_parser()
        def run(argv):
            args = parser.parse_args(argv)
            entry, call = _dispatch(args)
            call.update(entry.inproc)
            if entry.fields:
                given = {f: call.pop(f) for f in entry.fields if f in call}
                if given:
                    call["fields"] = given
            m = getattr(crib, entry.crib_method())
            return asyncio.run(m(**call)) if entry.is_async else m(**call)
        c = run(["ticket", "add", "the valve sticks when cold", "--title", "Valve",
                 "-p", "p", "--severity", "4", "--changeset-id", "ghi789"])
        out["cli_list"] = [r["relpath"] for r in run(
            ["ticket", "list", "-p", "p", "--where", "severity>=4"])["items"]]
        out["cli_eq"] = [r["relpath"] for r in run(
            ["ticket", "list", "-p", "p", "--changeset-id", "ghi789"])["items"]]
        out["c"] = c["relpath"]
    """,
    )
    a, b, c = out["a"], out["b"], out["c"]
    assert out["types"] == {
        "changeset_id": "string",
        "severity": "integer",
        "due": "string",
    }
    assert {"changeset_id", "severity", "due", "where", "k"} <= set(out["list_props"])
    assert "changeset_id: abc123" in out["read"] and "severity: 3" in out["read"]
    assert out["refused"] and "integer" in out["refused"]  # refused at the wire
    assert out["bad_op"] and "op '<' isn't one of" in out["bad_op"]
    assert out["eq"] == [a]
    assert out["ge"] == [a]
    assert out["date"] == [a]  # b has no due date: matches no condition
    assert out["re"] == [b]
    assert out["all"] == 2
    assert out["lookup"] == [b]  # the search ran over severity=1 only
    assert out["edited"] == [5, "abc123", "2026-12-01"]  # only severity changed
    assert out["cli_list"] == sorted([a, c])
    assert out["cli_eq"] == [c]


def test_an_anchored_facets_fields_list_by_symbol(tmp_path):
    out = _run(
        tmp_path,
        REVIEW,
        """
        import asyncio
        from crib.app import Crib
        from crib.codeindex import SymbolIndex
        from crib.config import Config
        from crib.paths import Paths
        from crib.store import InMemoryStore

        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
        SymbolIndex(crib.paths.project_dir("p")).write({
            "fqname": "pkg.Mod.foo", "name": "foo", "kind": "function",
            "lang": "python", "content_hash": "aaaa1111", "file": "pkg/mod.py",
            "line": 10, "signature": "def foo():", "description": "does foo",
            "container": [], "calls": [], "called_by": [], "name_terms": ["foo"],
        })
        asyncio.run(crib.learning_add("pkg.Mod.foo", "swallows the timeout", project="p",
                                      facet="review", fields={"severity": "high"}))
        res = crib.list_items("p", facet="review", fields={"severity": "high"})
        out["rows"] = [(r["symbol"], r["severity"]) for r in res["items"]]
        out["none"] = crib.list_items("p", facet="review", fields={"severity": "low"})["total"]
    """,
    )
    [(symbol, severity)] = out["rows"]
    assert "pkg/mod.py" in symbol and symbol.endswith("foo")
    assert severity == "high"
    assert out["none"] == 0


def test_field_queries_on_the_duckdb_store(tmp_path):
    """The same field filters over the embedded DuckDB store, where an int
    field is a BIGINT column (the in-memory store keeps what it is given)."""
    pytest.importorskip("duckdb")
    out = _run(
        tmp_path,
        TICKET,
        """
        import asyncio
        from crib.app import Crib
        from crib.config import Config
        from crib.paths import Paths
        from crib.store_duckdb import DuckDBStore

        paths = Paths.resolve().ensure()
        store = DuckDBStore.embedded(paths.duckdb_file)
        store.facet_sync()  # a declared facet is conformed explicitly
        crib = Crib(paths, Config(), store)
        made = {}
        for title, sev, cs, due in (("Gasket leak", 3, "abc123", "2026-12-01"),
                                     ("Sprocket noise", 1, "def456", None),
                                     ("Valve sticks", 12, "abc999", "2027-02-01")):
            fields = {"severity": sev, "changeset_id": cs}
            if due:
                fields["due"] = due
            r = asyncio.run(crib.store_note(f"{title} body", title, "p",
                                            facet="ticket", fields=fields))
            made[title] = r["relpath"]

        def rels(**kw):
            return [r["relpath"] for r in crib.list_items("p", facet="ticket", **kw)["items"]]

        out["made"] = made
        # 12 >= 3 numerically (a text compare would put "12" before "3")
        out["ge3"] = rels(where=[{"field": "severity", "op": ">=", "value": 3}])
        out["lt3"] = rels(where=[{"field": "severity", "op": "<", "value": 3}])
        out["re"] = rels(where=[{"field": "changeset_id", "op": "re", "value": "^abc"}])
        out["date"] = rels(where=[{"field": "due", "op": ">", "value": "2026-12-31"}])
        out["both"] = rels(fields={"changeset_id": "abc123"},
                           where=[{"field": "severity", "op": "==", "value": 3}])
        out["lookup"] = sorted({h.relpath for h in crib.lookup(
            "body", "p", store="tickets",
            where=[{"field": "severity", "op": ">", "value": 2}])})
        out["col"] = store._conn.execute(
            "SELECT DISTINCT typeof(severity) FROM crib_ticket_meta").fetchall()
        store.close()
    """,
    )
    m = out["made"]
    assert out["ge3"] == sorted([m["Gasket leak"], m["Valve sticks"]])
    assert out["lt3"] == [m["Sprocket noise"]]
    assert out["re"] == sorted([m["Gasket leak"], m["Valve sticks"]])
    assert out["date"] == [m["Valve sticks"]]
    assert out["both"] == [m["Gasket leak"]]
    assert out["lookup"] == sorted([m["Gasket leak"], m["Valve sticks"]])
    assert out["col"] == [["BIGINT"]]


def test_a_declared_facets_disabled_verbs_are_absent_everywhere(tmp_path):
    out = _run(
        tmp_path,
        """
[memo]
plural = "memos"
aspects = ["enriched"]
disable = ["import", "move", "distill"]
""",
        """
        import asyncio
        from crib.app import Crib
        from crib.cli import VERBS, build_parser
        from crib.config import Config
        from crib.paths import Paths
        from crib.server import READ_ONLY_TOOLS, build_server
        from crib.store import InMemoryStore
        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
        out["tools"] = sorted(t.name for t in asyncio.run(build_server(crib).list_tools())
                              if t.name.startswith("memo_"))
        out["rows"] = sorted(k for k in VERBS if k.startswith("memo "))
        try:
            build_parser().parse_args(["memo", "move", "x.md"])
            out["cli_move"] = True
        except SystemExit:
            out["cli_move"] = False
        out["ro"] = sorted(READ_ONLY_TOOLS["memos"])
    """,
    )
    gone = {"memo_import", "memo_move", "memo_distill"}
    assert not gone & set(out["tools"])
    assert "memo_add" in out["tools"] and "memo_summarize" in out["tools"]
    assert len(out["tools"]) == 13  # 16 core + enriched, less 3
    assert not {"memo import", "memo move", "memo distill"} & set(out["rows"])
    assert out["cli_move"] is False
    assert "memo_lookup" in out["ro"]


# ── explicit conformance: `crib facets sync` ────────────────────────────────
# Each step is its own crib process (the registry is read once per process),
# all on one embedded DuckDB store under tmp_path.

_CONFORM_PRELUDE = """
        import asyncio, contextlib, io
        from crib.app import Crib
        from crib.cli import main
        def cli(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = main(list(argv))
            return code, buf.getvalue()
        def refused(fn):
            try:
                fn()
            except Exception as e:
                return str(e)
            return None
"""


def test_a_new_declared_facet_is_pending_until_sync_apply(tmp_path):
    pytest.importorskip("duckdb")
    out = _run(
        tmp_path,
        TICKET,
        _CONFORM_PRELUDE
        + """
        crib = Crib.open()
        out["pending"] = crib.pending_facets
        out["add"] = refused(lambda: asyncio.run(crib.store_note("x", "X", "p", facet="ticket")))
        out["lookup"] = refused(lambda: crib.lookup("x", "p", store="tickets"))
        out["list"] = refused(lambda: crib.list_items("p", facet="ticket"))
        out["note"] = asyncio.run(crib.store_note("notes still work", "N", "p"))["relpath"]
        crib.close()
        out["dry"] = cli("facets", "sync")
        out["still"] = sorted(Crib.open().pending_facets)
        out["apply"] = cli("facets", "sync", "--apply")
        crib = Crib.open()
        out["after"] = crib.pending_facets
        r = asyncio.run(crib.store_note("works now", "T", "p", facet="ticket",
                                        fields={"severity": 2}))
        out["listed"] = [i["severity"] for i in crib.list_items("p", facet="ticket")["items"]]
        out["show"] = cli("facets", "show")
    """,
    )
    assert set(out["pending"]) == {"ticket"}
    for verb in ("add", "lookup", "list"):
        assert "isn't ready in this store" in out[verb], verb
        assert "crib facets sync --apply" in out[verb], verb
    assert out["note"].endswith(".md")
    code, text = out["dry"]
    assert code == 0 and "new" in text and "ticket" in text and "dry run" in text
    assert out["still"] == ["ticket"]  # a dry run changes nothing
    code, text = out["apply"]
    assert code == 0 and "applied" in text and "restart the crib daemon" in text
    assert out["after"] == {}
    assert out["listed"] == [2]
    code, text = out["show"]
    assert "ticket  (declared, current)" in text and "note  (built-in, current)" in text
    assert "fields changeset_id (str), severity (int), due (date)" in text


TICKET_CHANGED = (
    TICKET.replace('severity = "int"', 'severity = "str"') + 'owner = "str"\n'
)


def test_a_changed_facet_is_pending_and_sync_rebuilds_it_from_the_files(tmp_path):
    pytest.importorskip("duckdb")
    first = _run(
        tmp_path,
        TICKET,
        _CONFORM_PRELUDE
        + """
        cli("facets", "sync", "--apply")
        crib = Crib.open()
        r = asyncio.run(crib.store_note("gasket leak", "Gasket", "p", facet="ticket",
                                        fields={"severity": 3, "changeset_id": "abc"}))
        out["rel"] = r["relpath"]
        out["path"] = crib.locate(r["relpath"], "p", facet="ticket")
    """,
    )
    # a hand edit adds the new field's value to the file (the files are the truth)
    path = Path(first["path"])
    text = path.read_text()
    path.write_text(
        text.replace("changeset_id: abc", "changeset_id: abc\nowner: george")
    )
    out = _run(
        tmp_path,
        TICKET_CHANGED,
        _CONFORM_PRELUDE
        + """
        crib = Crib.open()
        out["pending"] = crib.pending_facets
        out["note"] = asyncio.run(crib.store_note("notes still work", "N", "p"))["relpath"]
        crib.close()
        out["dry"] = cli("facets", "sync")
        out["apply"] = cli("facets", "sync", "--apply")
        crib = Crib.open()
        rows = crib.list_items("p", facet="ticket", fields={"owner": "george"})["items"]
        out["rows"] = [(r["relpath"], r["severity"], r["owner"]) for r in rows]
        out["cols"] = [c for (c,) in crib.store._conn.execute(
            "SELECT column_name FROM duckdb_columns() "
            "WHERE table_name = 'crib_ticket_meta'").fetchall()]
    """,
    )
    why = out["pending"]["ticket"]
    assert why.startswith("changed") and "fields added owner" in why
    assert "retyped severity BIGINT->VARCHAR" in why
    assert out["note"].endswith(".md")  # other facets are unaffected
    assert "changed" in out["dry"][1] and "dry run" in out["dry"][1]
    assert "re-read 1 file" in out["apply"][1]
    assert out["rows"] == [[first["rel"], "3", "george"]]  # severity now a str
    assert out["cols"] == ["id", "changeset_id", "severity", "due", "owner"]


def test_a_removed_facet_is_dropped_by_sync_and_its_files_are_kept(tmp_path):
    pytest.importorskip("duckdb")
    first = _run(
        tmp_path,
        TICKET,
        _CONFORM_PRELUDE
        + """
        cli("facets", "sync", "--apply")
        crib = Crib.open()
        r = asyncio.run(crib.store_note("gasket leak", "Gasket", "p", facet="ticket"))
        out["path"] = crib.locate(r["relpath"], "p", facet="ticket")
    """,
    )
    out = _run(
        tmp_path,
        "",  # the facet is gone from the facets file,
        _CONFORM_PRELUDE
        + """
        crib = Crib.open()
        out["plan"] = [(p["facet"], p["status"]) for p in crib.store.facet_plan()]
        crib.close()
        out["apply"] = cli("facets", "sync", "--apply")
        crib = Crib.open()
        ex = crib.store._conn.execute
        out["tables"] = [t for (t,) in ex(
            "SELECT table_name FROM duckdb_tables() WHERE table_name = 'crib_ticket_meta'"
        ).fetchall()]
        out["items"] = ex("SELECT count(*) FROM crib_items WHERE kind = 'ticket'").fetchone()[0]
        out["chunks"] = ex("SELECT count(*) FROM crib_chunks WHERE kind = 'ticket'").fetchone()[0]
        out["plan_after"] = [p["status"] for p in crib.store.facet_plan()]
    """,
    )
    assert out["plan"] == [["ticket", "removed"]]
    assert "removed" in out["apply"][1]
    assert out["tables"] == [] and out["items"] == 0 and out["chunks"] == 0
    assert out["plan_after"] == []
    assert Path(first["path"]).exists()  # the files are never touched


def test_the_watcher_does_not_index_a_pending_facets_files(tmp_path):
    pytest.importorskip("duckdb")
    out = _run(
        tmp_path,
        TICKET,
        _CONFORM_PRELUDE
        + """
        crib = Crib.open()
        d = crib.paths.project_dir("p")
        asyncio.run(crib._on_fs_change("p", "tickets", "stray.md"))
        out["items"] = sorted(i.relpath for i in crib.store.items({"project": "p"}).values())
    """,
    )
    assert out["items"] == []  # not indexed as a note (or anything)
