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
optional = ["color", "size"]

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
optional = ["severity"]

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
optional = ["changeset_id", "severity", "due"]

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
        out["keep"] = cli("facets", "sync", "--apply")  # no --drop: kept
        crib = Crib.open()
        out["kept_items"] = crib.store._conn.execute(
            "SELECT count(*) FROM crib_items WHERE kind = 'ticket'").fetchone()[0]
        crib.close()
        out["apply"] = cli("facets", "sync", "--apply", "--drop")
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
    assert "kept" in out["keep"][1] and "--drop" in out["keep"][1]
    assert out["kept_items"] == 1  # nothing dropped without --drop
    assert "removed" in out["apply"][1]
    assert out["tables"] == [] and out["items"] == 0 and out["chunks"] == 0
    assert out["plan_after"] == []
    assert Path(first["path"]).exists()  # the files are never touched


def test_sync_refuses_while_the_facets_file_fails_to_load(tmp_path):
    """A typo makes the registry fall back to the built-ins, so every declared
    facet would look removed: sync refuses rather than drop them (review fix 2)."""
    pytest.importorskip("duckdb")
    _run(tmp_path, TICKET, _CONFORM_PRELUDE + """
        cli("facets", "sync", "--apply")
        crib = Crib.open()
        asyncio.run(crib.store_note("gasket leak", "Gasket", "p", facet="ticket"))
    """)
    out = _run(
        tmp_path,
        TICKET + "\nbogus_key = 1\n",
        _CONFORM_PRELUDE + """
        from crib import facets
        out["error"] = facets.LOAD_ERROR
        out["apply"] = cli("facets", "sync", "--apply", "--drop")
        crib = Crib.open()
        out["store"] = refused(lambda: crib.store.facet_sync(drop=True))
        out["items"] = crib.store._conn.execute(
            "SELECT count(*) FROM crib_items WHERE kind = 'ticket'").fetchone()[0]
    """,
    )
    assert out["error"]
    assert out["apply"][0] == 1 and "didn't load" in out["apply"][1]
    assert "didn't load" in out["store"]
    assert out["items"] == 1


def test_a_shared_store_needs_shared_to_change_a_facet(tmp_path):
    """On the crib DuckDB server one machine's sync changes the store for every
    machine: --apply alone refuses, --apply --shared goes ahead (review fix 2)."""
    pytest.importorskip("duckdb")
    out = _run(
        tmp_path,
        TICKET,
        _CONFORM_PRELUDE + """
        from crib.store_duckdb import DuckDBStore
        DuckDBStore.SHARED = True  # stand in for a QuackStore
        out["dry"] = cli("facets", "sync")
        out["plain"] = cli("facets", "sync", "--apply")
        crib = Crib.open()
        out["store_gate"] = refused(lambda: crib.store.facet_sync())
        out["pending"] = sorted(crib.store.facet_pending())
        crib.close()
        out["shared"] = cli("facets", "sync", "--apply", "--shared")
        crib = Crib.open()
        out["after"] = sorted(crib.store.facet_pending())
    """,
    )
    assert "SHARED" in out["dry"][1] and "--shared" in out["dry"][1]
    assert out["plain"][0] == 1 and "refusing" in out["plain"][1]
    assert "shared" in out["store_gate"]  # the store itself refuses, too
    assert out["pending"] == ["ticket"]
    assert out["shared"][0] == 0 and "every machine" in out["shared"][1]
    assert out["after"] == []


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


# ── locations phase 1: a declared facet mapped from a repo (`.crib` facets:) ──

ROUTINE = """
[routine]
plural = "routines"
"""


def _repo(tmp_path: Path, mapping: str, files: dict[str, str]) -> Path:
    repo = tmp_path / "toucan"
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    (repo / ".crib").write_text(f"project: p\nfacets:\n  routine: {mapping}\n")
    return repo


_MAPPED = """
        import asyncio
        from pathlib import Path
        from crib.app import Crib
        from crib.config import Config, CribLink
        from crib.paths import Paths
        from crib.store import InMemoryStore
        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
        link = CribLink.find(Path(REPO))
        synced = asyncio.run(crib._sync_mappings("p", link))
        def refused(fn):
            try:
                fn()
            except Exception as e:
                return str(e)
            return None
"""


def test_a_writable_mapped_facet_lives_in_the_repo(tmp_path):
    repo = _repo(
        tmp_path,
        "{dir: workflows, glob: '*.md'}",
        {
            "workflows/morning-briefing.md": "---\nname: morning\nschedule: 7am\n---\nbrief me\n",
            "workflows/sub/nested.md": "not picked: the glob is top-level\n",
            "workflows/notes.txt": "not markdown\n",
        },
    )
    out = _run(
        tmp_path,
        ROUTINE,
        _MAPPED.replace("REPO", repr(str(repo)))
        + """
        out["synced"] = synced
        items = crib.store.items({"project": "p", "kind": "routine"})
        out["rels"] = sorted(i.relpath for i in items.values())
        out["stub"] = Path(crib.project_paths("p").config_file).read_text()
        out["root"] = str(crib.pillars["routines"].root("p"))
        r = asyncio.run(crib.store_note("check the weather", "Weather check", "p",
                                        facet="routine"))
        out["added"] = r["relpath"]
        asyncio.run(crib.edit_note(r["relpath"], "check the weather twice", "p",
                                   facet="routine"))
        out["ring"] = sorted(p.name for p in (Path(out["root"]) / ".versions").iterdir())
        out["hits"] = [h.relpath for h in crib.lookup("weather", "p", store="routines")]
        asyncio.run(crib.reindex(None, "p", facet="routine"))
        out["after_ring"] = sorted(i.relpath for i in
                                   crib.store.items({"project": "p", "kind": "routine"}).values())
    """,
    )
    assert out["synced"]["routine"]["files"] == 1
    assert out["rels"] == ["morning-briefing.md"]  # the glob picks the facet's files
    assert "mappings:" in out["stub"] and "routine:" in out["stub"]
    assert out["root"] == str(repo / "workflows")
    assert (repo / "workflows" / out["added"]).exists()  # written into the repo
    assert "id:" in (repo / "workflows" / "morning-briefing.md").read_text()  # stamped
    assert ".gitignore" in out["ring"]  # the ring lives beside the files, ignored
    # the ring's own files are never the facet's items
    assert out["after_ring"] == sorted([out["added"], "morning-briefing.md"])
    assert out["hits"] == [out["added"]]


