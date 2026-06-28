"""Watcher reindexes external edits and is harmless on echoes (DESIGN §4, §9)."""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore
from crib.watch import decode

watchdog = pytest.importorskip("watchdog")


def test_decode_path_to_project_relpath(tmp_path):
    projects = tmp_path / "projects"
    p = projects / "notes" / "notes" / "sub" / "a.md"
    p.parent.mkdir(parents=True)
    p.write_text("x")
    assert decode(projects, str(p)) == ("notes", "sub/a.md")
    # not under <project>/notes/ -> ignored
    other = projects / "notes" / ".cribproject"
    other.write_text("name: notes")
    assert decode(projects, str(other)) is None


def test_watcher_indexes_external_edit(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    paths = Paths.resolve().ensure()
    crib = Crib(paths, Config(), InMemoryStore())

    async def scenario():
        crib.start_watchers(asyncio.get_running_loop())
        nd = crib.notes_dir("p")
        # Simulate an external editor writing a new note directly to disk.
        (nd / "external.md").write_text(
            "---\ntitle: ext\n---\nThe watcher should index this automatically.")
        for _ in range(50):                     # poll up to ~5s for debounce+index
            await asyncio.sleep(0.1)
            if crib.lookup("watcher index automatically", project="p"):
                break
        crib.stop_watchers()
        return crib.lookup("watcher index automatically", project="p")

    hits = asyncio.run(scenario())
    assert hits and hits[0].relpath == "external.md"
