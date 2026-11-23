"""Phased re-index (plan "Overlays 6"): changed files' structure, then edges
re-resolved (dangling calls reported), then descriptions, under one budget.
The extract phase itself is the ordinary sweep (LSP-driven, tested elsewhere);
here it is stubbed so the phase logic and the re-resolve pass are exact."""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.codeindex import SymbolIndex
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.delenv("CRIB_ROOT", raising=False)
    monkeypatch.delenv("CRIB_WRITABLE", raising=False)
    cfg = Config()
    cfg.generate.mode = "on"
    return Crib(Paths.resolve().ensure(), cfg, InMemoryStore())


def _sym(crib, proj, file, name, calls=(), called_by=(), description="d"):
    si = SymbolIndex(crib.paths.project_dir(proj))
    si.write({
        "fqn": f"{file[:-3]}.{name}", "name": name, "kind": "function",
        "lang": "python", "module": file[:-3], "parent": "",
        "content_hash": f"h-{file}-{name}", "file": file, "line": 1,
        "signature": f"def {name}():", "description": description,
        "container": [], "calls": list(calls), "called_by": list(called_by),
        "name_terms": [name], "symbol_ref": f"{file}#{name}"})


def _rec(crib, proj, name):
    crib.code.refresh(proj)
    return next(e for e in crib.code.records(proj) if e["name"] == name)


def test_reresolve_rebuilds_callers_and_reports_dangling(crib):
    # caller.py: `use` calls `kept` and `gone` (gone was removed from lib.py)
    _sym(crib, "p", "caller.py", "use",
         calls=["kept [lib.py]", "gone [lib.py]", "util [dep:u.py]"])
    # lib.py: `kept`'s recorded callers are stale: `old` no longer calls it,
    # and a caller from another project must survive
    _sym(crib, "p", "lib.py", "kept",
         called_by=["old [caller.py]", "far [dep:x.py]"])
    _sym(crib, "p", "lib.py", "lonely", called_by=["use [caller.py]"])
    crib.code.refresh("p")
    out = crib.indexer.reresolve_edges("p")
    assert out["complete"] and out["records_updated"] == 2
    assert sorted(_rec(crib, "p", "kept")["called_by"]) == [
        "far [dep:x.py]", "use [caller.py]"]
    assert _rec(crib, "p", "lonely")["called_by"] == []  # nobody calls it any more
    assert [d["callee"] for d in out["dangling"]] == ["gone [lib.py]"]
    # idempotent: a second pass changes nothing
    assert crib.indexer.reresolve_edges("p")["records_updated"] == 0


def test_a_call_into_a_deleted_file_is_dangling(crib):
    _sym(crib, "p", "caller.py", "use", calls=["helper [old.py]", "ext [vendored.py]"])
    crib.code.refresh("p")
    out = crib.indexer.reresolve_edges("p", deleted={"old.py"})
    # old.py was the project's (deleted in this change); vendored.py never was
    assert [d["callee"] for d in out["dangling"]] == ["helper [old.py]"]


def _stub_sweep(crib, monkeypatch, files, complete=True):
    """The extract phase: report `files` as the checkout's files."""
    async def sweep(proj, root, globs, budget_s=None, crib_at=None, describe_mode="inline"):
        assert describe_mode == "none"  # structure only in phase 1
        return {"files_indexed": len(files), "files_seen": len(files), "symbols": 0,
                "described": 0, "describes_failed": 0, "complete": complete,
                "remaining": 0 if complete else 1}
    monkeypatch.setattr(crib.indexer, "_index_project_code", sweep)
    monkeypatch.setattr(crib.indexer.services, "enumerate_code_files",
                        lambda root, globs: [root / f for f in files])


def test_phases_run_in_order_and_drop_deleted_files(crib, monkeypatch, tmp_path):
    _sym(crib, "p", "caller.py", "use", calls=["helper [old.py]"])
    _sym(crib, "p", "old.py", "helper", description="")
    crib.code.refresh("p")
    described = []

    async def describe(proj, root, rel, pending):
        described.append(rel)
        return {"described": len(pending), "file": rel}

    monkeypatch.setattr(crib.indexer, "_describe_and_patch", describe)
    _stub_sweep(crib, monkeypatch, ["caller.py"])  # old.py is gone in the checkout
    out = run(crib.indexer.index_phased("p", tmp_path, ["**/*.py"]))
    ph = out["phases"]
    assert ph["extract"]["deleted_files"] == ["old.py"]
    assert not any(e["file"] == "old.py" for e in crib.code.records("p"))
    assert [d["callee"] for d in out["dangling"]] == ["helper [old.py]"]
    assert ph["describe"]["complete"] and described == []  # old.py's symbol is gone
    assert out["complete"]


def test_describe_phase_describes_pending_files(crib, monkeypatch, tmp_path):
    _sym(crib, "p", "a.py", "f", description="")
    _sym(crib, "p", "b.py", "g", description="")
    _sym(crib, "p", "c.py", "h")  # already described
    crib.code.refresh("p")
    described = []

    async def describe(proj, root, rel, pending):
        described.append(rel)
        return {"described": len(pending), "file": rel}

    monkeypatch.setattr(crib.indexer, "_describe_and_patch", describe)
    _stub_sweep(crib, monkeypatch, ["a.py", "b.py", "c.py"])
    out = run(crib.indexer.index_phased("p", tmp_path, ["**/*.py"]))
    assert described == ["a.py", "b.py"]
    assert out["phases"]["describe"] == {"complete": True, "described": 2,
                                         "files_described": 2, "remaining_files": 0}


def test_a_spent_budget_skips_the_later_phases_and_says_so(crib, monkeypatch, tmp_path):
    _sym(crib, "p", "a.py", "f", description="")
    crib.code.refresh("p")
    _stub_sweep(crib, monkeypatch, ["a.py"], complete=False)

    async def describe(proj, root, rel, pending):
        raise AssertionError("no time left: not called")

    monkeypatch.setattr(crib.indexer, "_describe_and_patch", describe)
    out = run(crib.indexer.index_phased("p", tmp_path, ["**/*.py"], budget_s=1e-9))
    ph = out["phases"]
    assert not ph["extract"]["complete"] and ph["extract"]["deleted_files"] == []
    assert ph["reresolve"] == {"complete": False, "reason": "budget spent"}
    assert ph["describe"]["complete"] is False and ph["describe"]["remaining_files"] == 1
    assert out["complete"] is False


def test_generation_off_skips_describe(crib, monkeypatch, tmp_path):
    crib.config.generate.mode = "off"
    _sym(crib, "p", "a.py", "f", description="")
    crib.code.refresh("p")
    _stub_sweep(crib, monkeypatch, ["a.py"])
    out = run(crib.indexer.index_phased("p", tmp_path, ["**/*.py"]))
    assert out["phases"]["describe"]["skipped"] == "[generate] mode = off"


def test_the_cli_passes_budget_and_phased():
    from crib import cli

    args = cli.build_parser().parse_args(
        ["project", "index", "-p", "x", "--budget", "30", "--phased"])
    call = cli.VERBS["project index"].build(args)
    assert call["budget_s"] == 30.0 and call["phased"] is True
    args = cli.build_parser().parse_args(["project", "index", "-p", "x"])
    assert cli.VERBS["project index"].build(args)["phased"] is None
