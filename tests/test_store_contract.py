"""The Store contract, run against every backend.

One suite, parametrized over every vector-store backend crib ships or is building,
so a new backend is accepted by passing the SAME tests the current ones pass — not
tests written to fit it. A backend whose module isn't there yet is skipped, never
faked. Where today's backends genuinely disagree, the documented contract wins and
the divergence is marked per backend with the reason, so it stays visible instead
of being averaged away.

Backends: InMemory, Json, Chroma embedded, Chroma shared (a real `chroma run` on
loopback), DuckDB embedded, DuckDB over Quack.
"""

from __future__ import annotations

import contextlib
import math
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from crib.store import InMemoryStore, JsonStore, Record, Store

# ── vectors ──────────────────────────────────────────────────────────────────


def unit(*xs: float) -> list[float]:
    n = math.sqrt(sum(x * x for x in xs))
    return [x / n for x in xs]


X, Y, Z = unit(1, 0, 0), unit(0, 1, 0), unit(0, 0, 1)
XY = unit(1, 1, 0)  # 45° from X and Y


def rec(id: str, vec: list[float], doc: str = "", **meta: Any) -> Record:
    return Record(id, vec, doc or f"doc {id}", {"project": "p", **meta})


def top(store: Store, vec: list[float], k: int, where: dict[str, Any] | None = None):
    """The store's nearest-neighbour query. `where` is a metadata filter dict,
    not SQL; a backend that builds SQL text is covered by the hostile-value tests."""
    # pi-lens-ignore: python-sql-injection
    return store.query(vec, k, where)


# ── backends ─────────────────────────────────────────────────────────────────


