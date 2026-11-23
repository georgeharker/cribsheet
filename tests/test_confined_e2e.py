"""Confined mode end to end: a session against a hostile checkout writes
NOTHING outside its root, and the checkout's own .crib files decide nothing
(review of 0.28.0-dev1: B1, B2, S6)."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from crib.app import Crib, _build_store
from crib.config import Config
from crib.errors import CribUserError
from crib.paths import ROOT_ENV, Paths


def run(coro):
    return asyncio.run(coro)


def _snapshot(base: Path, skip: Path) -> dict[str, str]:
    out = {}
    for p in sorted(base.rglob("*")):
        if p.is_relative_to(skip):
            continue
        rel = str(p.relative_to(base))
        if p.is_symlink():
            out[rel] = "link:" + str(p.readlink())
        elif p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        else:
            out[rel] = "dir"
    return out


@pytest.fixture()
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for d in ("", ".config", ".local/share", ".cache"):
        (home / d).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    for v in (
        "CRIB_CONFIG_DIR",
        "CRIB_DATA_DIR",
        "CRIB_INDEX_DIR",
        "CRIB_STORE",
        "CRIB_WRITABLE",
        "CRIB_FACETS_FILE",
    ):
        monkeypatch.delenv(v, raising=False)
    # an inherited store dir and facets file the confined process must ignore
    monkeypatch.setenv("CRIB_INDEX_DIR", str(home / "trusted-index"))
    monkeypatch.setenv("CRIB_FACETS_FILE", str(home / "trusted-facets.toml"))
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv(ROOT_ENV, str(root))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("SECRET = 1\n")
    co = tmp_path / "checkout"
    (co / "src" / "evil").mkdir(parents=True)
    (co / ".git").mkdir()
    (co / "src" / "a.py").write_text("def a():\n    return 1\n")
    (co / "src" / "evil" / "b.py").write_text("def backdoor():\n    return 2\n")
    (co / "src" / "evil" / ".crib").write_text("project: hidden\n")
    (co / ".crib").write_text(
        "project: victim\nstore: crib-notes\nrefs:\n  - ../outside\n"
        'paths:\n  - "nothing/**"\n'
    )
    (co / "src" / "leak.py").symlink_to(outside / "secret.py")
    before = _snapshot(tmp_path, root)
    return tmp_path, root.resolve(), co, before


def test_a_confined_session_writes_nothing_outside_its_root(world):
    base, root, co, before = world
    paths = Paths.resolve().ensure()
    assert paths.index_dir == root / "index"  # the inherited CRIB_INDEX_DIR is ignored
    cfg = Config.load(paths.config_file)
    store, close = _build_store(paths, cfg)
    try:
        crib = Crib(paths, cfg, store)
        run(crib.project_create(project="pr"))
        run(crib.store_note("a review finding", title="Finding", project="pr"))
        # residency verbs would write into the checkout: refused
        with pytest.raises(CribUserError, match="confined mode"):
            run(crib.project_create(project="pr2", cwd=co, store="crib-notes"))
        with pytest.raises(CribUserError, match="confined mode"):
            run(crib.project_adopt(project="pr", cwd=co))
        with pytest.raises(CribUserError, match="confined mode"):
            run(crib.project_release(project="pr", cwd=co))
        # the checkout's .crib decides nothing, and nothing is written there
        link, created = crib._ensure_crib(co, "pr", want_code=True, want_docs=False)
        assert not created and link.store is None and not link.refs
        files = {
            str(p.relative_to(co)) for p in crib._enumerate_code_files(co, ["**/*.py"])
        }
        assert "src/evil/b.py" in files  # a planted nested .crib hides nothing
        assert "src/leak.py" not in files  # a symlink out of the checkout isn't code
        run(crib.reconcile_all())
        with pytest.raises(CribUserError):
            crib.snapshot("x")
        crib.close()
    finally:
        if close is not None:
            close()
    assert _snapshot(base, root) == before
