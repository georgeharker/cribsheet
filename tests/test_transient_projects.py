"""Transient projects: made for one job, discarded whole (plan "Overlays 2";
design "Transient projects, out-of-store homes, and code overlays")."""

from __future__ import annotations

import asyncio
import datetime

import pytest

from crib.app import Crib
from crib.expiry import parse as _expiry
from crib.config import Config, ProjectConfig
from crib.errors import CribUserError
from crib.paths import Paths
from crib.store import InMemoryStore


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.delenv("CRIB_WRITABLE", raising=False)
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def test_create_records_transient_and_expiry(crib):
    out = run(crib.project_create(project="scratch", transient=True, expires="2h"))
    assert out["project_created"] and out["transient"]
    pdir = crib.paths.project_dir("scratch")
    stub = ProjectConfig.load(pdir / ".cribproject", "scratch")
    assert stub.transient and stub.expires == out["expires"]
    assert (pdir / ".gitignore").read_text().strip() == "*"  # never git-synced
    assert crib.project_paths("scratch").transient


def test_a_plain_create_is_not_transient(crib):
    run(crib.project_create(project="kept"))
    assert not crib.project_paths("kept").transient
    assert not (crib.paths.project_dir("kept") / ".gitignore").exists()


def test_bad_combinations_are_refused(crib):
    with pytest.raises(CribUserError, match="transient too"):
        run(crib.project_create(project="x", expires="1h"))
    with pytest.raises(CribUserError, match="repo residency"):
        run(crib.project_create(project="x", store="notes", cwd=crib.paths.data_dir,
                                transient=True))
    with pytest.raises(CribUserError, match="neither an ISO time"):
        run(crib.project_create(project="x", transient=True, expires="soon"))


def test_expiry_parsing():
    now = datetime.datetime.now(datetime.UTC)
    at = datetime.datetime.fromisoformat(_expiry("30m"))
    assert datetime.timedelta(minutes=29) < at - now < datetime.timedelta(minutes=31)
    assert _expiry("2030-01-02T03:04:05") == "2030-01-02T03:04:05+00:00"
    assert _expiry("2030-01-02T03:04:05+02:00") == "2030-01-02T01:04:05+00:00"


def test_a_transient_project_keeps_no_version_history(crib):
    run(crib.project_create(project="scratch", transient=True))
    out = run(crib.store_note("first", title="T", project="scratch"))
    run(crib.edit_note(out["relpath"], "second", project="scratch"))
    assert crib.list_versions(out["relpath"], project="scratch") == []
    # contrast: an ordinary project, same edit, keeps the prior version
    kept = run(crib.store_note("first", title="T", project="kept"))
    run(crib.edit_note(kept["relpath"], "second", project="kept"))
    assert len(crib.list_versions(kept["relpath"], project="kept")) == 1


def test_the_startup_reconcile_skips_transient_projects(crib):
    run(crib.project_create(project="scratch", transient=True))
    run(crib.store_note("x", title="T", project="scratch"))
    run(crib.store_note("y", title="U", project="kept"))
    seen = []
    orig = crib.reindex

    async def spy(relpath=None, project=None, cwd=None, full=True, **kw):
        seen.append(project)
        return await orig(relpath, project, cwd, full, **kw)

    crib.reindex = spy
    run(crib.reconcile_all())
    assert "scratch" not in seen and "kept" in seen


def test_delete_discards_without_trash(crib):
    run(crib.project_create(project="scratch", transient=True))
    run(crib.store_note("x", title="T", project="scratch"))
    out = run(crib.project_delete(project="scratch"))
    assert out.get("trash") is None
    assert not crib.paths.project_dir("scratch").exists()
    trash = crib.paths.trash_dir
    assert not trash.exists() or not any(trash.glob("scratch-*"))


def test_the_sweep_deletes_only_expired_transients(crib):
    run(crib.project_create(project="due", transient=True,
                            expires="2020-01-01T00:00:00"))
    run(crib.project_create(project="later", transient=True, expires="1d"))
    run(crib.project_create(project="forever", transient=True))
    run(crib.project_create(project="kept"))
    deleted = run(crib.delete_expired_transients())
    assert deleted == ["due"]
    assert set(crib.projects()) >= {"later", "forever", "kept"}
    assert "due" not in crib.projects()


def test_a_synced_stub_claiming_transient_is_not_discarded(crib):
    """A `.cribproject` arriving by git sync (or a hand edit) that says
    transient and expired, but without the birth .gitignore or this host, is
    neither swept nor deleted without trash."""
    run(crib.store_note("precious", title="P", project="synced"))
    stub = crib.paths.project_dir("synced") / ".cribproject"
    pcfg = ProjectConfig.load(stub, "synced")
    pcfg.transient, pcfg.expires, pcfg.host = True, "2020-01-01T00:00:00+00:00", "other-host"
    pcfg.save(stub)
    crib.project_paths.invalidate("synced")
    assert crib.expired_transients() == []
    out = run(crib.project_delete(project="synced"))
    assert out.get("trash")  # an ordinary, undoable delete


def test_expiry_errors_are_user_errors():
    with pytest.raises(CribUserError):
        _expiry("99999999999d")


def test_a_string_false_is_not_transient(crib, tmp_path):
    stub = tmp_path / ".cribproject"
    stub.write_text('name: x\ntransient: "false"\n')
    assert not ProjectConfig.load(stub, "x").transient
    stub.write_text("name: x\ntransient: true\n")
    assert ProjectConfig.load(stub, "x").transient


def test_a_failing_transient_delete_is_reported_once(crib, monkeypatch, capsys):
    run(crib.project_create(project="due", transient=True, expires="2020-01-01T00:00:00"))

    async def stuck(project=None, force=False, cwd=None):
        raise OSError("busy")

    monkeypatch.setattr(crib, "project_delete", stuck)
    assert run(crib.delete_expired_transients()) == []
    assert run(crib.delete_expired_transients()) == []
    assert capsys.readouterr().err.count("not deleted yet") == 1
