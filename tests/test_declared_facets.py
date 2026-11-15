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
    from crib.cli import _FACET_CLI, _FACET_CLI_ORDER

    canonical = {t.verb for t in verbs.TEMPLATES}
    assert set(_FACET_CLI) == canonical
    assert set(_FACET_CLI_ORDER) == canonical


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
