"""The generation ledger wired into the startup backlogs: a provider that keeps
failing is paid for ONCE, then backed off across starts; a budget caps each
start; explicit requests ignore the ledger. (Ledger policy and storage are in
tests/test_genledger.py.)"""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.codeindex import SymbolIndex
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), _generation_on(), InMemoryStore())


def run(coro):
    return asyncio.run(coro)


def _blank(fq: str, file: str, file_hash: str) -> dict:
    return {
        "fqname": fq,
        "name": fq.split(".")[-1],
        "kind": "function",
        "lang": "python",
        "module": "pkg",
        "parent": "",
        "content_hash": "h-" + fq,
        "file": file,
        "file_hash": file_hash,
        "line": 1,
        "signature": "def _():",
        "description": "",
        "container": [],
        "calls": [],
        "called_by": [],
        "references": [],
        "name_terms": [fq.split(".")[-1]],
    }


def _seed(crib, tmp_path, files: dict[str, str]):
    """One undescribed symbol per file ({relpath: file_hash})."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    si = SymbolIndex(crib.paths.project_dir("p"))
    for i, (rel, h) in enumerate(sorted(files.items())):
        si.write(_blank(f"pkg.f{i}", rel, h))
    si.set_source_root(src)
    return si


def _failing_reindex(crib, calls):
    """Stands in for the real per-file reindex whose describe FAILS: records the
    call, then reports the outcome through the real ledger hook."""

    def reindex(root, rel, proj, patch, existing=None, describe_mode="inline"):
        calls.append(rel)
        si = SymbolIndex(crib.paths.project_dir(proj))
        entries = [e for e in si.all() if e.get("file") == rel]
        crib.indexer._record_describe(
            proj, rel, entries, entries, "provider returned nothing"
        )

    return reindex


def test_a_failing_describe_is_paid_once_then_backs_off(
    crib, tmp_path, monkeypatch, capsys
):
    _seed(crib, tmp_path, {"a.py": "ha", "b.py": "hb"})
    calls: list[str] = []
    monkeypatch.setattr(crib, "projects", lambda: ["p"])
    monkeypatch.setattr(crib, "_index_code_file_tracked", _failing_reindex(crib, calls))

    run(crib._describe_backlog())  # first start: both attempted, both fail
    assert sorted(calls) == ["a.py", "b.py"]
    run(crib._describe_backlog())  # next start: both backing off, no new calls
    assert sorted(calls) == ["a.py", "b.py"]
    err = capsys.readouterr().err
    assert "2 backing off after earlier failures" in err
    assert crib.ledger.counts()["describe"]["failing"] == 2


def test_an_edited_file_is_retried_at_once(crib, tmp_path, monkeypatch):
    si = _seed(crib, tmp_path, {"a.py": "ha"})
    calls: list[str] = []
    monkeypatch.setattr(crib, "projects", lambda: ["p"])
    monkeypatch.setattr(crib, "_index_code_file_tracked", _failing_reindex(crib, calls))
    run(crib._describe_backlog())
    e = si.all()[0]
    e["file_hash"] = "ha-edited"  # the file changed since it failed
    si.write(e)
    run(crib._describe_backlog())
    assert calls == ["a.py", "a.py"]


def test_the_budget_caps_each_start(crib, tmp_path, monkeypatch, capsys):
    _seed(crib, tmp_path, {f"m{i}.py": f"h{i}" for i in range(5)})
    calls: list[str] = []
    monkeypatch.setattr(crib, "projects", lambda: ["p"])
    monkeypatch.setattr(
        crib, "_index_code_file_tracked", lambda *a, **k: calls.append(a[1])
    )
    crib.config.generate.backlog_budget = 2
    run(crib._describe_backlog())
    assert len(calls) == 2
    assert "attempted 2 (budget 2)" in capsys.readouterr().err


def test_success_clears_the_ledger(crib, tmp_path):
    si = _seed(crib, tmp_path, {"a.py": "ha"})
    entries = si.all()
    crib.indexer._record_describe("p", "a.py", entries, entries, "boom")
    assert crib.ledger.counts()["describe"]["failing"] == 1
    for e in entries:
        e["description"] = "now described"
    crib.indexer._record_describe("p", "a.py", entries, entries, None)
    assert "describe" not in crib.ledger.counts()


def test_partial_describe_counts_as_a_failure(crib, tmp_path):
    """No exception, but symbols left blank after the mop-up: still a failure,
    or the backlog would retry it every start (the 2026-11-09 incident)."""
    si = _seed(crib, tmp_path, {"a.py": "ha"})
    entries = si.all()
    crib.indexer._record_describe("p", "a.py", entries, entries, None)
    row = crib.ledger.due("describe", {"p:a.py": "ha"}, _provider(crib))[1]["p:a.py"]
    assert "left undescribed" in row.last_error


def _provider(crib) -> str:
    from crib.genledger import provider_id

    return provider_id(crib.config.generate, "elaborate")


# ── enrichment ───────────────────────────────────────────────────────────────


def test_background_enrichment_backs_off_explicit_does_not(tmp_path, monkeypatch):
    import crib.generate as gen_mod
    from crib.generate import GenerationError

    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    cfg = Config()  # default summary labels: enrichment has facets to generate
    cfg.generate.mode = "on"  # background enrichment is what this tests
    assert cfg.retrieve.summary_labels
    c = Crib(Paths.resolve().ensure(), cfg, InMemoryStore())
    calls = {"n": 0}

    async def failing(*a, **k):
        calls["n"] += 1
        raise GenerationError("provider returned nothing")

    # _generate_enrichment imports these from crib.generate at call time
    monkeypatch.setattr(gen_mod, "agenerate_structured", failing)
    monkeypatch.setattr(gen_mod, "agenerate", failing)

    async def body():
        await c.store_note(
            "# A\nalpha body\n\n# B\nbeta body\n", title="n", project="p"
        )
        await c.enrich(project="p", background=True)  # first start: attempted, fails
        first = calls["n"]
        await c.enrich(project="p", background=True)  # next start: backing off
        second = calls["n"] - first
        r = await c.enrich(project="p")  # explicit: runs regardless
        return first, second, calls["n"] - first, r

    first, second, explicit, _ = run(body())
    assert first > 0
    assert second == 0
    assert explicit > 0
    assert c.ledger.counts()["enrich"]["failing"] >= 1


def _generation_on() -> Config:
    """These tests drive background generation, which [generate] mode "on"
    allows (the default without a models config is "explicit")."""
    cfg = Config()
    cfg.generate.mode = "on"
    return cfg