def test_a_read_only_derived_mapping_is_indexed_and_never_written(tmp_path):
    body = "---\nname: morning\n---\nbrief me about the weather\n"
    repo = _repo(
        tmp_path,
        "{dir: workflows, glob: '*.md', read_only: true, ids: derived}",
        {"workflows/morning.md": body},
    )
    out = _run(
        tmp_path,
        ROUTINE,
        _MAPPED.replace("REPO", repr(str(repo)))
        + """
        out["hits"] = [h.relpath for h in crib.lookup("weather", "p", store="routines")]
        out["add"] = refused(lambda: asyncio.run(
            crib.store_note("x", "X", "p", facet="routine")))
        out["edit"] = refused(lambda: asyncio.run(
            crib.edit_note("morning.md", "changed", "p", facet="routine")))
        out["forget"] = refused(lambda: asyncio.run(
            crib.forget("morning.md", "p", facet="routine")))
    """,
    )
    assert out["hits"] == ["morning.md"]
    assert (repo / "workflows" / "morning.md").read_text() == body  # not a byte changed
    for verb in ("add", "edit", "forget"):
        assert "read-only location" in out[verb], verb


def test_release_leaves_a_read_only_mapping_in_the_repo(tmp_path):
    """A read-only (or derived) mapping's files are the repo's: release moves
    nothing and keeps the mapping, rather than carrying them out of the
    checkout (review fix 1)."""
    body = "---\nname: morning\n---\nbrief me about the weather\n"
    repo = _repo(
        tmp_path,
        "{dir: workflows, glob: '*.md', read_only: true, ids: derived}",
        {"workflows/morning.md": body},
    )
    out = _run(
        tmp_path,
        ROUTINE,
        _MAPPED.replace("REPO", repr(str(repo)))
        + """
        out["res"] = asyncio.run(crib.project_release("p"))
        out["mapped"] = sorted(crib.project_paths("p").mappings)
        home = crib.project_paths("p").home_dir("routines")
        out["home"] = sorted(str(f) for f in home.rglob("*.md")) if home.is_dir() else []
        out["hits"] = [h.relpath for h in crib.lookup("weather", "p", store="routines")]
    """,
    )
    assert (repo / "workflows" / "morning.md").read_text() == body
    assert out["home"] == []
    assert "mapped" not in out["res"]
    assert "read-only" in out["res"]["kept"]["routine"]["why"]
    assert out["mapped"] == ["routines"]  # still recorded
    assert out["hits"] == ["morning.md"]  # still indexed


def test_adopt_refuses_to_move_items_into_a_read_only_mapping(tmp_path):
    repo = _repo(tmp_path, "{dir: workflows, read_only: true}", {"workflows/x.md": "x\n"})
    out = _run(
        tmp_path,
        ROUTINE,
        """
        import asyncio
        from pathlib import Path
        from crib.app import Crib
        from crib.config import Config
        from crib.paths import Paths
        from crib.store import InMemoryStore
        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
        a = asyncio.run(crib.store_note("x", "Local", "p", facet="routine"))
        try:
            asyncio.run(crib.project_adopt(cwd=Path(REPO)))
            out["err"] = None
        except Exception as e:
            out["err"] = str(e)
        home = crib.project_paths("p").home_dir("routines")
        out["still"] = (home / a["relpath"]).exists()
    """.replace("REPO", repr(str(repo))),
    )
    assert "is read-only" in out["err"]
    assert out["still"]
    assert sorted(f.name for f in (repo / "workflows").iterdir()) == ["x.md"]


def test_a_read_only_stamped_mapping_skips_unstamped_files(tmp_path):
    repo = _repo(
        tmp_path,
        "{dir: workflows, read_only: true}",
        {
            "workflows/stamped.md": "---\nid: 01M7EZZZZZZZZZZZZZZZZZZZZZ\n---\nhas an id\n",
            "workflows/fresh.md": "no id yet\n",
        },
    )
    out = _run(
        tmp_path,
        ROUTINE,
        _MAPPED.replace("REPO", repr(str(repo)))
        + """
        out["synced"] = synced
        from crib.doctor import diagnose
        out["doctor"] = [(f["check"], f["item"]) for f in diagnose(crib, "p")["findings"]
                         if f["check"] == "not yet stamped"]
        out["rels"] = sorted(i.relpath for i in
                             crib.store.items({"project": "p", "kind": "routine"}).values())
    """,
    )
    assert out["rels"] == ["stamped.md"]
    assert out["synced"]["routine"]["unstamped"] == ["fresh.md"]
    assert "id:" not in (repo / "workflows" / "fresh.md").read_text()  # never stamped
    assert out["doctor"] == [["not yet stamped", "fresh.md"]]


def test_a_mapped_dir_is_crib_owned_and_watched(tmp_path):
    from crib.config import CribLink
    from crib.watch import decode_mapped

    repo = _repo(
        tmp_path, "{dir: workflows}", {"workflows/a.md": "x\n", "docs/b.md": "y\n"}
    )
    link = CribLink.find(repo)
    assert link is not None and set(link.facets) == set()  # routine isn't declared HERE
    # (this process serves the built-ins only: the facet is declared in the
    # subprocess's facets file; the ownership rule is checked there too)
    out = _run(
        tmp_path,
        ROUTINE,
        f"""
        from pathlib import Path
        from crib.config import CribLink
        link = CribLink.find(Path({str(repo)!r}))
        out["owned"] = link.in_store(Path({str(repo / "workflows" / "a.md")!r}))
        out["doc"] = link.in_store(Path({str(repo / "docs" / "b.md")!r}))
    """,
    )
    assert out["owned"] is True and out["doc"] is False
    roots = {("p", "routines"): repo / "workflows"}
    assert decode_mapped(roots, str(repo / "workflows" / "deep" / "c.md")) == (
        "p",
        "routines",
        "deep/c.md",
    )
    assert decode_mapped(roots, str(repo / "workflows" / ".versions" / "c.md")) is None
    assert decode_mapped(roots, str(repo / "docs" / "b.md")) is None


