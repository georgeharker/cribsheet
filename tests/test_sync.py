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
    # the frontmatter merge driver is wired up (committed attribute + local config)
    assert "merge=cribnote" in (g.data_dir / ".gitattributes").read_text()
    assert "merge-driver" in git(g.data_dir, "config", "merge.cribnote.driver")


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


def test_join_seeds_shared_files_from_the_remote(tmp_path, remote):
    """A machine joining a POPULATED remote must not seed its own `.gitignore`/
    `.gitattributes` defaults — when the remote's copies diverge (e.g. another
    crib version wrote them), that both-added-conflicts with the join merge.
    Instead the remote branch's copies are adopted at init time."""
    a = _machine(tmp_path, "a", remote)
    gi = a.data_dir / ".gitignore"
    gi.write_text(gi.read_text() + "# remote-custom-rule\n")   # diverge from the default
    _note(a, "seed.md", "# seed\n")
    assert a.sync("seed").ok

    # machine b: local pre-join notes, joins via the `sync --remote` flow (init + sync)
    d = tmp_path / "b"
    (d / "projects" / "default" / "notes").mkdir(parents=True)
    b = GitBacking(d)
    b.init(f"file://{remote}")                  # fetch sees origin/main → adopt its copies
    for k, v in (("user.email", "t@t"), ("user.name", "t"),
                 ("init.defaultBranch", "main")):
        git(d, "config", k, v)
    git(d, "checkout", "-b", "main")
    assert "# remote-custom-rule" in (d / ".gitignore").read_text()   # theirs, not default

    _note(b, "local.md", "# local\n")
    res = b.sync("join")                        # commit local → pull (join) → push
    assert res.ok and not res.conflicts         # identical add/add can't conflict
    assert "# remote-custom-rule" in (d / ".gitignore").read_text()
    assert (d / "projects/default/notes/seed.md").exists()
    assert "merge=cribnote" in (d / ".gitattributes").read_text()


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


def _imported_note(g: GitBacking, name: str, repo: str, date: str, body: str) -> None:
    fm = (f"---\nid: 01ABC\nsource: imported\nsource_repo: {repo}\n"
          f"source_path: docs/{name}\nimported: '{date}'\n---\n\n")
    _note(g, name, fm + body)


def test_frontmatter_only_conflict_auto_resolves(tmp_path, remote):
    # two machines import the same doc with machine-local provenance, identical
    # body → the merge driver resolves the header and the pull NEVER surfaces it
    a = _machine(tmp_path, "a", remote)
    _imported_note(a, "x.md", "$HOME/a", "2026-06-27", "Same body.\n")
    a.sync()

    b = _machine(tmp_path, "b", remote)
    _imported_note(b, "x.md", "$HOME/b", "2026-06-28", "Same body.\n")
    res = b.sync("b import")

    assert res.ok and not res.conflicts          # header-only divergence is silent
    merged = (b.data_dir / "projects/default/notes/x.md").read_text()
    assert "<<<<<<<" not in merged
    assert merged.count("imported:") == 1         # one clean header, no duplicates
    assert "2026-06-27" in merged                 # earliest (first-import) survived


def test_body_conflict_surfaces_but_header_is_healed(tmp_path, remote):
    a = _machine(tmp_path, "a", remote)
    _imported_note(a, "x.md", "$HOME/a", "2026-06-27", "Base body.\n")
    a.sync()
    b = _machine(tmp_path, "b", remote)
    b.sync()                                      # both share the note

    _imported_note(a, "x.md", "$HOME/a", "2026-06-27", "Machine A's rewrite.\n")
    a.sync()
    _imported_note(b, "x.md", "$HOME/b", "2026-06-28", "Machine B's rewrite.\n")
    res = b.sync("b edit")

    assert not res.ok                             # divergent body IS surfaced
    assert any(c.endswith("x.md") for c in res.conflicts)
    conflicted = (b.data_dir / "projects/default/notes/x.md").read_text()
    assert "<<<<<<<" in conflicted                # body markers for the user
    head = conflicted.split("---")[1]             # …but the header is already merged
    assert head.count("imported:") == 1


def test_memory_bindings_is_gitignored(tmp_path, remote):
    a = _machine(tmp_path, "a", remote)
    (a.data_dir / "memory-bindings.json").write_text('[{"root":"/x","project":"p"}]')
    _note(a, "n.md", "note")
    a.sync()
    # the machine-specific bindings file must not be tracked
    assert "memory-bindings.json" not in git(a.data_dir, "ls-files")
