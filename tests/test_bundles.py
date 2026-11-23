"""Bundles: a self-contained, disposable crib root handed to a sandbox (plan
"Overlays 5"; design "Transient projects, out-of-store homes, and code
overlays", parts 6, 7, 9)."""

from __future__ import annotations

import asyncio
import datetime
import json

import pytest

from crib import bundle as bundles
from crib.app import Crib
from crib.config import Config
from crib.errors import CribUserError
from crib.paths import ROOT_ENV, Paths
from crib.store import InMemoryStore


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def trusted(tmp_path, monkeypatch):
    """A trusted-side crib with a base project (`toucan`, indexed from a repo
    with a .crib) and a context project (`llmkit`)."""
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv(ROOT_ENV, "x")
    monkeypatch.delenv(ROOT_ENV)
    monkeypatch.delenv("CRIB_WRITABLE", raising=False)
    paths = Paths.resolve().ensure()
    crib = Crib(paths, Config(), InMemoryStore())
    run(
        crib.store_note("the base knows sprockets", title="Base note", project="toucan")
    )
    run(crib.design_add("Ground", "A decision.", project="toucan"))
    run(crib.store_note("the context knows gears", title="Ctx note", project="llmkit"))
    repo = tmp_path / "toucan-repo"
    repo.mkdir()
    (repo / ".crib").write_text(
        'project: toucan\npaths:\n  - "src/**/*.py"\nstore: evil\n'
    )
    from crib.codeindex import SymbolIndex

    si = SymbolIndex(paths.project_dir("toucan"))
    si.set_source_root(repo)
    (si.root / "rec.toml").write_text('symbol_ref = "a.py#f"\n')
    (paths.config_dir / "lsp.json").write_text("{}\n")
    return paths


def _create(paths, tmp_path, **kw):
    return bundles.create(
        paths,
        Config(),
        tmp_path / "b",
        "toucan:pr-1",
        kw.pop("context", ["llmkit"]),
        **kw,
    )


def test_create_writes_a_self_contained_bundle(trusted, tmp_path):
    out = _create(trusted, tmp_path)
    b = tmp_path / "b"
    assert out["overlay"] == "toucan@pr-1" and out["context"] == ["llmkit"]
    ov = b / "data" / "projects" / "toucan@pr-1"
    assert (ov / "notes" / "base-note.md").exists()
    assert any((ov / "design").glob("*.md"))
    assert (ov / "symbol_index" / "rec.toml").exists()
    stub = (ov / ".cribproject").read_text()
    assert "transient: true" in stub and "expires:" in stub
    trusted_crib = (ov / ".crib").read_text()
    assert "project: toucan@pr-1" in trusted_crib and "src/**/*.py" in trusted_crib
    assert "store" not in trusted_crib  # residency never carried
    assert (b / "context" / "llmkit" / "notes" / "ctx-note.md").exists()
    assert (
        "$CONTEXT/llmkit"
        in (b / "data" / "projects" / "llmkit" / ".cribproject").read_text()
    )
    cfg = (b / "config" / "config.toml").read_text()
    assert 'writable = ["toucan@pr-1"]' in cfg and 'mode = "embedded"' in cfg
    assert 'CONTEXT = "$ROOT/context"' in cfg
    assert (b / "config" / "lsp.json").exists()
    m = bundles.read_manifest(b)
    assert m["overlay"] == "toucan@pr-1" and m["context"] == ["llmkit"] and m["expires"]
    assert str(b.resolve()) in bundles.registry(trusted)
    assert not any(b.rglob(".versions"))


def test_create_refuses_bad_input(trusted, tmp_path):
    with pytest.raises(CribUserError, match="<base>:<name>"):
        bundles.create(trusted, Config(), tmp_path / "x", "toucan", [])
    with pytest.raises(CribUserError, match="no project 'nope'"):
        bundles.create(trusted, Config(), tmp_path / "x", "nope:pr", [])
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "f").write_text("x")
    with pytest.raises(CribUserError, match="isn't empty"):
        bundles.create(trusted, Config(), tmp_path / "full", "toucan:pr", [])