def test_a_moved_mapping_rereads_the_new_dir_and_drops_the_old(tmp_path):
    repo = _repo(
        tmp_path,
        "{dir: workflows}",
        {"workflows/a.md": "alpha\n", "routines2/b.md": "beta\n"},
    )
    out = _run(
        tmp_path,
        ROUTINE,
        _MAPPED.replace("REPO", repr(str(repo)))
        + """
        out["before"] = sorted(i.relpath for i in
                               crib.store.items({"project": "p", "kind": "routine"}).values())
        Path(REPO, ".crib").write_text("project: p\\nfacets:\\n  routine: {dir: routines2}\\n")
        # its stamped items are still in workflows/: repointing would strand them
        asyncio.run(crib._sync_mappings("p", CribLink.find(Path(REPO))))
        out["held"] = sorted(i.relpath for i in
                             crib.store.items({"project": "p", "kind": "routine"}).values())
        Path(REPO, "workflows", "a.md").unlink()  # meant to start afresh there
        again = asyncio.run(crib._sync_mappings("p", CribLink.find(Path(REPO))))
        out["after"] = sorted(i.relpath for i in
                              crib.store.items({"project": "p", "kind": "routine"}).values())
        out["unchanged"] = asyncio.run(crib._sync_mappings("p", CribLink.find(Path(REPO))))
    """.replace("REPO", repr(str(repo))),
    )
    assert out["before"] == ["a.md"]
    assert out["held"] == ["a.md"]  # the move is held while a.md would be stranded
    assert "was moved" in out["_stderr"]
    assert out["after"] == ["b.md"]
    assert out["unchanged"] == {}  # nothing changed: nothing written


def test_crib_facets_init_and_show_toml(tmp_path):
    out = _run(
        tmp_path,
        None,
        """
        import contextlib, io, os
        from pathlib import Path
        from crib.cli import main
        def cli(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = main(list(argv))
            return code, buf.getvalue()
        f = Path(os.environ["CRIB_FACETS_FILE"])
        if f.exists():
            f.unlink()
        out["init"] = cli("facets", "init")
        out["written"] = f.read_text().startswith("# crib's system facets file")
        out["again"] = cli("facets", "init")
        out["force"] = cli("facets", "init", "--force")[0]
        out["toml"] = cli("facets", "show", "--toml")[1]
    """,
    )
    assert out["init"][0] == 0 and "wrote" in out["init"][1]
    assert out["written"] is True
    assert out["again"][0] == 1 and "already exists" in out["again"][1]
    assert out["force"] == 0
    assert "[note]" in out["toml"] and '[note.verbs]\nadd = "store"' in out["toml"]


# ── graph step 7: a declared graph facet with its own statuses ─────────────

INCIDENT = """
[incident]
plural = "incidents"
aspects = ["links", "status", "progress", "ordered"]

[incident.status]
values = { open = "open", mitigated = "claimed", resolved = "done", shelved = "paused", wontfix = "dropped" }
initial = "open"
transitions = { shelve = "shelved", wontfix = "wontfix" }

[incident.edges]
incident = "follows"
"*" = "informed_by"
"""


RFC = """
[rfc]
plural = "rfcs"
aspects = ["links", "status"]

[rfc.status]
values = { draft = "gated", accepted = "open", building = "claimed", shipped = "done", rejected = "dropped" }
initial = "draft"
transitions = { accept = "accepted", build = "building", ship = "shipped", redraft = "draft", reject = "rejected" }
"""


def test_a_facet_whose_commands_cannot_be_built_is_skipped_not_fatal(tmp_path):
    """The CLI tries a declared facet's commands on a scratch parser: one that
    fails (a clash the registry missed) loses its commands with a warning;
    every other command still works (review fix 4)."""
    out = _run(
        tmp_path,
        WIDGET,
        """
        import argparse, contextlib, io
        from crib import cli
        real = cli._facet_verb_parsers
        def boom(fsub, spec, proj):
            if spec.noun == "widget":
                raise argparse.ArgumentError(None, "conflicting option string: --x")
            return real(fsub, spec, proj)
        cli._facet_verb_parsers = boom
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            p = cli.build_parser()
        sp = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
        out["commands"] = sorted(sp.choices)
        out["err"] = err.getvalue()
    """,
    )
    assert "widget" not in out["commands"]
    assert {"note", "design", "plan", "serve"} <= set(out["commands"])
    assert "facet 'widget' has no CLI commands" in out["err"]


TASK = """
[task]
plural = "tasks"
aspects = ["links", "status", "progress"]

[task.status]
values = { todo = "open", doing = "claimed", done = "done" }
"""


def test_links_between_a_facets_own_items_block_without_an_edges_table(tmp_path):
    """No [edges] table: a progress facet's own links are `follows` (B waits
    for A), not `informed_by` (never blocks) (review fix 7)."""
    out = _run(
        tmp_path,
        TASK,
        """
        import asyncio
        from crib import facets
        from crib.app import Crib
        from crib.config import Config
        from crib.paths import Paths
        from crib.store import InMemoryStore
        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
        out["edges"] = dict(facets.by_noun("task").edges)
        a = asyncio.run(crib.plan_add("Migrate", "move the data", project="p", facet="task"))
        b = asyncio.run(crib.plan_add("Switch over", "point clients at it",
                                      deps=[a["relpath"]], project="p", facet="task"))
        out["kind"] = list(b["dep_kinds"].values())
        nxt = crib.plan_next(project="p", facet="task")
        out["next"] = [i["title"] for i in nxt["items"]]
    """,
    )
    assert out["edges"]["task"] == "follows"
    assert out["kind"] == ["follows"]
    assert out["next"] == ["Migrate"]  # Switch over waits


def test_a_progress_facet_that_reports_taint_reads_both(tmp_path):
    """read on a facet with progress AND report_taint: the working-set read
    (blocked, blocked_by) and why it is stale (reasons, causes) (factory F3)."""
    out = _run(
        tmp_path,
        TASK.replace('plural = "tasks"', 'plural = "tasks"\nreport_taint = true'),
        """
        import asyncio
        from crib.app import Crib
        from crib.config import Config
        from crib.paths import Paths
        from crib.server import build_server
        from crib.store import InMemoryStore
        from fastmcp import Client
        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
        mcp = build_server(crib)
        async def main():
            async with Client(mcp) as c:
                call = lambda n, **a: c.call_tool(n, a)
                a = (await call("task_add", title="Base", content="the base", project="p")).data
                b = (await call("task_add", title="On top", content="needs base",
                                deps=[a["relpath"]], project="p")).data
                await call("task_edit", ref=a["relpath"], new_content="the base, changed",
                           project="p")
                r = (await call("task_read", ref=b["relpath"], project="p")).data
                out["keys"] = sorted(k for k in ("blocked", "blocked_by", "reasons", "causes")
                                     if k in r)
                out["tainted"] = r["tainted"]
        asyncio.run(main())
    """,
    )
    assert out["keys"] == ["blocked", "blocked_by", "causes", "reasons"]
    assert out["tainted"] is True


