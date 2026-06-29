"""Mirroring Claude Code harness memory: munge, discovery, sync + reconcile."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from crib import claudemem
from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore


def test_munge_matches_harness_rule():
    # the harness encodes its launch path with every '/' and '.' -> '-'. Holds on
    # both OSes: on macOS the firmlink strip turns realpath's /System/Volumes/Data
    # /home back into /home, so we don't pick up the volume prefix.
    assert claudemem.munge(Path("/home/u/Development/cribsheet")) == \
        "-home-u-Development-cribsheet"
    assert claudemem.munge(Path("/home/u/.cache/x")) == "-home-u--cache-x"


def test_resolve_follows_symlinks(tmp_path):
    # symlinks ARE resolved (matching the harness's getcwd)
    target = tmp_path / "real"; target.mkdir()
    link = tmp_path / "link"; link.symlink_to(target)
    assert claudemem.resolve_path(link) == claudemem.resolve_path(target)


def test_firmlink_strip_is_boundary_safe():
    # …but the macOS Data-volume firmlink prefix is stripped, not followed
    from crib.claudemem import _FIRMLINK
    assert _FIRMLINK.sub("", "/System/Volumes/Data/Users/u/x") == "/Users/u/x"
    assert _FIRMLINK.sub("", "/Users/u/x") == "/Users/u/x"            # no-op when absent
    assert _FIRMLINK.sub("", "/System/Volumes/Database") == \
        "/System/Volumes/Database"                                   # word-boundary safe


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "idx"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    repo = tmp_path / "repo"
    repo.mkdir()
    mem = claudemem.harness_memory_dir(repo)   # uses the patched CLAUDE_CONFIG_DIR
    crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
    return crib, repo, mem


def run(coro):
    return asyncio.run(coro)


def test_find_harness_root_walks_up(env):
    _, repo, mem = env
    _write(mem / "a.md", "# a\nalpha")
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    assert claudemem.find_harness_root(sub) == repo.resolve()


def test_sync_mirrors_indexes_and_tags(env):
    crib, repo, mem = env
    _write(mem / "decisions.md",
           "---\nname: decisions\nmetadata:\n  type: project\n---\nuse RRF fusion")
    _write(mem / "MEMORY.md", "# index\n- skip me")   # the TOC must be excluded

    res = run(crib.import_claude_memory(project="p", root=repo))
    assert res["synced"] == 1 and res["removed"] == 0   # MEMORY.md skipped

    # mirrored under notes/claude-memory/<host>/, searchable, provenance + type tag
    rel = f"claude-memory/{claudemem.hostslug()}/decisions.md"
    hits = crib.lookup("RRF fusion", project="p")
    assert hits and hits[0].relpath == rel
    text = crib.read_note(rel, project="p")
    assert "source: claude_memory" in text and "claude-memory" in text

    # a binding was recorded for the daemon's live mirror
    assert any(b["root"] == str(repo.resolve()) and b["project"] == "p"
               for b in crib.memory_bindings.all())


def test_resync_is_idempotent_and_reconciles_deletes(env):
    crib, repo, mem = env
    _write(mem / "a.md", "# a\nalpha fact")
    _write(mem / "b.md", "# b\nbeta fact")
    run(crib.import_claude_memory(project="p", root=repo))

    host = claudemem.hostslug()
    # id is preserved across re-sync (identity/history survive)
    id1 = _note_id(crib, f"claude-memory/{host}/a.md")
    run(crib.import_claude_memory(project="p", root=repo))
    assert _note_id(crib, f"claude-memory/{host}/a.md") == id1

    # delete b upstream -> reconcile drops it here
    (mem / "b.md").unlink()
    res = run(crib.import_claude_memory(project="p", root=repo))
    assert res["removed"] == 1
    assert not (crib.notes_dir("p") / "claude-memory" / host / "b.md").exists()
    # b's chunks are gone from the index (hash embedder still matches a on
    # shared tokens, so assert b specifically is absent, not "no hits")
    assert not any(h.relpath == f"claude-memory/{host}/b.md"
                   for h in crib.lookup("beta fact", project="p", k=10))


def _note_id(crib: Crib, relpath: str) -> str:
    from crib import notes
    return notes.load(crib.abspath("p", relpath)).id or ""
