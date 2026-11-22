"""Listing without a query: `list` on every facet, `project items` across them,
and `facets list` as an alias of `facets show`."""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest

from crib import cli, facets
from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def run(coro):
    return asyncio.run(coro)


def _fill(crib):
    run(crib.store_note("alpha body", "Alpha", "p", tags=["x"]))
    run(crib.store_note("beta body", "Beta", "p"))
    run(crib.design_add("Ground", "A decision.", project="p"))
    run(crib.plan_add("First", project="p"))


def test_every_facet_has_a_list_verb():
    for spec in facets.FACETS:
        assert "list" in facets.available_verbs(spec.aspects), spec.noun


def test_notes_list_without_fields(crib):
    _fill(crib)
    out = crib.list_items("p", facet="note")
    assert out["total"] == 2
    assert [r["title"] for r in out["items"]] == ["Alpha", "Beta"]
    assert crib.list_items("p", facet="note", tags=["x"])["total"] == 1
    assert (
        crib.list_items("p", facet="note", k=1)["total"] == 2
    )  # k caps, total doesn't


def test_project_items_lists_every_facet(crib):
    _fill(crib)
    out = crib.project_items("p")
    kinds = [r["kind"] for r in out["items"]]
    assert kinds == ["note", "note", "design", "plan"]
    plan = out["items"][-1]
    assert plan["title"] == "First" and plan["status"] == "todo"
    assert out["items"][2]["status"] == "active"
    only = crib.project_items("p", kinds=["plan"])
    assert [r["kind"] for r in only["items"]] == ["plan"]
    with pytest.raises(Exception, match="unknown kinds"):
        crib.project_items("p", kinds=["nope"])


def test_the_items_emitter_styles_nothing_when_off_and_shows_the_rest(crib):
    _fill(crib)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._emit_items(
            {**crib.project_items("p", k=2)}, SimpleNamespace(cmd="project", json=False)
        )
    text = buf.getvalue()
    assert "Alpha" in text and "#x" in text and "2 of 4 shown" in text
    assert "\x1b" not in text


def test_facets_list_is_facets_show(crib, capsys):
    assert cli.main(["facets", "list"]) == cli.main(["facets", "show"])
    out = capsys.readouterr().out
    half = len(out) // 2
    assert out[:half] == out[half:]


def test_project_items_cli_json(crib, monkeypatch, capsys):
    _fill(crib)
    monkeypatch.setattr(Crib, "open", classmethod(lambda cls, *a, **k: crib))
    monkeypatch.setattr(crib, "close", lambda: None)
    cli.main(["--no-daemon", "--json", "project", "items", "-p", "p", "--kind", "note"])
    data = json.loads(capsys.readouterr().out)
    assert data["total"] == 2 and {r["kind"] for r in data["items"]} == {"note"}
