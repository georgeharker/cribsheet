"""Confined mode (`--root` / $CRIB_ROOT): everything under one root, nothing
written outside it, a checkout's own .crib never read or written. Plan
"Overlays 4"; design "Transient projects, out-of-store homes, and code
overlays", part 3 (and part 7: a reopened root reconciles its projects)."""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib, _build_store
from crib.config import Config, ProjectConfig, resolve_project
from crib.errors import CribUserError
from crib.paths import ROOT_ENV, Paths
from crib.store import InMemoryStore


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def root(tmp_path, monkeypatch):
    r = tmp_path / "root"
    r.mkdir()
    monkeypatch.setenv(ROOT_ENV, str(r))
    # per-dir overrides must LOSE to the root
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "elsewhere-data"))
    monkeypatch.delenv("CRIB_WRITABLE", raising=False)
    return r.resolve()


@pytest.fixture()
def crib(root):
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


@pytest.fixture()
def checkout(tmp_path):
    """A checkout under review, with a hostile .crib of its own."""
    co = tmp_path / "checkout"
    (co / "pkg").mkdir(parents=True)
    (co / "pkg" / "mod.py").write_text("def f():\n    return 1\n")
    (co / ".git").mkdir()
    (co / ".crib").write_text(
        'project: victim\nstore: ../../escape\npaths:\n  - "nothing/**"\n'
    )
    return co


def test_every_dir_is_under_the_root(root):
    paths = Paths.resolve()
    assert paths.root == root
    assert (paths.config_dir, paths.data_dir, paths.index_dir) == (
        root / "config",
        root / "data",
        root / "index",
    )


def test_the_shared_store_is_refused(root):
    cfg = Config()
    cfg.duckdb.mode = "shared"
    with pytest.raises(CribUserError, match="embedded store only"):
        _build_store(Paths.resolve().ensure(), cfg)


def test_git_history_is_refused(crib):
    with pytest.raises(CribUserError, match="no git history"):
        crib.snapshot("x")


def test_the_cli_refuses_memory_sync(root, capsys):
    from crib import cli

    assert cli.main(["memory", "sync"]) == 1
    assert "confined mode" in capsys.readouterr().err


def test_a_project_must_be_named(root, checkout):
    with pytest.raises(CribUserError, match="name the project"):
        resolve_project(Config(), None, checkout)  # the checkout's .crib is not read
    assert resolve_project(Config(), "copy", checkout) == "copy"


def test_the_checkout_crib_is_ignored_and_never_written(crib, checkout):
    before = (checkout / ".crib").read_text()
    link, created = crib._ensure_crib(checkout, "copy", want_code=True, want_docs=False)
    assert not created and link.project == "copy" and link.root == checkout.resolve()
    assert link.store is None and not link.facets
    assert "nothing/**" not in link.paths  # globs detected, not the checkout's
    assert (checkout / ".crib").read_text() == before
    with pytest.raises(CribUserError, match="name the project"):
        crib._ensure_crib(checkout, None, want_code=True, want_docs=False)


def test_a_trusted_crib_in_the_root_is_used(crib, checkout):
    trusted = crib.paths.project_dir("copy") / ".crib"
    trusted.parent.mkdir(parents=True, exist_ok=True)
    trusted.write_text('project: copy\nstore: ../out\npaths:\n  - "pkg/**/*.py"\n')
    link, _ = crib._ensure_crib(checkout, "copy", want_code=True, want_docs=False)
    assert link.paths == ["pkg/**/*.py"] and link.root == checkout.resolve()
    assert link.store is None  # residency ignored even in the trusted copy
    trusted.write_text("project: other\n")
    with pytest.raises(CribUserError, match="names project 'other'"):
        crib._ensure_crib(checkout, "copy", want_code=True, want_docs=False)


def test_writes_outside_the_root_are_refused(crib, tmp_path):
    outside = tmp_path / "outside-home"
    (outside / "notes").mkdir(parents=True)
    ProjectConfig(name="ctx", store_root=str(outside)).save(
        crib.paths.project_dir("ctx") / ".cribproject"
    )
    crib.project_paths.invalidate("ctx")
    with pytest.raises(CribUserError, match="writes only under its root"):
        run(crib.store_note("x", title="X", project="ctx"))
    assert not any((outside / "notes").iterdir())
    out = run(crib.store_note("y", title="Y", project="copy"))  # inside: fine
    assert (crib.paths.project_dir("copy") / "notes" / out["relpath"]).exists()


def test_watch_roots_outside_the_root_are_dropped(crib, tmp_path):
    inside = crib.paths.data_dir / "x"
    roots = {"a": inside, "b": tmp_path / "outside"}
    assert crib._in_root(roots) == {"a": inside}


def test_a_reopened_root_reconciles_its_transient_projects(crib):
    run(crib.project_create(project="copy", transient=True))
    run(crib.store_note("kept across sessions", title="Review note", project="copy"))
    # a new session on the same root: a fresh (empty) store, same files
    again = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
    run(again.reconcile_all())
    hits = again.lookup("kept across sessions", project="copy")
    assert any(h.relpath.startswith("review-note") for h in hits)


def test_the_cli_flag_sets_the_root(tmp_path, monkeypatch):
    import os

    from crib import cli

    # set-then-delete so monkeypatch RECORDS the var and removes what
    # cli.main sets at teardown (delenv of an unset var records nothing)
    monkeypatch.setenv(ROOT_ENV, "x")
    monkeypatch.delenv(ROOT_ENV)
    seen = []
    monkeypatch.setattr(
        cli, "_run_inprocess", lambda args, cfg: seen.append(os.environ.get(ROOT_ENV))
    )
    cli.main(["--root", str(tmp_path / "r"), "--no-daemon", "plan", "list", "-p", "x"])
    assert seen == [str((tmp_path / "r").resolve())]
