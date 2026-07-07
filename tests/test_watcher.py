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


def test_code_watcher_decode_filters(tmp_path):
    from crib.watch import CodeWatcher
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    cw = CodeWatcher(lambda *a: None, asyncio.new_event_loop())  # type: ignore[arg-type]
    cw.watch_root("proj", root)
    # a code file under the watched root → (project, root, relpath, deleted)
    f = root / "src" / "a.py"; f.write_text("x=1")
    assert cw._decode(str(f), False) == ("proj", str(root.resolve()), "src/a.py", False)
    # a delete event for a file that still EXISTS is FSEvents rename-save noise
    # → recorded as a change (trusting it wiped whole files' symbols)
    assert cw._decode(str(f), True) == ("proj", str(root.resolve()), "src/a.py", False)
    # a delete of a genuinely-missing file decodes as deleted
    g = root / "src" / "gone.py"
    assert cw._decode(str(g), True) == ("proj", str(root.resolve()), "src/gone.py", True)
    # a doc under the watched root → routed as an in-situ doc (\x00doc\x00-tagged)
    assert cw._decode(str(root / "README.md"), False) == (
        "proj", str(root.resolve()), "\x00doc\x00README.md", False)
    # a non-code, non-doc extension → ignored
    assert cw._decode(str(root / "a.png"), False) is None
    # junk dir → ignored
    assert cw._decode(str(root / "__pycache__" / "a.py"), False) is None
    # outside any watched root → ignored
    assert cw._decode(str(tmp_path / "elsewhere" / "b.py"), False) is None


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


def test_code_watcher_decode_routes_new_extensionless_files(tmp_path, monkeypatch):
    """A NEW extensionless autoload/dotfile routes by content (shebang/marker),
    like sweep enumeration — it must not wait for the next full sweep."""
    import asyncio

    from crib.watch import CodeWatcher
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / "repo"
    root.mkdir()
    cw = CodeWatcher(lambda *a: None, asyncio.new_event_loop())  # type: ignore[arg-type]
    cw.watch_root("proj", root)
    f = root / "_zdot_helper"                       # autoload: marker, no extension
    f.write_text("#autoload\n_zdot_helper() { :; }\n")
    assert cw._decode(str(f), False) == ("proj", str(root.resolve()),
                                         "_zdot_helper", False)
    g = root / "notes"                              # extensionless NON-code → ignored
    g.write_text("plain text\n")
    assert cw._decode(str(g), False) is None
