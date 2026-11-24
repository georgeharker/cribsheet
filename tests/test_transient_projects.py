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
        run(
            crib.project_create(
                project="x", store="notes", cwd=crib.paths.data_dir, transient=True
            )
        )
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


def test_a_synced_stub_claiming_transient_is_not_discarded(crib):
    """A `.cribproject` arriving by git sync (or a hand edit) that says
    transient and expired, but without the birth .gitignore or this host, is
    neither swept nor deleted without trash."""
    run(crib.store_note("precious", title="P", project="synced"))
    stub = crib.paths.project_dir("synced") / ".cribproject"
    pcfg = ProjectConfig.load(stub, "synced")
    pcfg.transient, pcfg.expires, pcfg.host = (
        True,
        "2020-01-01T00:00:00+00:00",
        "other-host",
    )
    pcfg.save(stub)
    crib.project_paths.invalidate("synced")
    assert crib._expiry_at("synced") is None  # never scheduled
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


def _sweeper_cycles(crib, monkeypatch, cycles=1):
    """Run `_transient_sweeper` for `cycles` waits, recording each wait's timeout
    (None = until woken), then stop."""
    waits = []

    class Stop(Exception):
        pass

    real = asyncio.wait_for

    async def fake_wait_for(aw, timeout):
        waits.append(timeout)
        if len(waits) >= cycles:
            aw.close()
            raise Stop
        return await real(aw, 0.01)

    monkeypatch.setattr("crib.app.asyncio.wait_for", fake_wait_for)
    with pytest.raises(Stop):
        run(crib._transient_sweeper())
    return waits


def test_the_sweeper_deletes_due_and_waits_until_the_next_expiry(crib, monkeypatch):
    """No polling: due transients are deleted, then the wait is exactly until
    MIN(expiry), the soonest remaining one."""

    run(
        crib.project_create(
            project="due", transient=True, expires="2020-01-01T00:00:00"
        )
    )
    run(crib.project_create(project="soon", transient=True, expires="30m"))
    run(crib.project_create(project="later", transient=True, expires="1d"))
    run(crib.project_create(project="forever", transient=True))
    (timeout,) = _sweeper_cycles(crib, monkeypatch)
    assert "due" not in crib.projects()
    assert {"soon", "later", "forever"} <= set(crib.projects())
    assert 1800 - 60 < timeout <= 1800  # until "soon", not a fixed poll
    assert crib._expiry.expiry_next()[0] == "soon"


def test_with_nothing_scheduled_the_sweeper_waits_until_woken(crib, monkeypatch):
    run(crib.project_create(project="forever", transient=True))
    assert _sweeper_cycles(crib, monkeypatch) == [3600.0]  # capped, not None


def test_a_stub_change_reschedules_and_wakes_the_wait(crib):
    """The watcher's stub hook: an extended expiry moves the row, a deleted
    stub clears it, and either wakes the sweeper."""

    run(crib.project_create(project="t", transient=True, expires="1h"))
    crib._expiry_wake = asyncio.Event()
    stub = crib.paths.project_dir("t") / ".cribproject"
    pcfg = ProjectConfig.load(stub, "t")
    pcfg.expires = "2030-01-01T00:00:00+00:00"
    pcfg.save(stub)
    run(crib._on_stub_change("t"))
    assert crib._expiry_wake.is_set()
    assert (
        crib._expiry.expiry_next()[1]
        == datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC).timestamp()
    )
    run(crib.project_delete(project="t", force=True))
    run(crib._on_stub_change("t"))
    assert crib._expiry.expiry_next() is None


def test_a_failed_scheduled_delete_retries_later(crib, monkeypatch, capsys):
    run(
        crib.project_create(
            project="due", transient=True, expires="2020-01-01T00:00:00"
        )
    )

    async def stuck(project=None, force=False, cwd=None):
        raise OSError("busy")

    monkeypatch.setattr(crib, "project_delete", stuck)
    (timeout,) = _sweeper_cycles(crib, monkeypatch)
    assert 3500 < timeout <= 3600  # retry_s, not a busy loop
    assert "not deleted yet" in capsys.readouterr().err


def test_the_sweeper_does_nothing_in_confined_mode(crib, monkeypatch):
    monkeypatch.setenv("CRIB_ROOT", str(crib.paths.data_dir))
    run(crib._transient_sweeper())  # returns at once


