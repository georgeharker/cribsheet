"""Cross-encoder rerank wiring — reorders the head, keeps cosine, degrades safe.

Uses a fake reranker so the test needs no model download.
"""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore


class _PromoteLast:
    """Scores only the last document high — so it should be promoted, but (under
    fusion) not all the way past a strongly-ranked head."""

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


def test_rerank_fuses_promotes_but_does_not_dethrone(crib):
    for i in range(5):
        run(crib.store_note(f"widget number {i} alpha beta", title=f"n{i}", project="p"))
    base = [h.relpath for h in crib.lookup("widget", project="p", rerank=False)]
    crib._reranker = _PromoteLast()               # inject fake, skip model load
    reranked = [h.relpath for h in crib.lookup("widget", project="p", rerank=True)]

    last = base[-1]
    assert reranked.index(last) < base.index(last)   # reranker promoted it...
    assert reranked[0] == base[0]                    # ...but one vote can't dethrone #1


def test_rerank_degrades_when_model_unavailable(crib):
    run(crib.store_note("alpha beta gamma", title="a", project="p"))

    class _Boom:
        def scores(self, *_):
            raise RuntimeError("no model")

    crib._reranker = _Boom()
    hits = crib.lookup("alpha", project="p", rerank=True)      # must not raise
    assert hits and hits[0].relpath.startswith("a")
