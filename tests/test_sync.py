"""Git-backed sharing: init, sync/push/pull against a local bare remote, and
conflict detection. Uses two clones to simulate two machines."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from crib.gitbacking import GitBacking


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return r.stdout


@pytest.fixture()
def remote(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    return bare


def _machine(tmp_path: Path, name: str, remote: Path) -> GitBacking:
    d = tmp_path / name
    (d / "projects" / "default" / "notes").mkdir(parents=True)
    g = GitBacking(d)
    g.init(f"file://{remote}")
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("init.defaultBranch", "main")):
        git(d, "config", k, v)
    git(d, "checkout", "-b", "main")
    return g


def _note(g: GitBacking, name: str, body: str) -> None:
    p = g.data_dir / "projects" / "default" / "notes" / name
    p.write_text(body)


def test_init_writes_gitignore_and_remote(tmp_path, remote):
    g = _machine(tmp_path, "a", remote)
    assert (g.data_dir / ".gitignore").exists()
    assert "memory-bindings.json" in (g.data_dir / ".gitignore").read_text()
    assert ".versions" not in (g.data_dir / ".gitignore").read_text()  # ring IS synced
    assert "origin" in git(g.data_dir, "remote")


def test_sync_round_trips_between_two_machines(tmp_path, remote):
    a = _machine(tmp_path, "a", remote)
    _note(a, "alpha.md", "# Alpha\nfrom machine a")
    res = a.sync("add alpha")
    assert res.ok and res.pushed

    b = _machine(tmp_path, "b", remote)
    res = b.sync()                       # pulls a's note
    assert res.ok
    assert (b.data_dir / "projects/default/notes/alpha.md").exists()
    assert res.changed                   # the pull brought new files


def test_pull_reports_conflicts_without_pushing(tmp_path, remote):
    a = _machine(tmp_path, "a", remote)
    _note(a, "x.md", "base\n")
    a.sync()
    b = _machine(tmp_path, "b", remote)
    b.sync()                             # both now share x.md = "base"

    _note(a, "x.md", "a edit\n"); a.sync()
    _note(b, "x.md", "b edit\n")         # divergent edit to the same line
    res = b.sync("b edit")
    assert not res.ok
    assert any(c.endswith("x.md") for c in res.conflicts)
    assert not res.pushed                # must not push a conflicted tree
    assert "resolve" in res.message.lower()


def test_memory_bindings_is_gitignored(tmp_path, remote):
    a = _machine(tmp_path, "a", remote)
    (a.data_dir / "memory-bindings.json").write_text('[{"root":"/x","project":"p"}]')
    _note(a, "n.md", "note")
    a.sync()
    # the machine-specific bindings file must not be tracked
    assert "memory-bindings.json" not in git(a.data_dir, "ls-files")
