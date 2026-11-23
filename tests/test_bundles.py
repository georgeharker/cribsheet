"""Bundles: a self-contained, disposable crib root handed to a sandbox (plan
"Overlays 5"; design "Transient projects, out-of-store homes, and code
overlays", parts 6, 7, 9)."""

from __future__ import annotations

import asyncio
import datetime
import json
from pathlib import Path

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

    with pytest.raises(ReadOnlyProject):
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
    assert expand_location("$CONTEXT/llmkit", locs) == Path(
        "/mnt/bundle/context/llmkit"
    )
    cyc = {"A": "$B/x", "B": "$A/y"}
    expand_location("$A/z", cyc)  # a cycle terminates (whatever it yields)


# ── 5b: the bundle's vectors ─────────────────────────────────────────────────


class _CountingEmbedder:
    """A cacheable (non-hash) embedder that counts what it computes."""

    model_id = "test-counting"
    dim = 8

    def __init__(self, calls):
        self.calls = calls

    def _vec(self, t):
        import hashlib

        d = hashlib.sha256(t.encode()).digest()
        v = [b / 255.0 for b in d[: self.dim]]
        n = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / n for x in v]

    def embed(self, texts):
        self.calls.extend(texts)
        return [self._vec(t) for t in texts]

    def embed_query(self, texts):
        return [self._vec(t) for t in texts]


@pytest.fixture()
def counting(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "crib.app.build_embedder", lambda cfg, stored_dim=None: _CountingEmbedder(calls)
    )
    return calls


def test_create_builds_the_bundle_store_and_its_vector_file(
    trusted, tmp_path, counting
):
    out = bundles.create(trusted, Config(), tmp_path / "b", "toucan:pr-1", ["llmkit"])
    b = tmp_path / "b"
    assert out["vectors"] and out["vectors"] > 0
    assert (b / "data" / "embed-cache.parquet").is_file()
    assert (b / "index" / "duckdb" / "crib.duckdb").is_file()  # a ready store too
    assert counting  # first time: computed


def test_vectors_read_through_the_main_cache(trusted, tmp_path, counting):
    """A second bundle of the same projects computes nothing: every vector is
    found in the main store's cache (filled here from the first bundle)."""
    from crib.store_duckdb import DuckDBStore

    bundles.create(trusted, Config(), tmp_path / "b1", "toucan:pr-1", ["llmkit"])
    main = DuckDBStore.embedded(trusted.duckdb_file)
    main.ec_import(tmp_path / "b1" / "data" / "embed-cache.parquet")
    main.close()
    counting.clear()
    out = bundles.create(trusted, Config(), tmp_path / "b2", "toucan:pr-2", ["llmkit"])
    assert out["vectors"] and counting == []


def test_a_store_rebuilt_elsewhere_reembeds_nothing(
    trusted, tmp_path, counting, monkeypatch
):
    """The sandbox keeps its store on scratch (--store): the fresh store loads
    the bundle's vector file, and the rebuild computes nothing."""
    from crib.app import _build_store

    bundles.create(trusted, Config(), tmp_path / "b", "toucan:pr-1", ["llmkit"])
    counting.clear()
    monkeypatch.setenv(ROOT_ENV, str(tmp_path / "b"))
    monkeypatch.setenv("CRIB_STORE", str(tmp_path / "scratch"))
    paths = Paths.resolve().ensure()
    cfg = Config.load(paths.config_file)
    store, close = _build_store(paths, cfg)
    try:
        box = Crib(paths, cfg, store)
        from crib.store_duckdb import DuckDBStore

        assert isinstance(store, DuckDBStore)
        assert store.ec_count() > 0  # loaded from the bundle's file
        run(box.reconcile_all())
        for p in ("toucan@pr-1", "llmkit"):
            box.code.refresh(p)
            box.code.fill_vectors(p)
        assert counting == []
        assert any(
            "base-note" in h.relpath
            for h in box.lookup("sprockets", project="toucan@pr-1")
        )
    finally:
        if close is not None:
            close()


