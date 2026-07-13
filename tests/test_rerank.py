"""Cross-encoder rerank wiring — range-matched blend over the head, degrades safe.

The rerank is a RANGE-MATCHED term: min-max the fusion score AND the cross-encoder
score over the top `rerank_top_n`, sum, re-order (see `Crib._rerank`). Unlike the old
RRF-of-orders fusion, a decisive reranker preference CAN dethrone the fusion #1 — but
its influence is bounded: both sides span [0,1], so a maximal rerank vote from the
fusion floor at best TIES the fusion top (and a tie keeps the fusion order).

Uses fake rerankers so the tests need no model download.
"""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.store import Hit, InMemoryStore


class _Fixed:
    """Returns a fixed score per document, in order."""

    def __init__(self, scores):
        self._scores = scores

    def scores(self, query, documents):
        return list(self._scores[:len(documents)])


class _PromoteLast:
    """Scores only the last document high."""

    def scores(self, query, documents):
        return [0.0] * (len(documents) - 1) + [1.0]


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    cfg = Config()
    cfg.retrieve.rerank = True
    cfg.retrieve.rerank_top_n = 50
    return Crib(Paths.resolve().ensure(), cfg, InMemoryStore())


def run(coro):
    return asyncio.run(coro)


def _hits(scores):
    return [Hit(f"c{i}", f"doc {i}", {}, s) for i, s in enumerate(scores)]


def test_rerank_decisive_preference_dethrones_close_head(crib):
    # fusion has c0 narrowly over c1; the reranker strongly prefers c1 → c1 wins.
    # (The range-matched design deliberately drops the old "one vote can't dethrone
    # #1" invariant — a decisive cross-encoder judgment on a close head SHOULD win.)
    crib._reranker = _Fixed([0.0, 1.0, 0.2])
    out = crib._rerank("q", _hits([0.51, 0.50, 0.10]))
    assert [h.id for h in out] == ["c1", "c0", "c2"]


def test_rerank_influence_is_bounded(crib):
    # both sides are min-maxed to [0,1], so the fusion FLOOR + a maximal rerank vote
    # (0+1) at best TIES the fusion TOP + rerank floor (1+0) — and the stable sort
    # resolves a tie in fusion order. The reranker can't leapfrog outright.
    crib._reranker = _Fixed([0.0, 0.0, 1.0])
    out = crib._rerank("q", _hits([0.9, 0.5, 0.1]))
    assert out[0].id == "c0"                         # fusion #1 holds on a bare tie


def test_rerank_reorders_head_only(crib):
    crib.config.retrieve.rerank_top_n = 3
    crib._reranker = _Fixed([0.1, 0.9, 1.0])
    out = crib._rerank("q", _hits([0.9, 0.8, 0.7, 0.6, 0.5]))
    assert [h.id for h in out] == ["c1", "c0", "c2", "c3", "c4"]  # tail untouched


def test_rerank_promotes_through_lookup(crib):
    # end-to-end wiring: rerank=True engages the blend inside lookup
    for i in range(5):
        run(crib.store_note(f"widget number {i} alpha beta", title=f"n{i}", project="p"))
    base = [h.relpath for h in crib.lookup("widget", project="p", rerank=False)]
    crib._reranker = _PromoteLast()               # inject fake, skip model load
    reranked = [h.relpath for h in crib.lookup("widget", project="p", rerank=True)]

    last = base[-1]
    assert reranked.index(last) < base.index(last)   # reranker promoted it


def _code_hits(scores):
    return [{"fqname": f"m.f{i}", "description": f"d{i}", "_score": s}
            for i, s in enumerate(scores)]


def test_code_rerank_same_blend_as_notes(crib, monkeypatch):
    # structural twin of Crib._rerank: min-max blend + cross-encoder over the head
    monkeypatch.setattr("crib.codequery._RERANK_N", 3)
    crib._reranker = _Fixed([0.0, 1.0, 0.2])
    out = crib._rerank_code("q", _code_hits([0.51, 0.50, 0.10]))
    assert [h["fqname"] for h in out] == ["m.f1", "m.f0", "m.f2"]   # decisive dethrone


def test_code_rerank_head_only_and_degrades(crib, monkeypatch):
    monkeypatch.setattr("crib.codequery._RERANK_N", 3)
    crib._reranker = _Fixed([0.1, 0.9, 1.0])
    out = crib._rerank_code("q", _code_hits([0.9, 0.8, 0.7, 0.6]))
    assert [h["fqname"] for h in out] == ["m.f1", "m.f0", "m.f2", "m.f3"]  # tail intact

    class _Boom:
        def scores(self, *_):
            raise RuntimeError("no model")

    crib._reranker = _Boom()
    hits = _code_hits([0.9, 0.8])
    assert crib._rerank_code("q", hits) == hits          # degrades to blend order


def test_rerank_degrades_when_model_unavailable(crib):
    run(crib.store_note("alpha beta gamma", title="a", project="p"))

    class _Boom:
        def scores(self, *_):
            raise RuntimeError("no model")

    crib._reranker = _Boom()
    hits = crib.lookup("alpha", project="p", rerank=True)      # must not raise
    assert hits and hits[0].relpath.startswith("a")