@dataclass
class Backend:
    name: str
    store: Store
    reopen: Callable[[], Store] | None  # a fresh instance over the same data
    fixed_dim: bool  # refuses vectors of a different dimension once one exists
    shared: bool  # several clients may open it at once
    new_client: Callable[[], Store] | None = None  # shared backends: another client


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def chroma_server(tmp_path_factory):
    """A real `chroma run` bound to loopback, for the shared-mode baseline."""
    exe = shutil.which("chroma", path=str(Path(sys.executable).parent)) or shutil.which(
        "chroma"
    )
    if not exe:
        pytest.skip("no `chroma` executable for the shared-mode baseline")
    import chromadb

    path, port = tmp_path_factory.mktemp("chroma-shared"), _free_port()
    proc = subprocess.Popen(
        [exe, "run", "--path", str(path), "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                chromadb.HttpClient(host="127.0.0.1", port=port).heartbeat()
                break
            except Exception:  # noqa: BLE001 — not up yet
                if proc.poll() is not None or time.monotonic() > deadline:
                    pytest.skip("`chroma run` did not come up")
                time.sleep(0.25)
        yield port
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(10)
        if proc.poll() is None:
            proc.kill()


def _make_memory(tmp_path, request) -> Backend:
    return Backend("memory", InMemoryStore(), None, fixed_dim=False, shared=False)


def _make_json(tmp_path, request) -> Backend:
    path = tmp_path / "store.json"
    return Backend(
        "json", JsonStore(path), lambda: JsonStore(path), fixed_dim=False, shared=False
    )


def _make_chroma_embedded(tmp_path, request) -> Backend:
    from crib.store import ChromaStore

    path = str(tmp_path / "chroma")
    return Backend(
        "chroma-embedded",
        ChromaStore.embedded(path),
        lambda: ChromaStore.embedded(path),
        fixed_dim=True,
        shared=False,
    )


def _make_chroma_shared(tmp_path, request) -> Backend:
    from crib.store import ChromaStore

    port = request.getfixturevalue("chroma_server")

    def client() -> Store:
        return ChromaStore.shared("127.0.0.1", port)

    store = client()
    store.recreate()  # one server for the session: start every test empty
    return Backend(
        "chroma-shared", store, client, fixed_dim=True, shared=True, new_client=client
    )


def _make_duckdb_embedded(tmp_path, request) -> Backend:
    mod = pytest.importorskip("crib.store_duckdb", reason="DuckDB store not built yet")
    path = tmp_path / "store.duckdb"
    store = mod.DuckDBStore.embedded(path)
    request.addfinalizer(store.close)

    def reopen() -> Store:
        store.close()
        again = mod.DuckDBStore.embedded(path)
        request.addfinalizer(again.close)
        return again

    return Backend("duckdb-embedded", store, reopen, fixed_dim=True, shared=False)


def _make_duckdb_quack(tmp_path, request) -> Backend:
    mod = pytest.importorskip("crib.store_duckdb", reason="DuckDB store not built yet")
    if not hasattr(mod, "QuackStore"):
        pytest.skip("Quack shared path not built yet")
    server = mod.serve_for_tests(tmp_path / "store.duckdb")
    request.addfinalizer(server.stop)

    def client() -> Store:
        c = mod.QuackStore.connect(server.uri, server.token)
        request.addfinalizer(c.close)
        return c

    return Backend(
        "duckdb-quack", client(), client, fixed_dim=True, shared=True, new_client=client
    )


BACKENDS = {
    "memory": _make_memory,
    "json": _make_json,
    "chroma-embedded": _make_chroma_embedded,
    "chroma-shared": _make_chroma_shared,
    "duckdb-embedded": _make_duckdb_embedded,
    "duckdb-quack": _make_duckdb_quack,
}

# Where a backend departs from the documented contract today: {test: {backend: why}}.
# strict xfail — if a backend starts conforming, the mark fails loudly and comes off.
DIVERGES = {
    "test_set_meta_replaces_not_merges": {
        "chroma-embedded": "chroma's collection.update MERGES metadata, so a key "
        "dropped from the schema lingers (and _meta_stable then re-sends it on every "
        "reindex)",
        "chroma-shared": "chroma's collection.update MERGES metadata (see embedded)",
    },
}


@pytest.fixture(params=list(BACKENDS))
def backend(request, tmp_path) -> Backend:
    b = BACKENDS[request.param](tmp_path, request)
    why = DIVERGES.get(request.node.originalname, {}).get(b.name)
    if why:
        request.applymarker(pytest.mark.xfail(reason=why, strict=True))
    return b


# ── empty store ──────────────────────────────────────────────────────────────


def test_empty_store(backend):
    s = backend.store
    assert s.current_dim() is None
    assert top(s, X, 5) == []
    assert s.get_meta({"project": "p"}) == {}
    assert s.get_docs({"project": "p"}) == {}
    s.upsert([])  # no-ops, never errors
    s.delete([])
    s.set_meta({})


# ── writes and reads ─────────────────────────────────────────────────────────


def test_upsert_then_read_back(backend):
    s = backend.store
    s.upsert([rec("a", X, "alpha", n=1), rec("b", Y, "beta", n=2)])
    assert s.current_dim() == 3
    assert s.get_docs({"project": "p"}) == {
        "a": ("alpha", {"project": "p", "n": 1}),
        "b": ("beta", {"project": "p", "n": 2}),
    }
    assert s.get_meta({"project": "p"}) == {
        "a": {"project": "p", "n": 1},
        "b": {"project": "p", "n": 2},
    }


def test_upsert_overwrites_by_id(backend):
    s = backend.store
    s.upsert([rec("a", X, "first", n=1)])
    s.upsert([rec("a", Y, "second", n=2)])
    assert s.get_docs({"project": "p"}) == {"a": ("second", {"project": "p", "n": 2})}
    hits = top(s, Y, 1)
    assert [h.id for h in hits] == ["a"]
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)  # the NEW vector is stored


def test_metadata_value_types_round_trip(backend):
    """crib's chunk metadata uses str, int and float (file_mtime is a float,
    occurrence/window_idx are ints). Each must come back as the type it went in."""
    s = backend.store
    meta = {"project": "p", "s": "text", "i": 7, "f": 1731097200.123456, "zero": 0}
    s.upsert([Record("a", X, "doc", meta)])
    got = s.get_meta({"project": "p"})["a"]
    assert got == meta
    assert {k: type(v) for k, v in got.items()} == {k: type(v) for k, v in meta.items()}


def test_delete(backend):
    s = backend.store
    s.upsert([rec("a", X), rec("b", Y), rec("c", Z)])
    s.delete(["a", "c", "never-existed"])  # a missing id is not an error
    assert set(s.get_meta({"project": "p"})) == {"b"}
    assert [h.id for h in top(s, X, 5)] == ["b"]


def test_set_meta_updates_without_reembedding(backend):
    s = backend.store
    s.upsert([rec("a", X, "alpha", tag="old")])
    s.set_meta({"a": {"project": "p", "tag": "new"}, "never-existed": {"project": "p"}})
    assert s.get_docs({"project": "p"}) == {
        "a": ("alpha", {"project": "p", "tag": "new"})
    }
    hit = top(s, X, 1)[0]  # vector and document untouched
    assert (hit.id, hit.document) == ("a", "alpha")
    assert hit.score == pytest.approx(1.0, abs=1e-5)
    assert set(s.get_meta({"project": "p"})) == {"a"}  # the unknown id wasn't created


def test_set_meta_replaces_not_merges(backend):
    """The Store contract says set_meta REPLACES a record's metadata. The indexer
    always passes the full new metadata, so a key dropped from the schema must
    disappear rather than linger."""
    s = backend.store
    s.upsert([rec("a", X, legacy="drop-me", keep="x")])
    s.set_meta({"a": {"project": "p", "keep": "x"}})
    assert s.get_meta({"project": "p"})["a"] == {"project": "p", "keep": "x"}


# ── filters ──────────────────────────────────────────────────────────────────


@pytest.fixture
def filtered(backend):
    s = backend.store
    s.upsert(
        [
            rec("a", X, store="notes", n=1, f=1.5),
            rec("b", Y, store="design", n=2, f=2.5),
            rec("c", XY, store="notes", n=1, f=2.5),
            Record(
                "d", Z, "doc d", {"project": "q", "store": "notes", "n": 1, "f": 1.5}
            ),
        ]
    )
    return s


@pytest.mark.parametrize(
    "where, expect",
    [
        ({"project": "p"}, {"a", "b", "c"}),
        ({"project": "p", "store": "notes"}, {"a", "c"}),  # several keys AND together
        ({"project": "p", "store": "notes", "n": 1}, {"a", "c"}),
        ({"n": 1}, {"a", "c", "d"}),  # int value
        ({"f": 2.5}, {"b", "c"}),  # float value
        ({"n": "1"}, set()),  # exact match is type-sensitive: 1 != "1"
        ({"absent": "x"}, set()),  # a key no record has matches nothing
        ({"project": "nope"}, set()),
        ({}, {"a", "b", "c", "d"}),  # no constraint
    ],
)
def test_where_filters(filtered, where, expect):
    assert set(filtered.get_meta(where)) == expect
    assert set(filtered.get_docs(where)) == expect
    assert {h.id for h in top(filtered, X, 10, where or None)} == expect


# ── query ────────────────────────────────────────────────────────────────────


def test_query_scores_are_cosine_similarity(backend):
    s = backend.store
    s.upsert([rec("a", X), rec("b", Y), rec("c", XY)])
    scores = {h.id: h.score for h in top(s, X, 3)}
    assert scores["a"] == pytest.approx(1.0, abs=1e-5)
    assert scores["c"] == pytest.approx(math.sqrt(0.5), abs=1e-5)
    assert scores["b"] == pytest.approx(0.0, abs=1e-5)


def test_query_orders_best_first_and_limits_k(backend):
    s = backend.store
    s.upsert(
        [rec("far", Y), rec("near", unit(1, 0.1, 0)), rec("mid", XY), rec("exact", X)]
    )
    hits = top(s, X, 3)
    assert [h.id for h in hits] == ["exact", "near", "mid"]
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
    assert len(top(s, X, 10)) == 4  # k larger than the store returns everything


def test_query_returns_document_and_metadata(backend):
    s = backend.store
    s.upsert([rec("a", X, "alpha body", title="Alpha", n=3)])
    (hit,) = top(s, X, 1)
    assert (hit.id, hit.document, hit.metadata) == (
        "a",
        "alpha body",
        {"project": "p", "title": "Alpha", "n": 3},
    )


def test_query_is_deterministic(backend):
    """Retrieval stability is a standing invariant: the same query on the same
    data returns the same ordered ids every time."""
    s = backend.store
    vecs = [unit(1, i / 10, (i % 3) / 7) for i in range(20)]
    s.upsert([rec(f"r{i:02d}", v) for i, v in enumerate(vecs)])
    q = unit(1, 0.42, 0.13)
    first = [h.id for h in top(s, q, 10)]
    assert all([h.id for h in top(s, q, 10)] == first for _ in range(5))


def test_query_with_a_wrong_dimension_raises(backend):
    """A query vector from a different embedder must fail loudly, never score
    against truncated vectors."""
    s = backend.store
    s.upsert([rec("a", X)])
    with pytest.raises(Exception):
        top(s, unit(1, 0), 1)


# ── dimension ────────────────────────────────────────────────────────────────


def test_fixed_dim_backends_refuse_a_second_dimension(backend):
    if not backend.fixed_dim:
        pytest.skip(f"{backend.name} stores vectors of any dimension")
    s = backend.store
    s.upsert([rec("a", X)])
    with pytest.raises(Exception):
        s.upsert([rec("b", unit(1, 0))])
    assert s.current_dim() == 3


def test_recreate_empties_and_accepts_a_new_dimension(backend):
    s = backend.store
    s.upsert([rec("a", X), rec("b", Y)])
    s.recreate()
    assert s.current_dim() is None
    assert s.get_meta({"project": "p"}) == {}
    s.upsert([rec("c", unit(1, 0))])  # e.g. a profile switch to another embedder
    assert s.current_dim() == 2
    assert [h.id for h in top(s, unit(1, 0), 5)] == ["c"]


# ── persistence ──────────────────────────────────────────────────────────────


def test_survives_reopen(backend):
    if backend.reopen is None:
        pytest.skip(f"{backend.name} keeps nothing beyond the process")
    backend.store.upsert([rec("a", X, "alpha", n=1), rec("b", Y, "beta", n=2)])
    backend.store.set_meta({"b": {"project": "p", "n": 3}})
    backend.store.delete(["a"])
    s = backend.reopen()
    assert s.get_docs({"project": "p"}) == {"b": ("beta", {"project": "p", "n": 3})}
    assert s.current_dim() == 3
    assert [h.id for h in top(s, Y, 5)] == ["b"]


# ── hostile values (guards any path that builds SQL text) ────────────────────

HOSTILE = [
    "O'Brien",
    'say "hi"',
    "back\\slash",
    "'); DROP TABLE chunks; --",
    "%_wildcards_%",
    "naïve — 日本語 🦆",
    "\u0001\u001f control",
    "new\nline\ttab",
    "$1 ? :name {}",
]


@pytest.mark.parametrize("value", HOSTILE, ids=range(len(HOSTILE)))
def test_hostile_values_round_trip_and_filter_exactly(backend, value):
    s = backend.store
    s.upsert(
        [
            Record(
                f"id {value}", X, f"doc {value}", {"project": value, "title": value}
            ),
            rec("decoy", Y),
        ]
    )
    want = {f"id {value}": (f"doc {value}", {"project": value, "title": value})}
    assert s.get_docs({"project": value}) == want
    assert [h.id for h in top(s, X, 5, {"project": value})] == [f"id {value}"]
    assert set(s.get_meta({"title": value})) == {f"id {value}"}
    s.set_meta({f"id {value}": {"project": value, "title": "renamed"}})
    s.delete([f"id {value}"])
    assert s.get_meta({"project": value}) == {}
    assert set(s.get_meta({"project": "p"})) == {"decoy"}  # nothing else touched


def test_long_document_round_trips(backend):
    s = backend.store
    doc = "lorem ipsum " * 20_000  # ~240 KB
    s.upsert([rec("a", X, doc)])
    assert s.get_docs({"project": "p"})["a"][0] == doc


# ── several clients (shared backends only) ───────────────────────────────────


def test_concurrent_writers_and_a_reader(backend):
    if not backend.shared:
        pytest.skip(f"{backend.name} is single-client")
    assert backend.new_client is not None
    w1, w2, reader = backend.store, backend.new_client(), backend.new_client()
    errors: list[BaseException] = []

    def write(client: Store, tag: str) -> None:
        try:
            for i in range(25):
                client.upsert([rec(f"{tag}{i:02d}", unit(1, i / 25, 0), writer=tag)])
        except BaseException as e:  # noqa: BLE001 — surfaced below
            errors.append(e)

    threads = [threading.Thread(target=write, args=a) for a in ((w1, "a"), (w2, "b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    assert len(reader.get_meta({"writer": "a"})) == 25
    assert len(reader.get_meta({"writer": "b"})) == 25
    assert len(top(reader, X, 100)) == 50