def test_no_vectors_skips_the_store(trusted, tmp_path, counting):
    out = bundles.create(
        trusted, Config(), tmp_path / "b", "toucan:pr-1", [], vectors=False
    )
    assert out["vectors"] is None and counting == []
    assert not (tmp_path / "b" / "data" / "embed-cache.parquet").exists()


def test_the_trusted_source_root_is_never_carried(trusted, tmp_path):
    """`.source_root` is the trusted machine's repo path: a sandbox would
    revalidate against it (dropping records, or reading trusted code)."""
    bundles.create(
        trusted, Config(), tmp_path / "b", "toucan:pr-1", ["llmkit"], vectors=False
    )
    assert not list((tmp_path / "b").rglob(".source_root"))


def test_a_read_only_projects_records_survive_revalidation(
    trusted, tmp_path, monkeypatch
):
    """Inside the sandbox a context project is read-only: revalidation and
    file drops never touch its records, even if a source root says files are
    gone."""
    from crib.codeindex import SymbolIndex

    bundles.create(
        trusted, Config(), tmp_path / "b", "toucan:pr-1", ["llmkit"], vectors=False
    )
    monkeypatch.setenv(ROOT_ENV, str(tmp_path / "b"))
    paths = Paths.resolve()
    si = SymbolIndex(paths.project_dir("llmkit"))
    si.write(
        {
            "fqn": "m.f",
            "name": "f",
            "kind": "function",
            "lang": "python",
            "module": "m",
            "parent": "",
            "content_hash": "h",
            "file": "m.py",
            "line": 1,
            "signature": "def f():",
            "description": "d",
            "container": [],
            "calls": [],
            "called_by": [],
            "name_terms": ["f"],
            "symbol_ref": "m.py#f",
        }
    )
    si.set_source_root(tmp_path / "gone")  # e.g. a path that isn't in the pod
    box = Crib(paths, Config.load(paths.config_file), InMemoryStore())
    box._revalidate("llmkit")
    box._drop_file("llmkit", "m.py")
    assert [e["name"] for e in box.code.records("llmkit")] == ["f"]


# ── review fixes: the trusted side vs a hostile sandbox ──────────────────────


def test_a_hostile_manifest_never_hangs_or_floods_the_trusted_side(trusted, tmp_path):
    """The manifest is writable in the sandbox: a FIFO (would block the read),
    a symlink (read through to anywhere) or a huge file is unreadable, and the
    sweep leaves that bundle alone."""
    import os

    for i, plant in enumerate(("fifo", "symlink", "huge")):
        b = tmp_path / f"b{i}"
        bundles.create(trusted, Config(), b, f"toucan:pr-{i}", [], vectors=False)
        m = b / "bundle.toml"
        m.unlink()
        if plant == "fifo":
            os.mkfifo(m)
        elif plant == "symlink":
            (tmp_path / "elsewhere.toml").write_text(
                'format = 1\nexpires = "2000-01-01"\n'
            )
            m.symlink_to(tmp_path / "elsewhere.toml")
        else:
            m.write_text('format = 1\nexpires = "2000-01-01"\n' + "#" * 100_000)
    rows = {Path(r["bundle"]).name: r["state"] for r in bundles.listing(trusted)}
    assert rows == {"b0": "unreadable", "b1": "unreadable", "b2": "unreadable"}
    assert bundles.sweep(trusted) == []


def test_trusted_expire_takes_the_overlay_from_the_registry(trusted, tmp_path):
    """A sandbox that rewrites the manifest's overlay, or swaps the overlay's
    dir for a symlink into the trusted store, can't make the trusted `expire`
    write a trusted project's stub."""
    b = tmp_path / "b"
    bundles.create(trusted, Config(), b, "toucan:pr-1", [], vectors=False)
    victim = trusted.project_dir("toucan") / ".cribproject"
    before = victim.read_text() if victim.exists() else None
    ov = b / "data" / "projects" / "toucan@pr-1"
    import shutil

    shutil.rmtree(ov)
    ov.symlink_to(trusted.project_dir("toucan"))
    with pytest.raises(CribUserError, match="symlink"):
        bundles.expire(b, "30d", trusted)
    assert (victim.read_text() if victim.exists() else None) == before
    with pytest.raises(CribUserError, match="not a registered bundle"):
        bundles.expire(tmp_path / "unregistered", "1d", trusted)


