"""Asymmetric query-instruction handling (recall-sensitive, regression-prone)."""

from crib.config import EmbedConfig
from crib.embed import (
    _BGE_EN_QUERY_INSTRUCTION,
    HashEmbedder,
    _resolve_query_prefix,
)


def test_bge_en_gets_query_instruction_by_default():
    cfg = EmbedConfig(model="st:BAAI/bge-small-en-v1.5")
    assert _resolve_query_prefix(cfg, "BAAI/bge-small-en-v1.5") == _BGE_EN_QUERY_INSTRUCTION


def test_explicit_empty_prefix_disables_instruction():
    cfg = EmbedConfig(model="st:BAAI/bge-small-en-v1.5", query_prefix="")
    assert _resolve_query_prefix(cfg, "BAAI/bge-small-en-v1.5") == ""


def test_custom_prefix_wins():
    cfg = EmbedConfig(model="st:whatever", query_prefix="query: ")
    assert _resolve_query_prefix(cfg, "whatever") == "query: "


def test_non_bge_models_get_no_instruction():
    cfg = EmbedConfig(model="fe:intfloat/e5-small")
    assert _resolve_query_prefix(cfg, "intfloat/e5-small") == ""
    # multilingual bge-m3 doesn't use the English s2p instruction
    assert _resolve_query_prefix(EmbedConfig(model="fe:BAAI/bge-m3"), "BAAI/bge-m3") == ""


def test_hash_embedder_query_is_symmetric():
    emb = HashEmbedder(dim=64)
    assert emb.embed_query(["hello world"]) == emb.embed(["hello world"])


# ── The hash fallback must not silently answer over real-model vectors ────────

def _uninstalled(monkeypatch):
    """Make every real backend look uninstalled (the ImportError path)."""
    import crib.embed as e
    for name in ("SentenceTransformerEmbedder", "FastEmbedEmbedder"):
        monkeypatch.setattr(e, name, lambda *a, **k: (_ for _ in ()).throw(
            ImportError("no backend here")))


def test_hash_fallback_still_applies_to_an_empty_store(monkeypatch):
    import pytest

    from crib.embed import build_embedder
    _uninstalled(monkeypatch)
    cfg = EmbedConfig(model="fe:BAAI/bge-small-en-v1.5")
    # nothing stored yet → degrade (a missing optional dep can't brick the server)
    assert isinstance(build_embedder(cfg, stored_dim=None), HashEmbedder)
    # …and a store that already holds hash vectors at the same dim is compatible
    assert isinstance(build_embedder(cfg, stored_dim=cfg.dim), HashEmbedder)
    # but real-model vectors are NOT: hash queries against them rank by a
    # meaningless dot product, at full confidence, with nothing saying so.
    with pytest.raises(RuntimeError, match="not installed"):
        build_embedder(cfg, stored_dim=384)