def test_a_declared_facets_hints_name_its_own_tools_and_statuses(tmp_path):
    """The engine's hints (next steps, refusals, blockers) name the facet's own
    verbs and statuses, never plan_*/design_* (review fix 8)."""
    out = _run(
        tmp_path,
        INCIDENT + RFC,
        """
        import asyncio, json
        from fastmcp import Client
        from crib.app import Crib
        from crib.config import Config
        from crib.paths import Paths
        from crib.server import build_server
        from crib.store import InMemoryStore
        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
        mcp = build_server(crib)
        texts = []
        async def main():
            async with Client(mcp) as c:
                async def call(name, **a):
                    try:
                        r = (await c.call_tool(name, a)).data
                    except Exception as e:
                        r = {"error": str(e)}
                    texts.append(json.dumps(r, default=str))
                    return r
                a = await call("incident_add", title="Failover", content="x", project="p")
                b = await call("incident_add", title="Postmortem", content="y", project="p")
                c3 = await call("incident_add", title="Flaky", content="z", project="p")
                nxt = await call("incident_next", project="p")
                out["next"] = nxt["items"][0]["next"]
                s = await call("incident_shelve", ref=b["relpath"], why="later", project="p")
                out["shelve"] = s["next"]
                w = await call("incident_wontfix", ref=c3["relpath"], why="noise", project="p")
                out["wontfix"] = w["next"]
                await call("incident_add", project="p")  # neither title nor items
                await call("incident_move", ref=a["relpath"], project="p")
                d = await call("rfc_add", title="Async API", content="go async", project="p")
                r = await call("rfc_read", ref=d["relpath"], project="p")
                out["draft"] = r["next"]
                t = await call("incident_add", title="Rollout", content="ship it",
                               rests_on=[d["relpath"]], project="p")  # a draft gates it
                rd = await call("incident_read", ref=t["relpath"], project="p")
                out["blocker"] = [x["status"] for x in rd["blocked_by"]]
        asyncio.run(main())
        out["texts"] = texts
    """,
    )
    assert out["next"].startswith("starting it? `incident_status ")
    assert "mitigated" in out["next"] and "resolved" in out["next"]
    assert "`incident_status " in out["shelve"] and " open`" in out["shelve"]
    assert "`incident_add`" in out["wontfix"] and "`incident_status <ref> open`" in out["wontfix"]
    assert "`rfc_accept " in out["draft"] and "DRAFT" in out["draft"]
    assert out["blocker"] == ["draft — `rfc_accept` it"]
    joined = "\n".join(out["texts"])
    assert "every incident needs a title" in joined
    assert "incident_move needs after" in joined
    for built_in in ("plan_", "design_"):
        assert built_in not in joined, [t for t in out["texts"] if built_in in t][:2]


def test_a_write_the_mappings_glob_does_not_match_is_refused(tmp_path):
    """A mapped location's items are the files its glob picks: writing any
    other relpath there would make a file nothing indexes or watches."""
    repo = _repo(tmp_path, "{dir: workflows, glob: '*.md'}", {"workflows/a.md": "a\n"})
    out = _run(
        tmp_path,
        ROUTINE,
        _MAPPED.replace("REPO", repr(str(repo)))
        + """
        from crib.notes import Note
        store = crib._facet_store("routine")
        out["nested"] = refused(lambda: asyncio.run(
            store.write("p", "sub/b.md", Note(Path("b.md"), {"title": "B"}, "b\\n"))))
        out["top"] = refused(lambda: asyncio.run(
            store.write("p", "c.md", Note(Path("c.md"), {"title": "C"}, "c\\n"))))
    """,
    )
    assert "isn't one of the routine files of project 'p'" in out["nested"]
    assert out["top"] is None
    assert not (repo / "workflows" / "sub").exists()


def test_changing_a_facets_store_leaves_no_ghost_hits(tmp_path):
    """A store moved by `crib facets sync`: the old dir's chunks go with it,
    so a search of the new (empty) store finds nothing."""
    pytest.importorskip("duckdb")
    first = WIDGET.replace('plural = "widgets"', 'plural = "widgets"\nstore = "wa"')
    _run(tmp_path, first, _CONFORM_PRELUDE + """
        cli("facets", "sync", "--apply")
        crib = Crib.open()
        asyncio.run(crib.store_note("the gasket leaks under pressure", "Gasket", "p",
                                    facet="widget"))
    """)
    out = _run(tmp_path, first.replace('store = "wa"', 'store = "wb"'),
               _CONFORM_PRELUDE + """
        cli("facets", "sync", "--apply")
        crib = Crib.open()
        out["hits"] = [h.relpath for h in crib.lookup("gasket leaks", "p", store="wb")]
        ex = crib.store._conn.execute
        out["chunks"] = ex("SELECT count(*) FROM crib_chunks WHERE kind='widget'").fetchone()[0]
    """)
    assert out["hits"] == [] and out["chunks"] == 0


def test_agents_are_told_about_this_machines_declared_facets(tmp_path):
    """The shipped directive describes crib's own facets; a declared one's
    guidance is generated where it is declared: `crib facets instructions` and
    the MCP server's instructions (pending facets say so)."""
    facets_file = ROUTINE.replace(
        'plural = "routines"',
        'plural = "routines"\nabout = "A routine is a job an agent runs on a schedule."',
    ) + '[routine.fields]\nschedule = "str"\ntimeout = "int"\n' + INCIDENT
    out = _run(
        tmp_path,
        facets_file,
        _CONFORM_PRELUDE + """
        from crib import facets
        from crib.server import build_server
        from crib.store import InMemoryStore
        from crib.config import Config
        from crib.paths import Paths
        out["cli"] = cli("facets", "instructions")
        mcp = build_server(Crib(Paths.resolve().ensure(), Config(), InMemoryStore()))
        out["server"] = mcp.instructions
        out["pending"] = facets.instructions(pending=["routine"])
        out["none"] = facets.instructions([s for s in facets.FACETS if s.builtin])
    """,
    )
    code, text = out["cli"]
    assert code == 0
    assert "### Declared facets on this machine" in text
    assert "**routines** (`routine_*`, `crib routine ...`): A routine is a job" in text
    assert "fields `schedule` (str), `timeout` (int)" in text
    assert "**incidents**" in text and "mitigated (claimed)" in text and "`incident_next`" in text
    assert "### Declared facets on this machine" in out["server"]
    assert "waits for `crib facets sync --apply`" in out["pending"]
    assert out["none"] == ""


def _hook_functions() -> str:
    import re

    hook = (Path(__file__).resolve().parent.parent / "plugins" / "claude" / "hooks"
            / "session-start.sh")
    return "".join(m.group(0) for m in re.finditer(
        r"^(warn|_declared_facets|_emit)\(\) \{.*?^\}\n", hook.read_text(), re.S | re.M))


