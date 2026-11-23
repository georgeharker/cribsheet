"""Homes outside the store: a project whose data tier lives at an arbitrary
absolute path (here a READ-ONLY directory, as a container's read-only mount
of a context project would be). Plan "Overlays 3"; design "Transient
projects, out-of-store homes, and code overlays", parts 1 and 6."""

from __future__ import annotations

import asyncio
import os
import stat

import pytest

from crib.app import Crib
from crib.config import Config, ProjectConfig
from crib.paths import Paths
from crib.store import InMemoryStore


def run(coro):
    return asyncio.run(coro)


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.fixture()
def context_home(tmp_path, monkeypatch):
    """`ctx`'s files on a read-only directory outside the crib store; its stub
    points there; this instance may write only `copy`."""
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.delenv("CRIB_WRITABLE", raising=False)
    home = tmp_path / "ro-mount" / "ctx"
    (home / "notes").mkdir(parents=True)
    (home / "notes" / "widget.md").write_text(
        "---\ntitle: Widget protocol\nid: 01JTESTWIDGET0000000000000\n---\n"
        "# Widget protocol\n\nThe widget handshake uses a sprocket token.\n")
    (home / "notes" / "no-id.md").write_text(
        "---\ntitle: Gear ratios\n---\n# Gear ratios\n\nThe flange gear runs 3:1.\n")
    (home / "design").mkdir()
    paths = Paths.resolve().ensure()
    stub = ProjectConfig(name="ctx", store_root=str(home))
    stub.save(paths.project_dir("ctx") / ".cribproject")
    cfg = Config()
    cfg.writable = ["copy"]
    crib = Crib(paths, cfg, InMemoryStore())
    before = _tree(home)
    for d in (home, *home.rglob("*")):
        if d.is_dir():
            d.chmod(stat.S_IRUSR | stat.S_IXUSR)
    yield crib, home, before
    for d in (home, *home.rglob("*")):
        if d.is_dir():
            d.chmod(stat.S_IRWXU)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_read_only_home_indexes_and_reads_and_is_never_written(context_home):
    crib, home, before = context_home
    assert crib.project_paths("ctx").store_root == home
    run(crib.reindex(project="ctx"))
    hits = crib.lookup("sprocket token handshake", project="ctx")
    assert any("widget" in h.relpath for h in hits)
    assert crib.list_items("ctx", facet="note")["total"] == 1
    # a note with no `id:` is not stamped (the home is read-only) or indexed: it
    # is reported, the same rule as a read-only mapped location
    out = run(crib.reindex(project="ctx"))
    assert out.get("unstamped") == ["no-id.md"], out
    assert "sprocket" in crib.read_note("widget.md", project="ctx")
    run(crib.reconcile_all())
    assert _tree(home) == before  # nothing was written into the read-only home


def test_writes_to_the_context_project_are_refused(context_home):
    from crib.access import ReadOnlyProject

    crib, _, _ = context_home
    with pytest.raises(ReadOnlyProject):
        run(crib.store_note("x", title="X", project="ctx"))
