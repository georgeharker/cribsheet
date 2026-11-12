"""The watcher's routes into the facets' refresh: an edit seen by the notes
watcher (Crib._on_fs_change) leaves the store current for both the chunk index
(its index:<pillar> stamp) and, for a design/plan, the graph's own items and
stamps, so neither the next reconcile nor the next graph read re-reads it.
(Driven directly: real FSEvents delivery is the watcher tests' business.)"""

from __future__ import annotations

import asyncio
import os

import pytest

from crib import notes
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


def _edit(path, status=None, body=None):
    note = notes.load(path)
    if status is not None:
        note.frontmatter["status"] = status
    if body is not None:
        note.body = body
    notes.save_atomic(note)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def _count_parses(monkeypatch) -> list[str]:
    seen: list[str] = []
    real = notes.parse

    def counting(text, p=None):
        seen.append(str(getattr(p, "name", p)))
        return real(text, p)

    monkeypatch.setattr(notes, "parse", counting)
    return seen


def test_a_design_edit_seen_by_the_watcher_needs_no_parse_at_the_next_read(
    crib, monkeypatch
):
    d = run(crib.design_add("Ground", "The base decision.", project="p"))
    crib.designs._load_graph("p")
    path = crib.designs._stores["design"].root("p") / d["relpath"]
    _edit(path, status="proposed", body="Reworded by hand.")
    run(crib._on_fs_change("p", "design", d["relpath"]))  # the watcher's dispatch
    item = crib.store.items({"id": d["id"]})[d["id"]]
    assert item.facet["status"] == "proposed"
    seen = _count_parses(monkeypatch)
    node = crib.designs._load_graph("p").nodes[d["id"]]
    assert node.status == "proposed" and seen == []


def test_a_plan_removed_behind_crib_leaves_the_graph_at_the_watcher(crib):
    p1 = run(crib.plan_add("Step", project="p"))
    crib.designs._load_graph("p")
    (crib.designs._stores["plan"].root("p") / p1["relpath"]).unlink()
    run(crib._on_fs_change("p", "plans", p1["relpath"]))
    assert not crib.store.items({"id": p1["id"]})
    assert not crib.store.file_stamps("plan", "p")


def test_a_note_edit_seen_by_the_watcher_needs_no_read_at_the_next_reconcile(
    crib, monkeypatch
):
    nd = crib.notestore.dir("p")
    nd.mkdir(parents=True, exist_ok=True)
    (nd / "a.md").write_text("---\ntitle: alpha\n---\n\nFirst note.\n")
    run(crib.reindex(project="p"))
    _edit(nd / "a.md", body="First note, edited.")
    run(crib._on_fs_change("p", "notes", "a.md"))
    docs = crib.store.get_docs({"project": "p", "kind": "note"})
    assert any("edited" in text for text, _meta in docs.values())
    seen: list[str] = []
    real = crib.index._plan

    def plan(project, notes_dir, relpath, *a, **k):
        seen.append(relpath)
        return real(project, notes_dir, relpath, *a, **k)

    monkeypatch.setattr(crib.index, "_plan", plan)
    assert run(crib.reindex(project="p", full=False))["changed"] == 0
    assert seen == []