def test_expire_replaces_a_planted_manifest_symlink_instead_of_following_it(
    trusted, tmp_path
):
    b = tmp_path / "b"
    bundles.create(trusted, Config(), b, "toucan:pr-1", [], vectors=False)
    other = tmp_path / "other.toml"
    other.write_text((b / "bundle.toml").read_text())
    (b / "bundle.toml").unlink()
    (b / "bundle.toml").symlink_to(other)
    before = other.read_text()
    with pytest.raises(CribUserError):  # the planted manifest is unreadable...
        bundles.expire(b, "30d", trusted)
    assert other.read_text() == before  # ...and the target was never written


def test_confined_expire_extends_only_its_own_root(trusted, tmp_path, monkeypatch):
    bundles.create(trusted, Config(), tmp_path / "b", "toucan:pr-1", [], vectors=False)
    bundles.create(trusted, Config(), tmp_path / "c", "toucan:pr-2", [], vectors=False)
    monkeypatch.setenv(ROOT_ENV, str(tmp_path / "b"))
    paths = Paths.resolve()
    bundles.expire(tmp_path / "b", "30d", paths)
    with pytest.raises(CribUserError, match="only this bundle"):
        bundles.expire(tmp_path / "c", "30d", paths)


def test_a_failed_create_leaves_nothing_behind(trusted, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(bundles, "_build_vectors", boom)
    with pytest.raises(RuntimeError):
        bundles.create(
            trusted,
            Config(),
            tmp_path / "b",
            "toucan:pr-1",
            ["llmkit"],
            context_to=tmp_path / "ctx",
        )
    assert not (tmp_path / "b").exists() and not (tmp_path / "ctx").exists()
    assert bundles.registry(trusted) == {}
    monkeypatch.undo()
    bundles.create(trusted, Config(), tmp_path / "b", "toucan:pr-1", [], vectors=False)


def test_one_stuck_bundle_doesnt_block_the_sweep(trusted, tmp_path, monkeypatch):
    for name in ("b1", "b2"):
        bundles.create(
            trusted,
            Config(),
            tmp_path / name,
            f"toucan:{name}",
            [],
            expires="1s",
            vectors=False,
        )
    real = bundles.delete

    def flaky(paths, b):
        if Path(b).name == "b1":
            raise OSError("busy")
        return real(paths, b)

    monkeypatch.setattr(bundles, "delete", flaky)
    later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
    assert [Path(g).name for g in bundles.sweep(trusted, now=later)] == ["b2"]


def test_a_context_copy_mounted_elsewhere(trusted, tmp_path, monkeypatch):
    """--context-to writes the context copy outside the bundle; --context-mount
    says where the sandbox sees it; a confined instance reads it there."""
    mount = tmp_path / "ro-mount"
    bundles.create(trusted, Config(), tmp_path / "b", "toucan:pr-1", ["llmkit"],
                   context_to=mount, context_mount=str(mount), vectors=False)
    assert (mount / "llmkit" / "notes" / "ctx-note.md").exists()
    assert not (tmp_path / "b" / "context").exists()
    assert f'CONTEXT = "{mount}"' in (tmp_path / "b" / "config" / "config.toml").read_text()
    monkeypatch.setenv(ROOT_ENV, str(tmp_path / "b"))
    paths = Paths.resolve()
    box = Crib(paths, Config.load(paths.config_file), InMemoryStore())
    assert box.project_paths("llmkit").data_root == mount / "llmkit"
    run(box.reconcile_all())
    assert any("ctx-note" in h.relpath for h in box.lookup("gears", project="llmkit"))
