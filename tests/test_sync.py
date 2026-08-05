"""Git-backed sharing: init, sync/push/pull against a local bare remote, and
conflict detection. Uses two clones to simulate two machines."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from crib import gitbacking
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


# ── a failed commit must stop the sync (3.1) ─────────────────────────────────

def _break_identity(g: GitBacking, tmp_path: Path, monkeypatch) -> None:
    """Make `git commit` fail for a reason crib can't paper over: no identity,
    and no auto-detection or outer config to fall back on."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-such-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-such-gitconfig"))
    git(g.data_dir, "config", "user.useConfigOnly", "true")
    git(g.data_dir, "config", "--unset", "user.name")
    git(g.data_dir, "config", "--unset", "user.email")


def test_snapshot_reports_commit_failure_structurally(tmp_path, remote, monkeypatch):
    g = _machine(tmp_path, "a", remote)
    _note(g, "x.md", "body\n")
    _break_identity(g, tmp_path, monkeypatch)

    res = g.snapshot_result("try")
    assert not res.ok and not res.committed
    assert "identity" in res.message.lower() or "email" in res.message.lower()


def test_sync_stops_when_the_commit_fails(tmp_path, remote, monkeypatch):
    # the string-sniff bug: a FAILED commit read as "committed", then sync
    # pulled onto the still-dirty tree
    a = _machine(tmp_path, "a", remote)
    _note(a, "seed.md", "# seed\n")
    a.sync("seed")
    b = _machine(tmp_path, "b", remote)
    b.sync()

    _note(b, "local.md", "# local\n")
    _break_identity(b, tmp_path, monkeypatch)
    res = b.sync("b edit")

    assert not res.ok and not res.committed and not res.pushed
    assert "sync stopped" in res.message           # the real git error, not prose
    assert "email" in res.message.lower() or "identity" in res.message.lower()
    assert git(b.data_dir, "status", "--porcelain").strip()   # nothing merged over it


def test_snapshot_with_nothing_to_do_is_ok_but_not_committed(tmp_path, remote):
    g = _machine(tmp_path, "a", remote)
    _note(g, "x.md", "body\n")
    assert g.snapshot_result("first").committed
    res = g.snapshot_result("again")
    assert res.ok and not res.committed and "nothing to" in res.message


# ── unborn HEAD adopts the remote's branch (3.2) ─────────────────────────────

@pytest.fixture()
def master_remote(tmp_path):
    """A populated remote whose HEAD is `master`, not `main`."""
    bare = tmp_path / "master-remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "master", str(bare)],
                   check=True, capture_output=True)
    seed = tmp_path / "master-seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=seed, check=True)
    (seed / "projects").mkdir()
    (seed / "projects" / "note.md").write_text("# from master\n")
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(seed, "config", k, v)
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "seed")
    git(seed, "push", f"file://{bare}", "master")
    return bare


def test_unborn_head_takes_the_branch_from_the_remote(tmp_path, master_remote):
    d = tmp_path / "joiner"
    d.mkdir()
    g = GitBacking(d)
    g.init(f"file://{master_remote}")        # init + fetch, still no commits here
    assert g._branch() == "master"           # not the hardcoded "main"


def test_setup_joins_a_master_remote_on_master(tmp_path, master_remote):
    d = tmp_path / "joiner2"
    d.mkdir()
    g = GitBacking(d)
    res = g.setup(f"file://{master_remote}")
    assert res.ok and res.pulled
    assert git(d, "rev-parse", "--abbrev-ref", "HEAD").strip() == "master"
    assert (d / "projects" / "note.md").exists()


def test_empty_remote_still_falls_back_to_the_local_default(tmp_path, remote):
    g = _machine(tmp_path, "a", remote)      # bare remote has no branches yet
    assert g._branch() == "main"


# ── bounded, non-interactive git (3.3) ───────────────────────────────────────

def test_git_runs_are_bounded_and_get_no_stdin(tmp_path, remote, monkeypatch):
    g = _machine(tmp_path, "a", remote)
    calls: list[dict] = []
    real = subprocess.run

    def spy(cmd, **kw):
        calls.append({"cmd": cmd, **kw})
        return real(cmd, **kw)

    monkeypatch.setattr(gitbacking.subprocess, "run", spy)
    g.state()
    g._run("fetch", "origin")
    assert calls, "no git ran"
    assert all(c["stdin"] is subprocess.DEVNULL for c in calls)   # no prompt can block
    assert all(c["timeout"] for c in calls)
    local = [c for c in calls if c["cmd"][1] != "fetch"]
    assert all(c["timeout"] == gitbacking._LOCAL_TIMEOUT for c in local)
    assert [c for c in calls if c["cmd"][1] == "fetch"][0]["timeout"] \
        == gitbacking._NET_TIMEOUT


def test_a_timeout_surfaces_as_a_failed_run_not_a_hang(tmp_path, remote, monkeypatch):
    g = _machine(tmp_path, "a", remote)

    def wedged(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))

    monkeypatch.setattr(gitbacking.subprocess, "run", wedged)
    r = g._run("status", "--porcelain")
    assert r.returncode == gitbacking._TIMEOUT_RC and "timed out" in r.stderr
    res = g.push()
    assert not res.ok and "timed out" in res.message


def test_daemon_side_backing_refuses_network_git(tmp_path, remote):
    """DESIGN §14: the daemon never runs fetch/pull/push — auth lives in the
    user's terminal. Local checkpoints stay available."""
    live = _machine(tmp_path, "a", remote)
    d = GitBacking(live.data_dir, allow_network=False)
    for res in (d.pull(), d.push(), d.sync("x"), d.setup(f"file://{remote}")):
        assert not res.ok and "CLI-side" in res.message
    assert d._run("fetch", "origin").returncode != 0
    _note(d, "n.md", "local edit\n")
    assert d.snapshot_result("local").committed      # snapshot/history still work
    assert d.history()


def test_memory_bindings_is_gitignored(tmp_path, remote):
    a = _machine(tmp_path, "a", remote)
    (a.data_dir / "memory-bindings.json").write_text('[{"root":"/x","project":"p"}]')
    _note(a, "n.md", "note")
    a.sync()
    # the machine-specific bindings file must not be tracked
    assert "memory-bindings.json" not in git(a.data_dir, "ls-files")
