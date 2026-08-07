"""Watcher reindexes external edits and is harmless on echoes (DESIGN §4, §9)."""

from __future__ import annotations

import asyncio
from pathlib import Path

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
    assert decode(projects, str(p)) == ("notes", "notes", "sub/a.md")
    # not under a pillar dir -> ignored
    other = projects / "notes" / ".cribproject"
    other.write_text("name: notes")
    assert decode(projects, str(other)) is None


def test_decode_covers_every_pillar_sibling(tmp_path):
    projects = tmp_path / "projects"
    for seg, rel in (("design", "d.md"), ("plans", "p.md"), ("learnings", "l.md")):
        f = projects / "proj" / seg / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
        assert decode(projects, str(f)) == ("proj", seg, rel), seg
    # a legacy pre-migration path decodes through the notes branch, so
    # stragglers pulled from an unmigrated machine keep indexing
    legacy = projects / "proj" / "notes" / "design" / "old.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("x")
    assert decode(projects, str(legacy)) == ("proj", "notes", "design/old.md")
    # a non-pillar top-level dir is not content
    other = projects / "proj" / "symbol_index" / "x.md"
    other.parent.mkdir(parents=True)
    other.write_text("x")
    assert decode(projects, str(other)) is None


def test_decode_store_routes_by_data_root_and_pillar(tmp_path):
    from crib.watch import decode_store
    root = tmp_path / "repo" / ".crib-store"
    for seg, rel in (("notes", "n.md"), ("design", "d.md")):
        f = root / seg / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
        assert decode_store({"p": root}, str(f)) == ("p", seg, rel), seg
    # store-root clutter (not under a pillar) is ignored
    top = root / "stray.md"
    top.write_text("x")
    assert decode_store({"p": root}, str(top)) is None


def test_code_watcher_decode_is_pure_path_work(tmp_path, monkeypatch):
    """`_decode` runs on the watchdog EVENT THREAD, so it classifies by path only —
    no stat, no read, no `.crib` parse (all of that moved to `_resolve_batch`)."""
    from pathlib import Path as _P

    from crib.watch import CodeWatcher
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    cw = CodeWatcher(lambda *a: None, asyncio.new_event_loop())  # type: ignore[arg-type]
    cw.watch_root("proj", root)
    rk = str(root.resolve())
    cw._code_exts()             # primed by `start()` in the daemon (see _watch_dirs)
    # any filesystem touch from the event thread is a bug — record and assert none
    touched: list[str] = []

    def _spy(name, fn):
        def wrapper(self, *a, **k):
            touched.append(name)
            return fn(self, *a, **k)
        return wrapper

    for attr in ("exists", "is_file", "read_text", "stat"):
        monkeypatch.setattr(_P, attr, _spy(attr, getattr(_P, attr)))
    f = root / "src" / "a.py"
    assert cw._decode(str(f), False) == ("proj", rk, "src/a.py", False, "code")
    # the delete FLAG is carried through verbatim; verification is deferred
    assert cw._decode(str(f), True) == ("proj", rk, "src/a.py", True, "code")
    # a doc-EXTENSION file is classified "doc"; the `docs:` globs decide later
    assert cw._decode(str(root / "README.md"), False) == (
        "proj", rk, "README.md", False, "doc")
    # extensionless → "sniff": routed by content, but not from this thread
    assert cw._decode(str(root / "_helper"), False) == (
        "proj", rk, "_helper", False, "sniff")
    # a non-code, non-doc extension → ignored
    assert cw._decode(str(root / "a.png"), False) is None
    # junk dir → ignored
    assert cw._decode(str(root / "__pycache__" / "a.py"), False) is None
    # outside any watched root → ignored
    assert cw._decode(str(tmp_path / "elsewhere" / "b.py"), False) is None
    assert touched == []


