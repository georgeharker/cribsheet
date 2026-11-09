"""The persistent embedding cache (crib.embedcache): text -> vector per model,
stored as plain vectors (no index) in the DuckDB store, so a warm start embeds
only text it has never seen."""

from __future__ import annotations

import socket

import pytest

from crib.embedcache import CachedEmbedder, text_hash, wrap


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CountingEmbedder:
    def __init__(self, dim: int = 3) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []
        self.query_prefix = "q: "

    def embed(self, texts):
        self.calls.append(list(texts))
        return [
            [float(len(t)), float(i), 1.0][: self.dim] + [0.5] * (self.dim - 3)
            for i, t in enumerate(texts)
        ]

    def embed_query(self, texts):
        return [[9.0] * self.dim for _ in texts]


@pytest.fixture(scope="module")
def quack_server(tmp_path_factory):
    from crib.store_quack import spawn_server

    root = tmp_path_factory.mktemp("ec-quack")
    try:
        srv = spawn_server(
            root / "crib.duckdb", _free_port(), root / "token", timeout=180
        )
    except RuntimeError as e:
        pytest.skip(f"crib DuckDB server did not start: {e}")
    yield srv
    srv.stop()


@pytest.fixture(params=["duckdb-embedded", "duckdb-quack"])
def store_factory(request, tmp_path):
    if request.param == "duckdb-embedded":
        from crib.store_duckdb import DuckDBStore

        path = tmp_path / "crib.duckdb"
        opened: list = []

        def make_embedded():
            for s in opened:
                s.close()
            opened.clear()
            s = DuckDBStore.embedded(path)
            opened.append(s)
            return s

        request.addfinalizer(lambda: [s.close() for s in opened])
        return make_embedded
    from crib.store_quack import QuackStore

    srv = request.getfixturevalue("quack_server")

    def make_quack():
        s = QuackStore.connect(srv.uri, srv.token)
        request.addfinalizer(s.close)
        return s

    return make_quack


def test_second_embed_of_the_same_text_is_a_hit(store_factory):
    inner = CountingEmbedder()
    ce = CachedEmbedder(inner, store_factory(), "m-hit")
    a = ce.embed(["alpha", "beta"])
    b = ce.embed(["beta", "alpha", "gamma"])
    assert inner.calls == [["alpha", "beta"], ["gamma"]]  # only gamma is new
    assert b[0] == a[1] and b[1] == a[0]  # same text, same vector, right order
    assert (ce.hits, ce.misses) == (2, 3)


def test_duplicates_in_one_call_embed_once(store_factory):
    inner = CountingEmbedder()
    ce = CachedEmbedder(inner, store_factory(), "m-dup")
    out = ce.embed(["x", "x", "y"])
    assert inner.calls == [["x", "y"]]
    assert out[0] == out[1]


def test_a_fresh_process_reuses_persisted_vectors(store_factory):
    first = CountingEmbedder()
    v = CachedEmbedder(first, store_factory(), "m-persist").embed(["the same alias"])
    second = CountingEmbedder()
    w = CachedEmbedder(second, store_factory(), "m-persist").embed(["the same alias"])
    assert second.calls == []  # served from the store
    assert w[0] == pytest.approx(v[0])


def test_models_and_dimensions_never_share_vectors(store_factory):
    s = store_factory()
    CachedEmbedder(CountingEmbedder(3), s, "bge-small").embed(["t"])
    other_model = CountingEmbedder(3)
    CachedEmbedder(other_model, s, "bge-large").embed(["t"])
    other_dim = CountingEmbedder(4)
    CachedEmbedder(other_dim, s, "bge-small").embed(["t"])  # same name, new dim
    assert other_model.calls == [["t"]] and other_dim.calls == [["t"]]


def test_hostile_text_round_trips(store_factory):
    nasty = "'); DROP TABLE crib_embed_cache; -- naïve 🦆 \\ \" ?"
    inner = CountingEmbedder()
    v = CachedEmbedder(inner, store_factory(), "m-nasty").embed([nasty])
    again = CountingEmbedder()
    w = CachedEmbedder(again, store_factory(), "m-nasty").embed([nasty])
    assert w[0] == pytest.approx(v[0])
    assert again.calls == []


def test_everything_else_passes_through(tmp_path):
    from crib.store_duckdb import DuckDBStore

    s = DuckDBStore.embedded(tmp_path / "c.duckdb")
    try:
        inner = CountingEmbedder()
        ce = CachedEmbedder(inner, s, "m")
        assert ce.dim == 3 and ce.query_prefix == "q: "
        assert ce.embed_query(["find me"]) == [[9.0, 9.0, 9.0]]  # queries aren't cached
        assert ce.embed([]) == [] and inner.calls == []
    finally:
        s.close()


def test_wrap_only_when_the_store_can_hold_it(tmp_path):
    from crib.store import InMemoryStore
    from crib.store_duckdb import DuckDBStore

    inner = CountingEmbedder()
    assert wrap(inner, InMemoryStore(), "m") is inner
    s = DuckDBStore.embedded(tmp_path / "c.duckdb")
    try:
        assert isinstance(wrap(inner, s, "m"), CachedEmbedder)
    finally:
        s.close()


def test_text_hash_is_exact():
    assert text_hash("a") != text_hash("a ") and len(text_hash("x")) == 64


def test_crib_on_duckdb_embeds_through_the_cache(tmp_path, monkeypatch):
    """A Crib opened on the DuckDB store gets the cached embedder, so the alias
    vectors and code descriptions it embeds survive a restart."""
    from crib.app import Crib
    from crib.config import Config
    from crib.paths import Paths
    from crib.store_duckdb import DuckDBStore

    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    paths = Paths.resolve().ensure()
    s = DuckDBStore.embedded(paths.duckdb_file)
    try:
        c = Crib(paths, Config(), s)
        assert isinstance(c.embedder, CachedEmbedder)
        c.embedder.embed(["an alias worth keeping"])
        assert c.embedder.misses == 1
        c2 = Crib(paths, Config(), s)  # "next start" on the same store
        c2.embedder.embed(["an alias worth keeping"])
        assert (c2.embedder.hits, c2.embedder.misses) == (1, 0)
    finally:
        s.close()