def _run_hook(tmp_path: Path, crib_body: str | None, *, facets: bool = True,
              via: str = "bin", extra_env: dict | None = None) -> tuple[str, float]:
    """Run the hook's _emit (with _declared_facets) against a fake plugin dir;
    `crib_body` is a fake crib's script (None: none), found on PATH (`via=path`),
    as $CRIB_BIN (`via=bin`), or AS the plugin's own bin/crib (`via=self`)."""
    import json as _json
    import time

    plug = tmp_path / "plugin"
    (plug / "bin").mkdir(parents=True, exist_ok=True)
    (plug / "instructions.txt").write_text("# Memory (cribsheet)\nthe directive\n")
    fb = tmp_path / "fakebin"
    fb.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("CRIB_BIN", "CRIBSHEET_DEV")}
    env["PATH"] = f"{fb}:/usr/bin:/bin"
    ff = tmp_path / "facets.toml"
    if facets:
        ff.write_text("[routine]\nplural = \"routines\"\n")
    env["CRIB_FACETS_FILE"] = str(ff)
    if crib_body is not None:
        target = {"path": fb / "crib", "bin": tmp_path / "mycrib",
                  "self": plug / "bin" / "crib"}[via]
        target.write_text("#!/usr/bin/env bash\n" + crib_body)
        target.chmod(0o755)
        if via == "bin":
            env["CRIB_BIN"] = str(target)
        if via == "self":
            env["CRIB_BIN"] = str(target)
    env.update(extra_env or {})
    script = f'set -euo pipefail\ndir={str(plug)!r}\n_warnings=""\n{_hook_functions()}_emit\n'
    t0 = time.monotonic()
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    took = time.monotonic() - t0
    assert r.returncode == 0, r.stderr
    return _json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"], took


_SECTION = ("[ \"$1 $2\" = 'facets instructions' ] && "
            "printf '### Declared facets on this machine\\n- routines\\n'\n")


@pytest.mark.parametrize("via", ["path", "bin"])
def test_the_claude_hook_adds_the_declared_facets_section(tmp_path, via):
    """plugins/claude/hooks/session-start.sh: the shipped directive, then what
    the machine's crib prints for `facets instructions`, in one SessionStart
    JSON."""
    ctx, _ = _run_hook(tmp_path, _SECTION, via=via)
    assert ctx.startswith("# Memory (cribsheet)\nthe directive")
    assert ctx.endswith("### Declared facets on this machine\n- routines")


@pytest.mark.parametrize("case", ["no-facets-file", "old-crib", "self", "no-crib", "not-executable"])
def test_the_claude_hook_never_waits_or_fetches_for_the_section(tmp_path, case):
    """No facets file: crib isn't even asked. An old crib (no subcommand): no
    section. The plugin's own bin/crib (its uvx fallback) is never used for it,
    and a CRIB_BIN that isn't executable doesn't count (review 3 S1)."""
    if case == "no-facets-file":
        ctx, _ = _run_hook(tmp_path, "echo SHOULD-NOT-RUN\n", facets=False, via="path")
    elif case == "old-crib":
        ctx, _ = _run_hook(tmp_path, "echo 'usage: crib' >&2; exit 2\n", via="path")
    elif case == "self":
        ctx, _ = _run_hook(tmp_path, _SECTION, via="self")
    elif case == "no-crib":
        ctx, _ = _run_hook(tmp_path, None)
    else:
        ctx, _ = _run_hook(tmp_path, None, extra_env={"CRIB_BIN": str(tmp_path / "nope")})
    assert ctx == "# Memory (cribsheet)\nthe directive"


def test_a_slow_crib_is_cut_off(tmp_path):
    ctx, took = _run_hook(tmp_path, "sleep 30\n", via="path")
    assert ctx == "# Memory (cribsheet)\nthe directive"
    assert took < 15


def test_adds_and_imports_take_a_mappings_suffix(tmp_path):
    """Under glob **/*.rt.md a declared note-like facet's new items and its
    imports are named for the glob (review 3 S3)."""
    repo = _repo(tmp_path, "{dir: workflows, glob: '**/*.rt.md'}", {"workflows/.keep": ""})
    doc = tmp_path / "brief.md"
    doc.write_text("# Brief\nmorning briefing\n")
    top = _repo(tmp_path / "t", "{dir: wf, glob: '*.rt.md'}", {"wf/.keep": ""})
    out = _run(
        tmp_path,
        ROUTINE,
        _MAPPED.replace("REPO", repr(str(repo)))
        + """
        a = asyncio.run(crib.store_note("x", "Morning", "p", facet="routine"))
        out["add"] = a["relpath"]
        imp = asyncio.run(crib.import_files([DOC], project="p", facet="routine"))
        out["import"] = sorted(str(r) for r in Path(REPO, "workflows").rglob("*.rt.md"))
        asyncio.run(crib._sync_mappings("q", CribLink.find(Path(TOP))))
        asyncio.run(crib.import_files([DOC], project="q", facet="routine"))
        out["top"] = sorted(f.name for f in Path(TOP, "wf").glob("*.rt.md"))
    """.replace("REPO", repr(str(repo))).replace("DOC", repr(str(doc))).replace(
            "TOP", repr(str(top))),
    )
    assert out["add"] == "morning.rt.md"
    assert str(repo / "workflows" / "imported" / "brief.rt.md") in out["import"]
    assert out["top"] == ["imported-brief.rt.md"]


def test_learnings_are_named_for_their_mappings_suffix(tmp_path):
    """A learnings pillar mapped under **/*.lrn.md: a new learning's name and
    migrate's canonical target end .lrn.md (review 3 S3)."""
    out = _run(
        tmp_path,
        None,
        """
        import asyncio
        from pathlib import Path
        from crib.app import Crib
        from crib.config import Config, CribLink
        from crib.paths import Paths
        from crib.store import InMemoryStore
        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
        repo = Path(REPO)
        (repo / "lrn").mkdir(parents=True)
        (repo / ".crib").write_text("project: p\\nfacets:\\n  learning: {dir: lrn, glob: '**/*.lrn.md'}\\n")
        asyncio.run(crib._sync_mappings("p", CribLink.find(repo)))
        entry = {"symbol_ref": "python:pkg/mod.py:pkg.mod.fn"}
        out["name"] = crib.learnings.relpath("p", entry)
        out["suffix"] = crib.learnings.store.suffix("p")
    """.replace("REPO", repr(str(tmp_path / "lrepo"))),
    )
    assert out["suffix"] == ".lrn.md"
    assert out["name"].endswith(".lrn.md")


