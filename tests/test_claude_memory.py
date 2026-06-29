"""Mirroring Claude Code harness memory: munge, discovery, sync + reconcile."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from crib import claudemem
from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore


def test_munge_matches_harness_rule():
    # the harness encodes its getcwd launch path with every '/' and '.' -> '-'.
    # Assert against each platform's NATIVE project root (which resolve()s
    # transparently): /home is a Linux notion; on macOS real projects live under
    # /Users, and /home would hit an autofs mount instead.
    root = "/Users/u" if sys.platform == "darwin" else "/home/u"
    flat = root.replace("/", "-")
    assert claudemem.munge(Path(f"{root}/Development/cribsheet")) == \
        f"{flat}-Development-cribsheet"
    assert claudemem.munge(Path(f"{root}/.cache/x")) == f"{flat}--cache-x"


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
