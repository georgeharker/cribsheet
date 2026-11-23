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


def test_reresolve_updates_only_edges_touching_changed_files(crib):
    """lib.py was re-extracted (`changed`): its callers came from the language
    server and stay. caller.py is unchanged: its stale caller entries from
    changed files go, and the changed file's fresh calls are added. Edges
    between unchanged files, and callers from other projects, are untouched."""
    # changed lib.py: `helper` now calls `util` (an unchanged-file target),
    # and the server recorded `use` and `main` as its callers
    _sym(crib, "p", "lib.py", "helper", calls=["util [caller.py]"],
         called_by=["use [caller.py]", "main [app.py]"])
    # unchanged caller.py: `util`'s callers were recorded against the OLD lib.py
    _sym(crib, "p", "caller.py", "util",
         called_by=["old_helper [lib.py]", "keep [app.py]", "far [dep:x.py]"])
    _sym(crib, "p", "caller.py", "use", calls=["helper [lib.py]", "gone [lib.py]"])
    _sym(crib, "p", "app.py", "keep", calls=["util [caller.py]"])
    crib.code.refresh("p")
    out = crib.indexer.reresolve_edges("p", changed={"lib.py"})
    assert sorted(_rec(crib, "p", "util")["called_by"]) == [
        "far [dep:x.py]", "helper [lib.py]", "keep [app.py]"]
    # the changed file's own callers are the server's, untouched
    assert sorted(_rec(crib, "p", "helper")["called_by"]) == [
        "main [app.py]", "use [caller.py]"]
    assert [d["callee"] for d in out["dangling"]] == ["gone [lib.py]"]
    assert out["records_updated"] == 1
    # idempotent
    assert crib.indexer.reresolve_edges("p", changed={"lib.py"})["records_updated"] == 0


def test_reresolve_keeps_callers_only_the_server_knows(crib):
    """An unchanged record's callers from unchanged files (e.g. learned from the
    call hierarchy, with no matching `calls` entry) survive a re-resolve."""
    _sym(crib, "p", "a.py", "target", called_by=["dynamic [b.py]"])
    _sym(crib, "p", "b.py", "dynamic")  # no calls recorded (a dynamic dispatch)
    _sym(crib, "p", "c.py", "changed_fn")
    crib.code.refresh("p")
    crib.indexer.reresolve_edges("p", changed={"c.py"})
    assert _rec(crib, "p", "target")["called_by"] == ["dynamic [b.py]"]


def test_reresolve_with_same_named_symbols_touches_only_changed_files(crib):
    """Two `run`s in one unchanged file: a re-resolve for an unrelated change
    doesn't fan callers out between them."""
    _sym(crib, "p", "m.py", "run", called_by=["a [x.py]"])
    crib.code.refresh("p")
    crib.indexer.reresolve_edges("p", changed={"other.py"})
    assert _rec(crib, "p", "run")["called_by"] == ["a [x.py]"]


def test_a_call_into_a_deleted_file_is_dangling(crib):
    _sym(crib, "p", "caller.py", "use", calls=["helper [old.py]", "ext [vendored.py]"])
    crib.code.refresh("p")
    out = crib.indexer.reresolve_edges("p", changed=set(), deleted={"old.py"})
    # old.py was deleted in this change; vendored.py was never the project's
    assert [d["callee"] for d in out["dangling"]] == ["helper [old.py]"]


def _stub_sweep(crib, monkeypatch, files, complete=True):
    """The extract phase: report `files` as the checkout's files."""
    async def sweep(proj, root, globs, budget_s=None, crib_at=None, describe_mode="inline",
                    **_kw):  # `files=` too; the checkout is `files` above
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


def test_a_file_emptied_of_symbols_counts_as_changed(crib, monkeypatch, tmp_path):
    """lib.py is still on disk but its last symbol was removed: its old edges
    into other records go (S-3)."""
    _sym(crib, "p", "lib.py", "helper", calls=["util [caller.py]"])
    _sym(crib, "p", "caller.py", "util", called_by=["helper [lib.py]"])
    crib.code.refresh("p")

    async def sweep(proj, root, globs, budget_s=None, crib_at=None, describe_mode="inline",
                    **_kw):
        crib.code.drop_file("p", "lib.py")  # the re-extract found no symbols
        crib.code.refresh("p")
        return {"files_indexed": 2, "files_seen": 2, "symbols": 0, "described": 0,
                "describes_failed": 0, "complete": True, "remaining": 0}

    monkeypatch.setattr(crib.indexer, "_index_project_code", sweep)
    monkeypatch.setattr(crib.indexer.services, "enumerate_code_files",
                        lambda root, globs: [root / "lib.py", root / "caller.py"])
    out = run(crib.indexer.index_phased("p", tmp_path, ["**/*.py"]))
    assert "lib.py" in out["phases"]["extract"]["changed_files"]
    assert _rec(crib, "p", "util")["called_by"] == []