def test_doctor_names_a_field_value_that_doesnt_fit_its_type(tmp_path):
    """`timeout: 30s` on an int field indexes as empty, so the item drops out
    of filters on it: doctor names the file, the field and the fix."""
    repo = _repo(
        tmp_path,
        "{dir: workflows, glob: '*.md', read_only: true, ids: derived}",
        {
            "workflows/morning.md": "---\nname: morning\ntimeout: 30s\n---\nbrief me\n",
            "workflows/nightly.md": "---\nname: nightly\ntimeout: 600\nwhen: 2026-11-16\n---\nback up\n",
        },
    )
    out = _run(
        tmp_path,
        ROUTINE + '[routine.fields]\ntimeout = "int"\nwhen = "date"\n',
        _MAPPED.replace("REPO", repr(str(repo)))
        + """
        from crib.doctor import diagnose
        out["found"] = [(f["item"], f["field"], f["value"]) for f in diagnose(crib, "p")["findings"]
                        if f["check"] == "field value doesn't fit its type"]
        out["fix"] = [f["fix"] for f in diagnose(crib, "p")["findings"]
                      if f["check"] == "field value doesn't fit its type"]
    """,
    )
    assert out["found"] == [["morning.md", "timeout", "30s"]]
    assert "takes an int" in out["fix"][0]


def test_derived_ids_ignore_a_files_own_id(tmp_path):
    """Under `ids: derived` the id is the path's: another tool's `id:` in the
    frontmatter (here two files sharing one) must not merge or hide items."""
    body_a = "---\nid: morning-brief\ntype: periodic-task\n---\nbrief me about the weather\n"
    body_b = "---\nid: morning-brief\n---\na second file claiming that id\n"
    repo = _repo(
        tmp_path,
        "{dir: workflows, glob: '*.md', read_only: true, ids: derived}",
        {"workflows/morning.md": body_a, "workflows/other.md": body_b},
    )
    out = _run(
        tmp_path,
        ROUTINE,
        _MAPPED.replace("REPO", repr(str(repo)))
        + """
        from crib.util import derived_ulid
        items = crib.store.items({"project": "p", "kind": "routine"})
        out["rels"] = sorted(i.relpath for i in items.values())
        out["ids_ok"] = all(i.id == derived_ulid("p", i.relpath) for i in items.values())
        out["tagged"] = [h.relpath for h in crib.lookup("weather", "p", store="routines",
                                                        tags=["periodic-task"])]
    """,
    )
    assert out["rels"] == ["morning.md", "other.md"]
    assert out["ids_ok"]
    assert out["tagged"] == ["morning.md"]  # its `type` still filters as a tag
    assert (repo / "workflows" / "morning.md").read_text() == body_a  # untouched


def test_toucans_routines_get_titles_tags_reads_and_a_summary(tmp_path):
    """The routine rough edges: a title from the heading or filename (none in
    the frontmatter), list rows with tags and a tags filter (type counts),
    an extensionless read, a clean miss, and the index summary."""
    repo = _repo(
        tmp_path,
        "{dir: workflows, glob: '*.md', read_only: true, ids: derived}",
        {
            "workflows/morning-briefing.md": (
                "---\nname: morning-briefing\ntype: periodic-task\ntags: [daily]\n"
                "schedule: 0 16 * * 1-5\n---\n# Morning Briefing\n\nbrief me\n"),
            "workflows/research.md": "---\nname: research\ntype: workflow\n---\nlook it up\n",
        },
    )
    spec = ROUTINE.replace('plural = "routines"', 'plural = "routines"\n'
                           'title_from = ["title", "heading", "filename"]')
    out = _run(
        tmp_path,
        spec + '[routine.fields]\nname = "str"\nschedule = "str"\n',
        _MAPPED.replace("REPO", repr(str(repo)))
        + """
        rows = crib.list_items("p", facet="routine")["items"]
        out["titles"] = {r["relpath"]: r["title"] for r in rows}
        out["tags"] = {r["relpath"]: r["tags"] for r in rows}
        out["periodic"] = [r["relpath"] for r in crib.list_items(
            "p", facet="routine", tags=["periodic-task"])["items"]]
        out["read"] = crib.read_note("morning-briefing", "p", facet="routine")[:3]
        out["miss"] = refused(lambda: crib.read_note("morning-brief", "p", facet="routine"))
        out["summary"] = crib._mapping_summary("p")
    """,
    )
    assert out["titles"] == {"morning-briefing.md": "Morning Briefing", "research.md": "research"}
    assert out["tags"]["morning-briefing.md"] == ["daily", "periodic-task"]
    assert out["periodic"] == ["morning-briefing.md"]
    assert out["read"] == "---"
    assert "no routine 'morning-brief'" in out["miss"] and "morning-briefing.md" in out["miss"]
    s = out["summary"]["routine"]
    assert (s["items"], s["read_only"], s["ids"], s["glob"]) == (2, True, "derived", "*.md")


def test_project_index_reports_the_facets_even_while_its_sweep_runs(tmp_path, capsys):
    """Every `project index` answer, a running sweep's handle included, carries
    the facet summary, and the CLI prints a running sweep as running (it used
    to print `indexed 0/0 files` and nothing about the facet)."""
    repo = _repo(tmp_path, "{dir: workflows, glob: '*.md', read_only: true, ids: derived}",
                 {"workflows/a.md": "---\nname: a\n---\n# A\nbody\n"})
    out = _run(
        tmp_path,
        ROUTINE,
        _MAPPED.replace("REPO", repr(str(repo)))
        + """
        res = asyncio.run(crib.project_index(cwd=Path(REPO)))
        out["facets"] = res.get("facets")
        out["running"] = bool(res.get("running"))
    """.replace("REPO", repr(str(repo))),
    )
    assert out["facets"]["routine"]["items"] == 1
    from crib.cli import _emit_project

    _emit_project({"project": "toucan", "running": True, "joined": True, "done": 11,
                   "total": 14, "facets": out["facets"]}, "index", False)
    printed = capsys.readouterr().out
    assert "indexing in progress — 11/14 files" in printed
    assert "facet routine:" in printed and "1 item(s) indexed" in printed
    assert "indexed 0/0" not in printed
    _emit_project({"project": "toucan", "running": True, "done": None, "total": None},
                  "index", False)
    _emit_project({"project": "toucan", "running": True, "done": None, "total": None},
                  "wait", False)
    printed = capsys.readouterr().out
    assert "None" not in printed and printed.count("starting (counting files)") == 2


