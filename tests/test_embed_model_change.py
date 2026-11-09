"""A full reindex recreates the vector store when the embedder changes, whether
the dimension moved or only the model did (two 384-d models look alike)."""

from __future__ import annotations

from types import SimpleNamespace

from crib.app import Crib
from crib.store import InMemoryStore, Record


def _crib(store, model_id, dim=2):
    calls = []
    fake = SimpleNamespace(
        store=store,
        embedder=SimpleNamespace(model_id=model_id, dim=dim),
        index=SimpleNamespace(invalidate_all_caches=lambda: calls.append(1)),
    )
    return fake, calls


def _filled():
    s = InMemoryStore()
    s.upsert([Record("a", [1.0, 0.0], "doc", {"project": "p"})])
    return s


def test_first_full_reindex_records_the_model_without_recreating():
    s = _filled()
    fake, calls = _crib(s, "fe:small")
    assert Crib._maybe_recreate_for_dim(fake) is False  # type: ignore[arg-type]
    assert s.meta_get("embed_model") == "fe:small"
    assert s.current_dim() == 2 and calls == []


def test_same_model_is_left_alone():
    s = _filled()
    s.meta_set("embed_model", "fe:small")
    fake, calls = _crib(s, "fe:small")
    assert Crib._maybe_recreate_for_dim(fake) is False  # type: ignore[arg-type]
    assert s.current_dim() == 2 and calls == []


def test_a_same_dimension_model_change_recreates():
    s = _filled()
    s.meta_set("embed_model", "fe:small")
    fake, calls = _crib(s, "st:other")
    assert Crib._maybe_recreate_for_dim(fake) is True  # type: ignore[arg-type]
    assert s.current_dim() is None
    assert s.meta_get("embed_model") == "st:other"
    assert calls == [1]


def test_a_dimension_change_recreates():
    s = _filled()
    fake, calls = _crib(s, "fe:large", dim=3)
    assert Crib._maybe_recreate_for_dim(fake) is True  # type: ignore[arg-type]
    assert s.current_dim() is None and calls == [1]
