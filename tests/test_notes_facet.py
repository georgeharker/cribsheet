"""The stamp-gated reconcile (NoteStore.reindex(full=False)): index_file records
a stamp per note (crib.filesync), and the startup/post-pull sweep re-reads only
the notes whose stamp moved, still dropping a deleted note's chunks and still
reporting duplicate ids."""

from __future__ import annotations

import asyncio
import os

import pytest

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


def _note(crib, relpath: str, title: str, body: str) -> None:
    path = crib.notestore.dir("p") / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {title}\n---\n\n{body}\n")


def _touch(path):
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def _reads(crib, monkeypatch) -> list[str]:
    seen: list[str] = []
    real = crib.index._plan

    def plan(project, notes_dir, relpath, *a, **k):
        seen.append(relpath)
        return real(project, notes_dir, relpath, *a, **k)

    monkeypatch.setattr(crib.index, "_plan", plan)
    return seen


def test_the_reconcile_reads_only_what_changed(crib, monkeypatch):
    _note(crib, "a.md", "alpha", "First note.")
    _note(crib, "sub/b.md", "beta", "Second note.")
    run(crib.reindex(project="p"))  # full: stamps everything
    nd = crib.notestore.dir("p")
    (nd / "sub" / "b.md").write_text((nd / "sub" / "b.md").read_text() + "\nMore.\n")
    _touch(nd / "sub" / "b.md")
    seen = _reads(crib, monkeypatch)
    out = run(crib.reindex(project="p", full=False))
    assert seen == ["sub/b.md"] and out["changed"] == 1
    seen.clear()
    assert run(crib.reindex(project="p", full=False))["changed"] == 0
    assert seen == []  # nothing moved: nothing read


def test_a_deleted_note_is_dropped_and_an_explicit_reindex_reads_all(crib, monkeypatch):
    _note(crib, "a.md", "alpha", "First note.")
    _note(crib, "b.md", "beta", "Second note.")
    run(crib.reindex(project="p"))
    (crib.notestore.dir("p") / "b.md").unlink()
    out = run(crib.reindex(project="p", full=False))
    assert out["removed"] >= 1
    assert {
        m["relpath"]
        for m in crib.store.get_meta({"project": "p", "kind": "note"}).values()
    } == {"a.md"}
    seen = _reads(crib, monkeypatch)
    run(crib.reindex(project="p"))  # full, as the tool runs it
    assert seen == ["a.md"]


def test_duplicate_ids_are_still_reported(crib):
    _note(crib, "a.md", "alpha", "First note.")
    run(crib.reindex(project="p"))
    nd = crib.notestore.dir("p")
    (nd / "copy.md").write_text((nd / "a.md").read_text())  # an interrupted move
    out = run(crib.reindex(project="p", full=False))
    assert out.get("duplicate_ids") and out["duplicate_ids"][0]["relpaths"] == [
        "a.md",
        "copy.md",
    ]


def test_a_chunk_schema_change_rechunks_every_note(crib, monkeypatch):
    _note(crib, "a.md", "alpha", "First note.")
    run(crib.reindex(project="p"))
    crib._schema_marker.write_text("0\n")  # an older chunking scheme
    seen = _reads(crib, monkeypatch)
    run(crib.reconcile_all())
    assert "a.md" in seen
    seen.clear()
    run(crib.reconcile_all())  # marker now current
    assert "a.md" not in seen
