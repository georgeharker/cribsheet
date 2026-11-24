"""Confined mode (`--root` / $CRIB_ROOT): everything under one root, nothing
written outside it, a checkout's own .crib never read or written. Plan
"Overlays 4"; design "Transient projects, out-of-store homes, and code
overlays", part 3 (and part 7: a reopened root reconciles its projects)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from crib.app import Crib, _build_store
from crib.config import Config, ProjectConfig, resolve_project
from crib.errors import CribUserError
from crib.paths import ROOT_ENV, Paths
from crib.store import InMemoryStore


def run(coro):
    return asyncio.run(coro)


def _register_flag_env(monkeypatch):
    """Register every variable crib.entry may set, so monkeypatch removes what
    a flag sets at teardown (delenv of an unset var records nothing)."""
    for v in ("CRIB_ROOT", "CRIB_STORE", "CRIB_WRITABLE"):
        monkeypatch.setenv(v, "x")
        monkeypatch.delenv(v)


@pytest.fixture()
def root(tmp_path, monkeypatch):
    r = tmp_path / "root"
    r.mkdir()
    monkeypatch.setenv(ROOT_ENV, str(r))
    # per-dir overrides LOSE to the root (the index dir, below, is the exception)
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "elsewhere-data"))
    monkeypatch.delenv("CRIB_INDEX_DIR", raising=False)
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


def test_the_store_may_live_outside_the_root(root, tmp_path, monkeypatch):
    """The root is the file set; the store (index dir) is a disposable cache
    that --store ($CRIB_STORE) may put elsewhere. Config and data stay put. An
    inherited $CRIB_INDEX_DIR is ignored: it could point at the trusted index."""
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "trusted-index"))
    assert Paths.resolve().index_dir == root / "index"
    monkeypatch.setenv("CRIB_STORE", str(tmp_path / "scratch"))
    paths = Paths.resolve()
    assert paths.index_dir == tmp_path / "scratch"
    assert paths.duckdb_file.is_relative_to(tmp_path / "scratch")
    assert paths.data_dir == root / "data" and paths.config_dir == root / "config"


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

    from crib import cli, entry

    _register_flag_env(monkeypatch)
    seen = []
    monkeypatch.setattr(
        cli, "_run_inprocess", lambda args, cfg: seen.append(os.environ.get(ROOT_ENV))
    )
    entry.main(["--root", str(tmp_path / "r"), "--no-daemon", "plan", "list", "-p", "x"])
    assert seen == [str((tmp_path / "r").resolve())]


def test_confined_cli_verbs_run_in_process(root, monkeypatch):
    """A sandbox has no warm daemon or sharedserver: confined verbs never
    attach to one, whatever [daemon] says."""
    from crib import cli

    ran = []
    monkeypatch.setattr(cli, "_run_daemon", lambda a, c: ran.append("daemon"))
    monkeypatch.setattr(cli, "_run_inprocess", lambda a, c: ran.append("inprocess"))
    cli.main(["plan", "list", "-p", "x"])
    assert ran == ["inprocess"]


def test_the_entry_point_sets_env_flags_before_crib_loads(monkeypatch):
    """--root/--store/--writable are applied before the CLI is imported (the
    facet registry resolves at import, from the config dir); a subcommand's
    own --store is not a global flag."""
    from crib import entry

    for v in ("CRIB_ROOT", "CRIB_STORE", "CRIB_WRITABLE"):
        monkeypatch.setenv(v, "x")
        monkeypatch.delenv(v)
    entry._apply_env_flags(["--json", "--root", "/tmp/r", "--writable=a",
                            "--writable", "b", "--store", "/tmp/s", "plan", "list"])
    import os

    assert os.environ["CRIB_ROOT"] == str(Path("/tmp/r").resolve())
    assert os.environ["CRIB_STORE"] == str(Path("/tmp/s").resolve())
    assert os.environ["CRIB_WRITABLE"] == "a,b"
    monkeypatch.delenv("CRIB_STORE")
    entry._apply_env_flags(["project", "create", "x", "--store", "notes"])
    assert "CRIB_STORE" not in os.environ


def test_writable_is_refused_when_a_verb_runs_in_the_daemon(tmp_path, monkeypatch, capsys):
    """--writable governs THIS process; a daemon-attached verb would ignore it
    silently, so it is refused (pass --no-daemon)."""
    from crib import cli, entry

    _register_flag_env(monkeypatch)
    for v in ("CRIB_CONFIG_DIR", "CRIB_DATA_DIR", "CRIB_INDEX_DIR"):
        monkeypatch.setenv(v, str(tmp_path / v))
    monkeypatch.setattr(cli, "_run_daemon", lambda a, c: pytest.fail("attached"))
    import crib.sharedserver as ss

    monkeypatch.setattr(ss, "available", lambda: True)
    assert entry.main(["--writable", "a", "plan", "list", "-p", "x"]) == 2
    assert "--no-daemon" in capsys.readouterr().err


def test_abbreviated_env_flags_are_rejected(root):
    """`--ro` would parse as --root but escape crib.entry's env handling (S-1)."""
    from crib import cli

    with pytest.raises(SystemExit):
        cli.main(["--ro", str(root), "plan", "list", "-p", "x"])


def test_cli_main_refuses_env_flags_it_cannot_apply(tmp_path, monkeypatch, capsys):
    from crib import cli

    _register_flag_env(monkeypatch)
    assert cli.main(["--root", str(tmp_path / "r"), "plan", "list", "-p", "x"]) == 2
    assert "crib.entry" in capsys.readouterr().err


def test_lsp_and_grammar_config_come_from_the_root(root, tmp_path, monkeypatch):
    """lsp.json decides which binaries run: confined mode reads the root's (S-2)."""
    from crib import codeindex

    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "trusted-config"))
    assert codeindex._config_dir() == root / "config"


def test_a_subcommands_own_store_option_is_not_the_global_one(tmp_path, monkeypatch):
    """`project create NAME --store REL` (in-repo residency) shares the
    spelling, not the dest, of the global --store (N-1)."""
    from crib import cli

    _register_flag_env(monkeypatch)
    args = cli.build_parser().parse_args(["project", "create", "demo", "--store", ".s"])
    assert args.g_store is None and args.store == ".s"
    assert cli._env_flags_unapplied(args) is None


def test_a_confined_store_conforms_its_facets_at_open(root, monkeypatch):
    """A bundle's store is a disposable cache: declared facets pending in it
    are conformed when a --root crib opens it (never dropping one), so a
    bundle's routines/agents list without a manual `facets sync`."""
    from crib.app import Crib
    from crib.config import Config
    from crib.paths import Paths
    from crib.store import InMemoryStore

    synced: list[bool] = []

    class Pending(InMemoryStore):
        def facet_pending(self):
            return {} if synced else {"routine": "its table is created"}

        def facet_sync(self, drop=False, shared=False):
            synced.append(drop)
            return []

    crib = Crib(Paths.resolve().ensure(), Config(), Pending())
    assert synced == [False] and crib.pending_facets == {}
