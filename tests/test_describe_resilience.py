"""Describe-path resilience: 429 retry/backoff, failure visibility, the redo gate.

Born 2026-09-14 from a live incident: a zai coding-plan sweep 429'd most of its
describes, and "fast" on the ticker was actually every call aborting — failures
were invisible mid-run and unaggregated at the end. These tests pin the three
guarantees that fix it.
"""

from __future__ import annotations

import asyncio

import pytest

import crib.generate as gen
from crib.app import Crib
from crib.codeindexer import _stale_symbols, _parts
from crib.config import Config, GenerateConfig
from crib.paths import Paths
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


# --- 1. rate-limit retry with backoff (crib/generate.py) ----------------------


def _patch_bridge(monkeypatch, script):
    """Replace llmkit's chat_structured with `script`: each entry is either an
    exception to raise or a (code, data) tuple to return. Records attempts."""
    import llmkit.bridge as bridge

    state = {"attempts": 0}

    def fake(provider, request, **kw):
        step = script[min(state["attempts"], len(script) - 1)]
        state["attempts"] += 1
        if isinstance(step, BaseException):
            raise step
        return step

    monkeypatch.setattr(bridge, "chat_structured", fake)
    return state


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Backoff must never actually sleep in tests — record the delays instead."""
    delays: list[float] = []
    monkeypatch.setattr(gen.time, "sleep", delays.append)
    return delays


def test_rate_limited_generation_retries_then_succeeds(monkeypatch, no_sleep):
    state = _patch_bridge(
        monkeypatch,
        [
            RuntimeError("Error code: 429 - rate limit exceeded"),
            RuntimeError("Error code: 429 - quota exceeded"),
            (0, {"symbols": []}),
        ],
    )
    out = gen.generate_structured(GenerateConfig(), "sys", "user", {})
    assert out == {"symbols": []}
    assert state["attempts"] == 3
    assert no_sleep == [2.0, 4.0]  # doubling backoff, no first-attempt wait


def test_non_rate_limit_fails_fast(monkeypatch, no_sleep):
    state = _patch_bridge(monkeypatch, [ValueError("schema mismatch")])
    with pytest.raises(gen.GenerationError):
        gen.generate_structured(GenerateConfig(), "sys", "user", {})
    assert state["attempts"] == 1
    assert no_sleep == []  # nothing transient about it — no wait


def test_rate_limit_gives_up_after_max_attempts(monkeypatch, no_sleep):
    state = _patch_bridge(monkeypatch, [RuntimeError("429 too many requests")] * 10)
    with pytest.raises(gen.GenerationError):
        gen.generate_structured(GenerateConfig(), "sys", "user", {})
    assert state["attempts"] == gen._RATE_LIMIT_ATTEMPTS
    assert no_sleep == [2.0, 4.0, 8.0]


# --- 2. failure visibility (codeindexer aggregation + sweeps counter) ---------


def test_sweep_aggregates_describe_failures(crib, tmp_path, monkeypatch):
    """Files whose describe failed still count as indexed (structure survives)
    but are counted in `describes_failed` — never again invisible."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def a(): pass\n")
    (root / "b.py").write_text("def b(): pass\n")

    def fake(
        rt, rel, proj, patch_edges, existing=None, describe_mode="inline", sweep=False
    ):
        return {"symbols": 1, "described": 0, "descriptions_error": "429 …"}

    monkeypatch.setattr(crib.indexer, "_index_code_file_tracked", fake)
    out = asyncio.run(crib._index_project_code("p", root, ["**/*.py"]))
    assert out["files_indexed"] == 2  # structure landed despite the 429s
    assert out["describes_failed"] == 2
    assert out["described"] == 0


def test_ticker_shows_desc_fail(capsys):
    from crib.codestore import format_sweep

    clean = format_sweep({"done": 3, "total": 10}, now=1e9)
    assert "desc-fail" not in clean
    dirty = format_sweep({"done": 3, "total": 10, "failed": 2}, now=1e9)
    assert "2 desc-fail" in dirty


# --- 3. the redo gate: a failed describe is retried next sweep -----------------


def test_stale_includes_symbols_without_descriptions():
    """The gate reads the PRIOR (store) entry — that's where a failed describe
    leaves its mark: same content_hash but empty description / missing keywords
    key → STILL STALE → the next sweep retries exactly these files."""
    key = _parts({"name": "run", "container": []})
    # a fresh extraction: identity + body hash, no facets yet (describe attaches
    # them AFTER the staleness decision)
    fresh_entry = [{"name": "run", "container": [], "content_hash": "h1"}]

    good = {
        key: {"content_hash": "h1", "description": "does a thing", "keywords": ["a"]}
    }
    assert _stale_symbols(fresh_entry, good) == []  # healthy: skips free

    failed = {key: {"content_hash": "h1", "description": "", "keywords": []}}
    assert _stale_symbols(fresh_entry, failed) == fresh_entry  # 429'd → retried

    no_kw = {key: {"content_hash": "h1", "description": "does a thing"}}
    assert _stale_symbols(fresh_entry, no_kw) == fresh_entry  # kw backfill

    changed = {
        key: {"content_hash": "h0", "description": "does a thing", "keywords": ["a"]}
    }
    assert _stale_symbols(fresh_entry, changed) == fresh_entry  # body moved
