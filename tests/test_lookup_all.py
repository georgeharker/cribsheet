"""`project_lookup` (Crib.lookup_all): one search across notes, decisions,
plans and learnings, each pillar's candidates pooled before ONE rerank."""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.config import Config
from crib.errors import CribUserError
from crib.paths import Paths
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    cfg = Config()
    cfg.retrieve.rerank = False  # no model download in a unit test
    return Crib(Paths.resolve().ensure(), cfg, InMemoryStore())


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def seeded(crib):
    run(
        crib.store_note(
            "The relay orders every frame before delivery.",
            title="relay ordering",
            project="p",
        )
    )
    d = run(
        crib.design_add(
            "Relay owns frame ordering",
            "The relay orders frames; forks never reorder.",
            project="p",
        )
    )
    p = run(
        crib.plan_add(
            "Implement relay frame ordering",
            "Make the relay order frames.",
            project="p",
        )
    )
    run(crib.plan_status(p["relpath"], "done", project="p"))
    return crib, d, p


def test_every_kind_comes_back_tagged(seeded):
    crib, d, p = seeded
    hits = crib.lookup_all("relay frame ordering", project="p")
    kinds = {h["kind"] for h in hits}
    assert {"note", "design", "plan"} <= kinds
    plan = next(h for h in hits if h["kind"] == "plan")
    assert plan["status"] == "done" and plan["relpath"] == p["relpath"]
    design = next(h for h in hits if h["kind"] == "design")
    assert design["status"] == "active"
    assert [h["score"] for h in hits] == sorted(
        (h["score"] for h in hits), reverse=True
    )


def test_kinds_and_status_filter(seeded):
    crib, d, p = seeded
    only = crib.lookup_all("relay frame ordering", project="p", kinds=["design"])
    assert only and {h["kind"] for h in only} == {"design"}
    live = crib.lookup_all("relay frame ordering", project="p", status=["todo"])
    assert all(h.get("status") == "todo" for h in live) and not any(
        h["relpath"] == p["relpath"] for h in live
    )
    with pytest.raises(CribUserError, match="unknown kinds"):
        crib.lookup_all("relay", project="p", kinds=["symbol"])