def test_doctor_lists_untitled_items_when_the_facet_insists(tmp_path):
    repo = _repo(tmp_path, "{dir: workflows, glob: '*.md', read_only: true, ids: derived}",
                 {"workflows/a.md": "---\nname: a\n---\nno heading here\n"})
    out = _run(
        tmp_path,
        ROUTINE,
        _MAPPED.replace("REPO", repr(str(repo)))
        + """
        from crib.doctor import diagnose
        out["found"] = [(f["item"], f["fix"]) for f in diagnose(crib, "p")["findings"]
                        if f["check"] == "no title"]
    """,
    )
    assert [i for i, _ in out["found"]] == ["a.md"]
    assert "title_from" in out["found"][0][1]


def test_add_refuses_a_missing_required_field_and_doctor_names_files(tmp_path):
    repo = _repo(tmp_path, "{dir: workflows, glob: '*.md', read_only: true, ids: derived}",
                 {"workflows/a.md": "---\nschedule: daily\n---\nno name here\n"})
    out = _run(
        tmp_path,
        ROUTINE + '[routine.fields]\nname = "str"\nschedule = "str"\n',
        _MAPPED.replace("REPO", repr(str(repo)))
        + """
        from crib.doctor import diagnose
        out["found"] = [(f["item"], f["field"]) for f in diagnose(crib, "p")["findings"]
                        if f["check"] == "required field missing"]
        out["refused"] = refused(lambda: asyncio.run(
            crib.store_note("x", "X", "other", facet="routine", fields={"schedule": "d"})))
        a = asyncio.run(crib.store_note("x", "X", "other", facet="routine",
                                        fields={"name": "x", "schedule": "d"}))
        out["edit"] = refused(lambda: asyncio.run(
            crib.edit_note(a["relpath"], "changed", "other", facet="routine")))
    """,
    )
    assert out["found"] == [["a.md", "name"]]
    assert "needs `name`" in out["refused"]
    assert out["edit"] is None


def test_a_declared_graph_facets_cli_runs_in_process(tmp_path):
    """Every CLI row of a declared graph facet names a real Crib method whose
    signature takes what the row passes (design review P1: `crib --no-daemon
    incident list` raised AttributeError on `incident_list`), and the verbs run
    end to end without the daemon."""
    out = _run(
        tmp_path,
        INCIDENT + RFC,
        _CONFORM_PRELUDE + """
        import inspect
        from crib import cli as climod
        rows = {k: v for k, v in climod._facet_rows().items()
                if k.split()[0] in ("incident", "rfc")}
        bad = []
        for key, row in rows.items():
            m = getattr(Crib, row.crib_method(), None)
            if m is None:
                bad.append((key, row.crib_method(), "missing"))
                continue
            names = set(inspect.signature(m).parameters)
            want = {n for n, _ in row.inproc} | {"cwd"}
            if not want <= names:
                bad.append((key, row.crib_method(), sorted(want - names)))
        out["bad"] = bad
        out["n"] = len(rows)
        out["pending"] = cli("--no-daemon", "incident", "list", "-p", "p")
        cli("facets", "sync", "--apply")
        out["add"] = cli("--no-daemon", "incident", "add", "Failover", "the primary failed",
                         "-p", "p")
        out["list"] = cli("--no-daemon", "incident", "list", "-p", "p")
        out["next"] = cli("--no-daemon", "incident", "next", "-p", "p")
        out["shelve"] = cli("--no-daemon", "incident", "shelve", "failover.md", "--why",
                            "later", "-p", "p")
        out["rfc"] = cli("--no-daemon", "rfc", "add", "Async API", "go async", "-p", "p")
        out["accept"] = cli("--no-daemon", "rfc", "accept", "async-api.md", "-p", "p")
    """,
    )
    assert out["n"] > 20 and out["bad"] == []
    assert out["pending"][0] != 0  # refused while pending, not an empty list
    for k in ("add", "list", "next", "shelve", "rfc", "accept"):
        assert out[k][0] == 0, (k, out[k])
    assert "Failover" in out["list"][1]


def test_a_declared_graph_facet_gets_similar_with_its_own_roles_and_tools(tmp_path):
    """Tidy B: `similar` compares declared facets too; a twin of a resolved
    incident is `settled` (a done role, not the word "done"), and the hint
    names incident_read, not a built-in tool."""
    out = _run(
        tmp_path,
        INCIDENT,
        """
        import asyncio
        from crib.app import Crib
        from crib.config import Config
        from crib.paths import Paths
        from crib.store import InMemoryStore
        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
        text = "the primary database failed over to the replica at noon"
        a = asyncio.run(crib.plan_add("Failover", text, project="p", facet="incident"))
        rel = a["items"][0]["relpath"]
        asyncio.run(crib.plan_status(rel, "resolved", project="p", facet="incident"))
        b = asyncio.run(crib.plan_add("Failover again", text, project="p", facet="incident"))
        out["similar"] = [(r["relpath"], r["verdict"], r["next"])
                          for r in b["items"][0].get("similar", []) or b.get("similar", [])]
        out["rel"] = rel
    """,
    )
    hit = [x for x in out["similar"] if x[0] == out["rel"]]
    assert hit and hit[0][1] == "settled"
    assert "incident_read" in hit[0][2]


