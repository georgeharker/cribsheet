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