def test_code_watcher_resolve_batch_applies_the_io_half(tmp_path):
    """The deferred half: existence re-check, `docs:` globs, content sniff — run
    ONCE per coalesced burst, on a worker thread, not per event."""
    from crib.watch import CodeWatcher
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    rk = str(root.resolve())
    (root / "src" / "a.py").write_text("x=1")
    (root / "README.md").write_text("# hi")
    (root / "scratch.md").write_text("notes")
    (root / "docs" / "guide.md").write_text("# guide")
    (root / ".crib").write_text(
        'project: proj\ndocs:\n  - "README.md"\n  - "docs/**/*.md"\n')
    resolve = CodeWatcher._resolve_batch
    # a delete event for a file that still EXISTS is FSEvents rename-save noise
    # → recorded as a change (trusting it wiped whole files' symbols)
    assert resolve({"src/a.py": (rk, True, "code")}) == {"src/a.py": (rk, False)}
    # a delete of a genuinely-missing file stays deleted
    assert resolve({"src/gone.py": (rk, True, "code")}) == {"src/gone.py": (rk, True)}
    # docs are scoped to the `.crib docs:` globs (same as the sweep): a MATCHING
    # doc routes as an in-situ doc (\x00doc\x00-tagged), one outside is dropped
    assert resolve({"README.md": (rk, False, "doc"),
                    "docs/guide.md": (rk, False, "doc"),
                    "scratch.md": (rk, False, "doc")}) == {
        "\x00doc\x00README.md": (rk, False),
        "\x00doc\x00docs/guide.md": (rk, False)}


def test_code_watcher_resolve_batch_routes_new_extensionless_files(tmp_path,
                                                                   monkeypatch):
    """A NEW extensionless autoload/dotfile routes by content (shebang/marker),
    like sweep enumeration — it must not wait for the next full sweep."""
    from crib.watch import CodeWatcher
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / "repo"
    root.mkdir()
    rk = str(root.resolve())
    (root / "_zdot_helper").write_text("#autoload\n_zdot_helper() { :; }\n")
    (root / "notes").write_text("plain text\n")
    assert CodeWatcher._resolve_batch({
        "_zdot_helper": (rk, False, "sniff"),
        "notes": (rk, False, "sniff"),          # extensionless NON-code → dropped
    }) == {"_zdot_helper": (rk, False)}


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


def test_watcher_indexes_an_edit_in_a_pillar_sibling(tmp_path, monkeypatch):
    """An external write into `projects/<p>/design/` lands in the DESIGN store —
    the raw-edit half of the taint guarantee, post-split."""
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())

    async def scenario():
        crib.start_watchers(asyncio.get_running_loop())
        dd = crib.designstore.dir("p")
        (dd / "external.md").write_text(
            "---\ntitle: ext\ntype: design\n---\nA quorum write is the rule.")
        for _ in range(50):
            await asyncio.sleep(0.1)
            if crib.lookup("quorum write rule", project="p", store="design"):
                break
        crib.stop_watchers()
        return (crib.lookup("quorum write rule", project="p", store="design"),
                crib.lookup("quorum write rule", project="p"))

    design_hits, note_hits = asyncio.run(scenario())
    assert design_hits and design_hits[0].relpath == "external.md"
    assert design_hits[0].store == "design"
    assert not note_hits                    # never leaks into the notes pillar


def test_dispatch_tasks_are_held_and_failures_logged(capsys):
    """asyncio holds tasks only weakly, so a dispatch nobody references can be
    GC'd mid-flight (a save's reindex silently lost) — and a dispatch that raises
    must say so instead of dying inside an unobserved task."""
    from crib.watch import Watcher

    async def scenario():
        started = asyncio.Event()

        async def on_change(project, store, relpath):
            started.set()
            await asyncio.sleep(0.02)
            raise RuntimeError("index blew up")

        w = Watcher(Path("/nowhere"), on_change, asyncio.get_running_loop())
        w._fire(("p", "notes", "a.md"))
        await started.wait()
        assert w._tasks                       # strong ref held while in flight
        await asyncio.sleep(0.1)
        assert not w._tasks                   # released on completion
    asyncio.run(scenario())
    assert "index blew up" in capsys.readouterr().err