def test_transitions_into_every_role_work(tmp_path):
    """A transition moves an item from wherever it is, by role: ungating a
    draft, into claimed and done (answering with what that unblocked), back
    to gated; already-there refuses. Only pausing and dropping were covered
    before, and the rest always failed (review fix 3). This facet has no
    progress aspect, so transitions are its only way to move."""
    out = _run(
        tmp_path,
        RFC,
        """
        import asyncio
        from fastmcp import Client
        from crib.app import Crib
        from crib.config import Config
        from crib.paths import Paths
        from crib.server import build_server
        from crib.store import InMemoryStore

        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
        mcp = build_server(crib)

        async def main():
            async with Client(mcp) as c:
                out["tools"] = sorted(t.name for t in await c.list_tools()
                                      if t.name.startswith("rfc_"))

                async def call(name, **args):
                    try:
                        return (await c.call_tool(name, args)).data
                    except Exception as e:
                        return {"error": str(e)}

                a = await call("rfc_add", title="Async API",
                               content="Make the client async.", project="p")
                r = a["relpath"]
                out["initial"] = (await call("rfc_read", ref=r, project="p"))["status"]
                out["accept"] = (await call("rfc_accept", ref=r, project="p"))["status"]
                out["again"] = (await call("rfc_accept", ref=r, project="p")).get("error")
                out["build"] = (await call("rfc_build", ref=r, project="p"))["status"]
                ship = await call("rfc_ship", ref=r, project="p")
                out["ship"] = (ship["status"], "unblocked" in ship)
                out["redraft"] = (await call("rfc_redraft", ref=r, project="p"))["status"]
                out["reaccept"] = (await call("rfc_accept", ref=r, project="p"))["status"]
                rej = await call("rfc_reject", ref=r, why="superseded by sync", project="p")
                out["reject"] = rej.get("status")
        asyncio.run(main())
    """,
    )
    assert {"rfc_accept", "rfc_build", "rfc_ship", "rfc_redraft",
            "rfc_reject"} <= set(out["tools"])
    assert "rfc_status" not in out["tools"]  # no progress aspect
    assert out["initial"] == "draft"
    assert out["accept"] == "accepted"  # ungated
    assert "already accepted" in out["again"]
    assert out["build"] == "building"  # claimed
    assert out["ship"] == ["shipped", True]  # done, answering with what it unblocked
    assert out["redraft"] == "draft"  # back to gated
    assert out["reaccept"] == "accepted"
    assert out["reject"] == "rejected"


def test_a_declared_graph_facet_runs_on_its_own_statuses(tmp_path):
    out = _run(
        tmp_path,
        INCIDENT,
        """
        import asyncio
        from fastmcp import Client
        from crib.app import Crib
        from crib.config import Config
        from crib.paths import Paths
        from crib.server import build_server
        from crib.store import InMemoryStore

        crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
        mcp = build_server(crib)

        async def main():
            async with Client(mcp) as c:
                out["tools"] = sorted(t.name for t in await c.list_tools()
                                      if t.name.startswith("incident_"))

                async def call(name, **args):
                    return (await c.call_tool(name, args)).data

                a = await call("incident_add", title="Database failover",
                               content="the primary failed over to the replica", project="p")
                b = await call("incident_add", title="Write the postmortem",
                               content="after the failover is resolved", deps=[a["relpath"]],
                               project="p")
                c3 = await call("incident_add", title="Flaky alert",
                                content="pages at 3am for nothing", project="p")
                out["first"] = (await call("incident_read", ref=a["relpath"], project="p"))["status"]
                out["dep_kind"] = b.get("dep_kinds")
                nxt = await call("incident_next", project="p")
                out["next_before"] = sorted(i["title"] for i in nxt["items"])
                await call("incident_status", ref=a["relpath"], status="mitigated", project="p")
                out["claimed"] = (await call("incident_next", project="p"))["claimed"]
                done = await call("incident_status", ref=a["relpath"], status="resolved",
                                  project="p")
                out["unblocked"] = [u["title"] for u in done.get("unblocked", [])]
                nxt = await call("incident_next", project="p")
                out["next_after"] = sorted(i["title"] for i in nxt["items"])
                s = await call("incident_shelve", ref=b["relpath"], why="after the quarter",
                               project="p")
                out["shelved"] = s["status"]
                w = await call("incident_wontfix", ref=c3["relpath"], why="the alert is retired",
                               project="p")
                out["wontfix"] = w["status"]
                try:
                    await call("incident_status", ref=a["relpath"], status="closed", project="p")
                    out["bad_status"] = None
                except Exception as e:
                    out["bad_status"] = str(e)
                hits = await call("incident_lookup", query="failover replica", project="p")
                out["lookup"] = [h["relpath"] for h in hits][:1]
                lst = await call("incident_list", project="p", all=True)
                out["list"] = sorted(lst) if isinstance(lst, dict) else str(type(lst))
                out["a"] = a["relpath"]
        asyncio.run(main())
    """,
    )
    want = {
        f"incident_{v}"
        for v in (
            "add",
            "read",
            "edit",
            "append",
            "lookup",
            "list",
            "forget",
            "dep_add",
            "dep_remove",
            "reaffirm",
            "graph",
            "status",
            "review",
            "review_apply",
            "next",
            "move",
            "shelve",
            "wontfix",
        )
    }
    assert (
        set(out["tools"]) == want
    )  # no check/tree/supersede/promote/extract: no such traits
    assert out["first"] == "open"  # its own initial status
    assert list((out["dep_kind"] or {}).values()) == ["follows"]  # its own edges table
    assert out["next_before"] == [
        "Database failover",
        "Flaky alert",
    ]  # the postmortem waits
    assert out["claimed"] == 1  # mitigated plays `claimed`
    assert out["unblocked"] == ["Write the postmortem"]  # resolved plays `done`
    assert "Write the postmortem" in out["next_after"]
    assert (
        out["shelved"] == "shelved" and out["wontfix"] == "wontfix"
    )  # free transition names
    assert "use one of open, mitigated, resolved, shelved, wontfix" in out["bad_status"]
    assert out["lookup"] == [out["a"]]


def test_a_declared_graph_facet_conforms_and_has_a_cli(tmp_path):
    """Its store table comes with its aspects (status, rank, dates, body hash,
    trigger/review), it waits for `crib facets sync` like any declared facet,
    and its CLI subcommands come from the same templates."""
    pytest.importorskip("duckdb")
    out = _run(
        tmp_path,
        INCIDENT,
        _CONFORM_PRELUDE
        + """
        from crib.cli import VERBS, build_parser
        from crib.items import FACETS
        out["fields"] = list(FACETS["incident"])
        crib = Crib.open()
        out["pending"] = sorted(crib.pending_facets)
        crib.close()
        out["apply"] = cli("facets", "sync", "--apply")[0]
        crib = Crib.open()
        out["rows"] = sorted(k for k in VERBS if k.startswith("incident "))
        p = build_parser()
        out["parsed"] = p.parse_args(["incident", "shelve", "x.md", "--why", "later"]).why
        r = asyncio.run(crib.plan_add("Database failover", "the primary failed", project="p",
                                      facet="incident"))
        out["status"] = crib.plan_read(r["relpath"], "p", facet="incident")["status"]
    """,
    )
    assert out["fields"] == [
        "status",
        "rank",
        "trigger",
        "reviewed",
        "created",
        "updated",
        "body_hash",
    ]
    assert out["pending"] == ["incident"] and out["apply"] == 0
    assert "incident shelve" in out["rows"] and "incident next" in out["rows"]
    assert "incident check" not in out["rows"]  # it doesn't report taint
    assert out["parsed"] == "later"
    assert out["status"] == "open"
