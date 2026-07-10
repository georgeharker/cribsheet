"""Deferred code-symbol describe: the backoff queue + defer-mode indexing.

Structural indexing (symbols + call graph) stays eager on every save; the LLM
description pass is decoupled onto a per-file exponential-backoff queue so an edit
burst coalesces to ONE focused describe. A changed symbol's description is blanked
on the structural write (a durable "needs describing" signal), and a crash/stop
mid-window is healed on next start by re-driving anything left blank.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from crib.app import Crib
from crib.codeindex import SymbolIndex
from crib.config import Config
from crib.describe_queue import DescribeQueue
from crib.paths import Paths
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def run(coro):
    return asyncio.run(coro)


def _sym(fq, h, body):
    return {"name": fq.split(".")[-1], "kind": "function", "content_hash": h,
            "_body": body}


# ── the scheduler ─────────────────────────────────────────────────────────────
class _FakeTimer:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _FakeLoop:
    """Records call_later delays; runs call_soon_threadsafe inline. Enough to assert
    the backoff schedule deterministically without real time."""

    def __init__(self):
        self.delays: list[float] = []

    def call_soon_threadsafe(self, cb, *a):
        cb(*a)

    def call_later(self, delay, cb, *a):
        self.delays.append(delay)
        return _FakeTimer()

    def create_task(self, coro):
        coro.close()


def test_backoff_delays_double_then_cap():
    loop = _FakeLoop()

    async def _noop(*a):
        pass

    q = DescribeQueue(loop, _noop, base=1.0, cap=8.0)
    sym = {"pkg.f": _sym("pkg.f", "h", "b")}
    for _ in range(6):
        q.enqueue("p", Path("/r"), "f.py", sym)          # same file re-edited 6×
    assert loop.delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]  # doubles, then pinned at cap


def test_burst_coalesces_to_one_focused_describe():
    calls: list[dict] = []

    async def body():
        async def describe(proj, root, rel, pending):
            calls.append(dict(pending))

        q = DescribeQueue(asyncio.get_running_loop(), describe, base=0.01, cap=0.05)
        q.enqueue("p", Path("/r"), "f.py", {"pkg.a": _sym("pkg.a", "ha", "a")})
        q.enqueue("p", Path("/r"), "f.py", {"pkg.b": _sym("pkg.b", "hb", "b")})
        await asyncio.sleep(0.12)

    run(body())
    assert len(calls) == 1                                # one describe for the burst
    assert set(calls[0]) == {"pkg.a", "pkg.b"}            # both changed symbols merged


def test_failed_describe_reenqueues_as_retry():
    n = {"c": 0}

    async def body():
        async def describe(proj, root, rel, pending):
            n["c"] += 1
            if n["c"] == 1:
                raise RuntimeError("LLM down")           # first attempt fails

        q = DescribeQueue(asyncio.get_running_loop(), describe, base=0.01, cap=0.04)
        q.enqueue("p", Path("/r"), "f.py", {"pkg.a": _sym("pkg.a", "ha", "a")})
        await asyncio.sleep(0.2)

    run(body())
    assert n["c"] >= 2                                    # backoff-as-retry ran again


# ── defer-mode indexing (blank-on-change + enqueue only stale) ────────────────
def _entry(fq, h, body):
    return {"fqname": fq, "name": fq.split(".")[-1], "kind": "function",
            "lang": "python", "module": "pkg.mod", "parent": "", "content_hash": h,
            "file": "pkg/mod.py", "line": 1, "signature": "def _():",
            "description": "", "container": [], "calls": [], "called_by": [],
            "references": [], "name_terms": [fq.split(".")[-1]], "_body": body}


def test_defer_keeps_unchanged_blanks_changed_and_enqueues_only_stale(
        crib, tmp_path, monkeypatch):
    from crib import codeindex as ci
    root = tmp_path / "src"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("def a(): pass\ndef b(): pass\n")

    # 1) inline index — both symbols described
    monkeypatch.setattr(ci, "extract_file", lambda r, rel, **k: [
        _entry("pkg.mod.a", "ha", "def a(): pass"),
        _entry("pkg.mod.b", "hb", "def b(): pass")])
    monkeypatch.setattr(ci, "describe_file",
                        lambda cfg, r, rel: {"a": "does A", "b": "does B"})
    crib._index_code_file_tracked(root, "pkg/mod.py", "p", True)  # inline
    si = SymbolIndex(crib.paths.project_dir("p"))
    assert {e["fqname"]: e["description"] for e in si.all()} == \
        {"pkg.mod.a": "does A", "pkg.mod.b": "does B"}

    # 2) defer — only b's body changes
    enq: list = []

    class _Q:
        def enqueue(self, proj, rt, rel, syms):
            enq.append((rel, dict(syms)))

    crib.indexer.set_describe_queue(_Q())
    monkeypatch.setattr(ci, "extract_file", lambda r, rel, **k: [
        _entry("pkg.mod.a", "ha", "def a(): pass"),
        _entry("pkg.mod.b", "hb2", "def b(): return 1")])
    crib._index_code_file_tracked(root, "pkg/mod.py", "p", True, None, "defer")

    descs = {e["fqname"]: e["description"] for e in si.all()}
    assert descs["pkg.mod.a"] == "does A"                 # unchanged → kept
    assert descs["pkg.mod.b"] == ""                       # changed → blanked (durable signal)
    assert len(enq) == 1
    assert list(enq[0][1]) == ["pkg.mod.b"]               # only the stale symbol queued


def test_describe_and_patch_patches_then_clobber_guards(crib, tmp_path, monkeypatch):
    from crib import codeindex as ci
    si = SymbolIndex(crib.paths.project_dir("p"))
    si.write(_entry("pkg.mod.b", "hb2", "def b(): return 1"))     # on disk: blank desc, hb2
    monkeypatch.setattr(ci, "describe_symbols",
                        lambda cfg, syms: {"b": "does B v2"})

    async def go(pending):
        await crib.indexer._describe_and_patch("p", tmp_path, "pkg/mod.py", pending)

    run(go({"pkg.mod.b": _sym("pkg.mod.b", "hb2", "def b(): return 1")}))
    assert si.read("pkg.mod.b")["description"] == "does B v2"      # patched

    # stale content_hash vs disk → skipped (a newer edit already re-queued it)
    run(go({"pkg.mod.b": _sym("pkg.mod.b", "OLD", "def b(): pass")}))
    assert si.read("pkg.mod.b")["description"] == "does B v2"      # untouched


def test_backlog_reindexes_only_blank_described_files(crib, tmp_path, monkeypatch):
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "mod.py").write_text("def b(): pass\n")
    si = SymbolIndex(crib.paths.project_dir("p"))
    done = _entry("pkg.mod.done", "hd", "x")
    done["description"] = "already described"                     # described → not backlog
    si.write(done)
    si.write(_entry("pkg.mod.blank", "hb", "y"))                 # blank desc → backlog
    si.set_source_root(src)

    calls: list = []
    monkeypatch.setattr(crib, "projects", lambda: ["p"])
    monkeypatch.setattr(crib, "_index_code_file_tracked",
                        lambda root, rel, proj, patch, existing=None,
                        describe_mode="inline": calls.append((rel, describe_mode)))
    run(crib._describe_backlog())
    assert calls == [("pkg/mod.py", "inline")]                   # inline re-drive, once
