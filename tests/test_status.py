"""`status` — the one-call health summary (projects, git, LSP sessions,
in-flight indexing)."""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.codeindex import SymbolIndex
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def test_status_inventories_projects(crib):
    asyncio.run(crib.store_note("A fact.", None, "alpha", None))
    SymbolIndex(crib.paths.project_dir("alpha")).write({
        "fqname": "m.f", "name": "f", "kind": "function", "content_hash": "h",
        "file": "m.py", "line": 1, "container": [], "calls": [], "called_by": [],
        "references": [], "name_terms": ["f"]})
    asyncio.run(crib.code_append("m.f", "insight", project="alpha"))

    d = crib.status()
    alpha = next(p for p in d["projects"] if p["project"] == "alpha")
    assert alpha["symbols"] == 1
    assert alpha["learnings"] == 1
    assert alpha["notes"] >= 2                # the stored note + the learning note
    assert d["git"] == {"enabled": False}     # data dir is not a repo here
    # the LSP pool is process-global (another test may have warmed a session):
    # assert the report SHAPE, not emptiness
    assert all({"root", "server", "pid", "alive", "busy", "idle_s"} <= set(s)
               for s in d["lsp_sessions"])
    assert d["indexing"] == {}
    assert d["store"] == "InMemoryStore" and d["embed_model"]


def test_status_reports_in_flight_indexing(crib, monkeypatch):
    """While `_index_file_sync` runs, status names the (project, file)."""
    seen: list[dict] = []

    def fake_inner(root, rel, proj, patch_edges, existing=None):
        seen.append(crib.status()["indexing"])
        return {"symbols": 0}

    monkeypatch.setattr(crib, "_index_file_inner", fake_inner)
    crib._index_file_sync("root", "pkg/mod.py", "alpha", True)  # type: ignore[arg-type]
    assert seen == [{"alpha": ["pkg/mod.py"]}]
    assert crib.status()["indexing"] == {}    # cleared once the work finishes
