"""Per-project read-only overrides: a crib instance may write only the projects
in its `writable` allowlist (crib/access.py). Design "Transient projects,
out-of-store homes, and code overlays", part 5; plan "Overlays 1"."""

from __future__ import annotations

import asyncio

import pytest

from crib.access import WRITABLE_ENV, ReadOnlyProject, WriteAccess, writable_from
from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.delenv(WRITABLE_ENV, raising=False)
    return tmp_path


def _crib(store, writable=None):
    cfg = Config()
    cfg.writable = writable
    return Crib(Paths.resolve().ensure(), cfg, store)


@pytest.fixture()
def seeded(dirs):
    """`orig` and `copy` both exist; then an instance that may write only `copy`."""
    store = InMemoryStore()
    open_crib = _crib(store)
    run(open_crib.store_note("original fact", title="Orig note", project="orig"))
    run(open_crib.design_add("Ground", "A decision.", project="orig"))
    run(open_crib.store_note("copy fact", title="Copy note", project="copy"))
    return _crib(store, writable=["copy"])


def test_unset_means_every_project_is_writable():
    access = WriteAccess(None)
    assert access.allows("anything")
    access.require("anything")  # no raise


def test_an_allowlist_refuses_everything_else_and_says_why():
    access = WriteAccess(["copy"])
    assert access.allows("copy") and not access.allows("orig")
    with pytest.raises(ReadOnlyProject) as e:
        access.require("orig", "note_add")
    msg = str(e.value)
    assert "note_add refused" in msg and "'orig'" in msg and "copy" in msg
    assert "Reads still work" in msg


def test_the_environment_wins_over_the_config(monkeypatch):
    monkeypatch.delenv(WRITABLE_ENV, raising=False)
    assert writable_from(["a"]) == ["a"]
    monkeypatch.setenv(WRITABLE_ENV, "b, c")
    assert writable_from(["a"]) == ["b", "c"]
    monkeypatch.setenv(WRITABLE_ENV, "")
    assert writable_from(["a"]) == []  # set but empty: nothing is writable


def test_config_file_and_env(dirs, monkeypatch):
    f = dirs / "config.toml"
    f.write_text('writable = ["copy"]\n')
    assert Config.load(f).writable == ["copy"]
    monkeypatch.setenv(WRITABLE_ENV, "other")
    assert Config.load(f).writable == ["other"]


def test_writes_to_the_writable_project_work(seeded):
    out = run(seeded.store_note("more", title="Another", project="copy"))
    assert out["relpath"]
    run(seeded.plan_add("Review it", project="copy"))


def test_every_write_funnel_refuses_a_read_only_project(seeded, dirs):
    with pytest.raises(ReadOnlyProject):
        run(seeded.store_note("nope", title="Nope", project="orig"))
    with pytest.raises(ReadOnlyProject):
        run(seeded.design_add("Other", "No.", project="orig"))
    with pytest.raises(ReadOnlyProject):
        run(seeded.plan_add("Nope", project="orig"))
    with pytest.raises(ReadOnlyProject):
        run(seeded.project_rename(project="orig", to="renamed"))
    with pytest.raises(ReadOnlyProject):
        run(seeded.project_rename(project="copy", to="elsewhere"))  # target too
    with pytest.raises(ReadOnlyProject):
        run(seeded.project_delete(project="orig"))
    with pytest.raises(ReadOnlyProject):
        run(seeded.project_create(project="brand-new"))
    src = dirs / "src.py"
    src.write_text("def f():\n    return 1\n")
    with pytest.raises(ReadOnlyProject):
        run(seeded.code_index(str(src), project="orig"))
    assert "orig" in seeded.projects()  # nothing was changed


def test_reads_of_a_read_only_project_still_work(seeded):
    hits = seeded.lookup("original fact", project="orig")
    assert any(h.relpath.startswith("orig-note") for h in hits)
    assert seeded.list_items("orig", facet="note")["total"] == 1


def test_the_cli_flag_sets_the_allowlist_for_this_process(dirs, monkeypatch):
    import os

    from crib import cli, entry

    # set-then-delete: monkeypatch must record the vars to remove what
    # crib.entry sets (delenv of an unset var records nothing)
    for v in (WRITABLE_ENV, "CRIB_ROOT", "CRIB_STORE"):
        monkeypatch.setenv(v, "x")
        monkeypatch.delenv(v)
    seen = []
    monkeypatch.setattr(cli, "_run_inprocess",
                        lambda args, cfg: seen.append(os.environ.get(WRITABLE_ENV)))
    entry.main(["--writable", "a", "--writable", "b", "--no-daemon", "plan", "list",
                "-p", "x"])
    assert seen == ["a,b"]