def test_the_watcher_reports_stub_changes(tmp_path):
    from crib.watch import decode_stub

    pd = tmp_path / "projects"
    assert decode_stub(pd, str(pd / "p" / ".cribproject")) == "p"
    assert decode_stub(pd, str(pd / "p" / "notes" / ".cribproject")) is None
    assert decode_stub(pd, str(pd / "p" / "notes" / "x.md")) is None
    assert decode_stub(pd, str(tmp_path / "elsewhere" / ".cribproject")) is None


def test_the_sweeper_deletes_only_due_discardable_transients(crib, monkeypatch):
    """Through the path that runs: a due transient goes; a later one, one with
    no expiry, an ordinary project, and a synced stub from another host stay."""
    run(crib.project_create(project="due", transient=True,
                            expires="2020-01-01T00:00:00"))
    run(crib.project_create(project="later", transient=True, expires="1d"))
    run(crib.project_create(project="forever", transient=True))
    run(crib.project_create(project="kept"))
    run(crib.store_note("precious", title="P", project="synced"))
    stub = crib.paths.project_dir("synced") / ".cribproject"
    pcfg = ProjectConfig.load(stub, "synced")
    pcfg.transient, pcfg.expires, pcfg.host = True, "2020-01-01T00:00:00+00:00", "other-host"
    pcfg.save(stub)
    _sweeper_cycles(crib, monkeypatch)
    assert "due" not in crib.projects()
    assert {"later", "forever", "kept", "synced"} <= set(crib.projects())


def test_an_extension_no_event_reported_is_honoured(crib, monkeypatch):
    """The stub was extended but no watcher event arrived (cached paths, a due
    row): the stub itself is re-read before deleting, so nothing goes."""
    import time

    run(crib.project_create(project="t", transient=True, expires="2020-01-01T00:00:00"))
    crib.project_paths("t")  # cached with the old expiry
    stub = crib.paths.project_dir("t") / ".cribproject"
    pcfg = ProjectConfig.load(stub, "t")
    pcfg.expires = "2030-01-01T00:00:00+00:00"
    pcfg.save(stub)  # no invalidate, no _on_stub_change
    monkeypatch.setattr(crib, "_sync_expiry_all",
                        lambda: crib._expiry.expiry_set("t", time.time() - 10))
    _sweeper_cycles(crib, monkeypatch)
    assert "t" in crib.projects() and stub.exists()


def test_each_wait_is_capped(crib, monkeypatch):
    """The loop's clock stops while a laptop sleeps; a far expiry, or none,
    is still re-checked within the cap (a cheap re-read of the queue)."""
    run(crib.project_create(project="far", transient=True, expires="30d"))
    assert _sweeper_cycles(crib, monkeypatch) == [3600.0]


def test_a_failed_pass_backs_off_and_the_sweeper_survives(crib, monkeypatch, capsys):
    calls = []

    def boom(now):
        calls.append(now)
        raise RuntimeError("store gone")

    monkeypatch.setattr(crib._expiry, "expiry_due", boom)
    waits = _sweeper_cycles(crib, monkeypatch, cycles=2)
    assert waits == [60.0, 60.0] and len(calls) == 2
    assert capsys.readouterr().err.count("scheduled-delete pass failed") == 1


def test_a_rename_moves_the_schedule(crib):
    run(crib.project_create(project="a", transient=True, expires="1d"))
    run(crib.project_rename(project="a", to="b"))
    nxt = crib._expiry.expiry_next()
    assert nxt is not None and nxt[0] == "b"


def test_status_shows_the_next_delete_not_a_running_job(crib):
    """The sweeper is resident: status lists no "running" row for it, but its
    soonest scheduled delete (and how many are scheduled)."""
    run(crib.project_create(project="a", transient=True, expires="2h"))
    run(crib.project_create(project="b", transient=True, expires="1d"))
    crib._expiry_wake = asyncio.Event()  # as when the sweeper runs
    crib._sync_expiry_all()
    nd = crib.status()["next_delete"]
    assert nd["project"] == "a" and nd["scheduled"] == 2
    assert 2 * 3600 - 60 < nd["in_s"] <= 2 * 3600
    crib._expiry.expiry_replace({})
    assert crib.status()["next_delete"] == {"scheduled": 0}


def test_the_sweeper_is_not_listed_as_running(crib):
    from crib.diag import DIAG

    async def main() -> list[str]:
        crib._spawn_bg(asyncio.get_running_loop(), asyncio.sleep(0.05),
                       "transient expiry", listed=False)
        await asyncio.sleep(0)
        return [j["job"] for j in DIAG.snapshot()["background"]]

    assert "transient expiry" not in run(main())
