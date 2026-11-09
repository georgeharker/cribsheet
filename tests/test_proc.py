"""crib.proc: child processes started without fork().

On macOS, once a process has used Apple's Network.framework (DuckDB's Quack
client does), its fork handler crashes the child of every later fork() before
exec. These tests pin that crib.proc takes CPython's posix_spawn path and keeps
subprocess's behaviour otherwise."""

from __future__ import annotations

import subprocess
import sys

import pytest

from crib import proc


def test_runs_and_captures():
    r = proc.run([sys.executable, "-c", "print('hi')"], capture_output=True, text=True)
    assert (r.returncode, r.stdout) == (0, "hi\n")


def test_resolves_a_bare_program_name():
    r = proc.run(["git", "--version"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.startswith("git version")


def test_cwd_is_applied(tmp_path):
    (tmp_path / "marker").write_text("x")
    r = proc.run(
        [sys.executable, "-c", "import os; print(sorted(os.listdir('.')))"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert r.stdout.strip() == "['marker']"


def test_cwd_with_awkward_characters(tmp_path):
    d = tmp_path / 'it\'s a "dir" $HOME ;'
    d.mkdir()
    r = proc.run(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        cwd=d,
        capture_output=True,
        text=True,
    )
    assert r.stdout.strip().endswith(d.name)


def test_arguments_pass_through_untouched(tmp_path):
    args = ["$HOME", "a b", "'q'", '"dq"', "*", ";rm -rf /"]
    for cwd in (None, tmp_path):
        r = proc.run(
            [sys.executable, "-c", "import sys; print(sys.argv[1:])", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        assert r.stdout.strip() == repr(args)


def test_missing_program_raises_like_subprocess():
    with pytest.raises(FileNotFoundError):
        proc.run(["no-such-program-crib-test"])


def test_missing_cwd_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        proc.run([sys.executable, "-c", "pass"], cwd=tmp_path / "absent")


def test_exit_code_and_timeout_behave_like_subprocess(tmp_path):
    assert proc.run([sys.executable, "-c", "raise SystemExit(3)"]).returncode == 3
    with pytest.raises(subprocess.TimeoutExpired):
        proc.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            timeout=0.5,
        )


def test_popen_pipes(tmp_path):
    p = proc.popen(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write(sys.stdin.read().upper())",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    out, _ = p.communicate("quack", timeout=30)
    assert out == "QUACK"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"preexec_fn": lambda: None},
        {"start_new_session": True},
        {"pass_fds": (3,)},
        {"umask": 0},
        {"close_fds": True},
        {"executable": "/bin/sh"},
    ],
    ids=[
        "preexec_fn",
        "start_new_session",
        "pass_fds",
        "umask",
        "close_fds",
        "executable",
    ],
)
def test_options_that_force_fork_are_refused(kwargs):
    with pytest.raises(ValueError, match="fork|close_fds"):
        proc.run([sys.executable, "-c", "pass"], **kwargs)


def test_takes_the_posix_spawn_path(monkeypatch, tmp_path):
    """The point of the module: CPython's _posix_spawn runs, its fork path never
    does — with and without a cwd."""
    spawned, forked = [], []
    # private CPython method, absent from the stubs: reach it by name
    real_spawn = getattr(subprocess.Popen, "_posix_spawn")  # noqa: B009

    def spy_spawn(self, *a, **k):
        spawned.append(1)
        return real_spawn(self, *a, **k)

    import _posixsubprocess

    real_fork_exec = _posixsubprocess.fork_exec

    def spy_fork_exec(*a, **k):
        forked.append(1)
        return real_fork_exec(*a, **k)

    monkeypatch.setattr(subprocess.Popen, "_posix_spawn", spy_spawn)
    monkeypatch.setattr(_posixsubprocess, "fork_exec", spy_fork_exec)
    proc.run(["git", "--version"], capture_output=True)
    proc.run([sys.executable, "-c", "pass"], cwd=tmp_path)
    proc.popen([sys.executable, "-c", "pass"]).wait(30)
    assert (len(spawned), forked) == (3, [])


def test_crib_code_spawns_never_forks(monkeypatch, tmp_path):
    """crib's own process launches go through crib.proc: git (sync/backing),
    merge-file, and the sharedserver CLI."""
    import _posixsubprocess

    forked = []
    real = _posixsubprocess.fork_exec
    monkeypatch.setattr(
        _posixsubprocess,
        "fork_exec",
        lambda *a, **k: (forked.append(a[0]), real(*a, **k))[1],
    )
    from crib.gitbacking import GitBacking
    from crib.merge import merge_note_texts

    g = GitBacking(tmp_path)
    g.init()
    merge_note_texts(
        "---\ntitle: t\n---\nbase\n",
        "---\ntitle: t\n---\nours\n",
        "---\ntitle: t\n---\ntheirs\n",
    )
    assert forked == []