def test_the_sandbox_opens_it_confined(trusted, tmp_path, monkeypatch):
    """Opened with --root: the overlay is writable, the context readable and
    read-only, and everything indexes from the bundle's own files."""
    _create(trusted, tmp_path)
    monkeypatch.setenv(ROOT_ENV, str(tmp_path / "b"))
    paths = Paths.resolve()
    cfg = Config.load(paths.config_file)
    assert cfg.writable == ["toucan@pr-1"]
    box = Crib(paths, cfg, InMemoryStore())
    run(box.reconcile_all())
    assert any(
        "base-note" in h.relpath for h in box.lookup("sprockets", project="toucan@pr-1")
    )
    assert any("ctx-note" in h.relpath for h in box.lookup("gears", project="llmkit"))
    run(box.store_note("review finding", title="Finding", project="toucan@pr-1"))
    from crib.access import ReadOnlyProject

    with pytest.raises((ReadOnlyProject, CribUserError)):
        run(box.store_note("nope", title="Nope", project="llmkit"))


def test_expire_extends_from_inside_and_the_sweep_reads_it(trusted, tmp_path):
    _create(trusted, tmp_path, expires="1d")
    b = tmp_path / "b"
    later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=2)
    assert bundles.sweep(trusted, now=later) == [str(b.resolve())]  # due: gone
    assert not b.exists() and bundles.registry(trusted) == {}

    _create(trusted, tmp_path, expires="1d")
    bundles.expire(b, "30d")  # e.g. a session inside the sandbox
    assert bundles.sweep(trusted, now=later) == []
    assert b.exists()
    stub = (b / "data" / "projects" / "toucan@pr-1" / ".cribproject").read_text()
    assert bundles.read_manifest(b)["expires"] in stub


def test_delete_trusts_the_registry_not_the_manifest(trusted, tmp_path):
    """The manifest is writable in the sandbox: a planted path in it is never
    deleted; only the registered bundle and context dirs are."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("precious")
    out = _create(trusted, tmp_path, context_to=tmp_path / "ctx")
    b = tmp_path / "b"
    (b / "bundle.toml").write_text(
        (b / "bundle.toml").read_text() + f"context_dir = {json.dumps(str(victim))}\n"
    )
    gone = bundles.delete(trusted, b)
    assert set(gone["removed"]) == {str(b.resolve()), out["context_dir"]}
    assert (victim / "keep").read_text() == "precious"
    with pytest.raises(CribUserError, match="not a registered bundle"):
        bundles.delete(trusted, b)


def test_listing_states(trusted, tmp_path):
    _create(trusted, tmp_path)
    rows = bundles.listing(trusted)
    assert len(rows) == 1 and rows[0]["state"] == "live"
    (tmp_path / "b" / "bundle.toml").write_text("not = [toml")
    assert bundles.listing(trusted)[0]["state"] == "unreadable"
    assert bundles.sweep(trusted) == []  # an unreadable manifest is left alone


def test_the_cli(trusted, tmp_path, capsys):
    from crib import cli

    assert (
        cli.main(
            [
                "bundle",
                "create",
                str(tmp_path / "b"),
                "--overlay",
                "toucan:pr-1",
                "--context",
                "llmkit",
                "--expires",
                "2h",
            ]
        )
        == 0
    )
    assert "crib --root" in capsys.readouterr().out
    assert cli.main(["--json", "bundle", "list"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["overlay"] == "toucan@pr-1"
    assert cli.main(["bundle", "delete", str(tmp_path / "b")]) == 0
    assert cli.main(["bundle", "delete", str(tmp_path / "b")]) == 2


def test_a_location_may_be_a_token_itself():
    from pathlib import Path

    from crib.config import expand_location

    locs = {"ROOT": "/mnt/bundle", "CONTEXT": "$ROOT/context"}
    assert expand_location("$CONTEXT/llmkit", locs) == Path("/mnt/bundle/context/llmkit")
    cyc = {"A": "$B/x", "B": "$A/y"}
    expand_location("$A/z", cyc)  # a cycle terminates (whatever it yields)
