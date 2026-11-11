"""The Store contract, run against every backend.

One suite, parametrized over every vector-store backend crib ships or is building,
so a new backend is accepted by passing the SAME tests the current ones pass — not
tests written to fit it. A backend whose module isn't there yet is skipped, never
faked.

Backends: InMemory, Json, DuckDB embedded, DuckDB over Quack (a real crib DuckDB
server on loopback). Chroma was removed 2026-11-09; until then it ran here as
the baseline, and this suite is where its set_meta MERGE (vs the contract's
REPLACE) was caught.
"""

from __future__ import annotations

import math
import socket
import threading
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from crib.errors import CribUserError
from crib.store import InMemoryStore, Record, Store

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


def _make_memory(tmp_path, request) -> Backend:
    return Backend("memory", InMemoryStore(), None, fixed_dim=False, shared=False)


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


@pytest.fixture(scope="session")
def quack_server(tmp_path_factory):
    """One crib DuckDB server (Quack on loopback) for the session, launched the
    way sharedserver launches it: `python -m crib.store_quack`. Quack installs
    into the default extension dir on first use, so a box that can't reach
    extensions.duckdb.org skips this backend rather than failing."""
    quack = pytest.importorskip("crib.store_quack", reason="Quack path not built yet")
    root = tmp_path_factory.mktemp("duckdb-quack")
    try:
        server = quack.spawn_server(
            root / "crib.duckdb", _free_port(), root / "token", timeout=180
        )
    except RuntimeError as e:
        pytest.skip(f"crib DuckDB server did not start: {e}")
    yield server
    server.stop()


def _make_duckdb_quack(tmp_path, request) -> Backend:
    quack = pytest.importorskip("crib.store_quack", reason="Quack path not built yet")
    server = request.getfixturevalue("quack_server")

    def client() -> Store:
        c = quack.QuackStore.connect(server.uri, server.token)
        request.addfinalizer(c.close)
        return c

    store = quack.QuackStore.connect(server.uri, server.token)
    request.addfinalizer(store.close)
    # One server for the session: start every test empty, caches and settings
    # included, not just the chunks recreate() drops.
    store.recreate()
    with store._writing() as ex:
        for table in ("crib_embed_cache", "crib_record_cache", "crib_gen_ledger"):
            # pi-lens-ignore: python-sql-injection
            ex.run(f"DELETE FROM {table}")
        ex.run("DELETE FROM crib_meta WHERE key = 'embed_model'")
    return Backend(
        "duckdb-quack", store, client, fixed_dim=True, shared=True, new_client=client
    )


BACKENDS = {
    "memory": _make_memory,
    "duckdb-embedded": _make_duckdb_embedded,
    "duckdb-quack": _make_duckdb_quack,
}


@pytest.fixture(params=list(BACKENDS))
def backend(request, tmp_path) -> Backend:
    return BACKENDS[request.param](tmp_path, request)


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
            rec("a", X, kind="note", n=1, f=1.5),
            rec("b", Y, kind="design", n=2, f=2.5),
            rec("c", XY, kind="note", n=1, f=2.5),
            Record("d", Z, "doc d", {"project": "q", "kind": "note", "n": 1, "f": 1.5}),
        ]
    )
    return s


@pytest.mark.parametrize(
    "where, expect",
    [
        ({"project": "p"}, {"a", "b", "c"}),
        ({"project": "p", "kind": "note"}, {"a", "c"}),  # several keys AND together
        ({"project": "p", "kind": "note", "n": 1}, {"a", "c"}),
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


# ── filter semantics (every backend agrees) ──────────────────────────────────


def test_any_matches_list_membership(backend):
    s = backend.store
    s.upsert(
        [
            rec("a", X, labels=["design", "x"]),
            rec("b", Y, labels=["plan"]),
            rec("c", Z),  # no labels key
            rec("d", XY, labels="design"),  # not a list: never a member
        ]
    )
    assert {h.id for h in top(s, X, 10, {"labels": {"$any": ["design"]}})} == {"a"}
    assert {h.id for h in top(s, X, 10, {"labels": {"$any": ["plan", "x"]}})} == {
        "a",
        "b",
    }
    assert top(s, X, 10, {"labels": {"$any": []}}) == []
    both = {"project": "p", "labels": {"$any": ["plan"]}}
    assert set(s.get_meta(both)) == {"b"}


def test_any_refuses_a_non_list(backend):
    backend.store.upsert([rec("a", X, labels=["x"])])
    with pytest.raises(CribUserError):
        top(backend.store, X, 5, {"labels": {"$any": "x"}})
    with pytest.raises(CribUserError):
        top(backend.store, X, 5, {"labels": {"$gt": 1}})


@pytest.mark.parametrize(
    "stored, wanted",
    [(True, 1), (1, True), (1, 1.0), (1.0, 1), ("1", 1), (None, None)],
    ids=[
        "bool-vs-int",
        "int-vs-bool",
        "int-vs-float",
        "float-vs-int",
        "str-vs-int",
        "none",
    ],
)
def test_exact_match_is_type_exact(backend, stored, wanted):
    backend.store.upsert([rec("a", X, n=stored)])
    assert top(backend.store, X, 5, {"n": wanted}) == []


def test_ties_break_by_id(backend):
    backend.store.upsert([rec(i, X) for i in ("c", "a", "b")])
    assert [h.id for h in top(backend.store, X, 3)] == ["a", "b", "c"]


def test_nonpositive_k_returns_nothing(backend):
    backend.store.upsert([rec("a", X)])
    assert top(backend.store, X, 0) == []
    assert top(backend.store, X, -1) == []


def test_zero_vector_scores_zero_not_nan(backend):
    """A zero vector has no direction: its cosine is 0, and it ranks below any
    real match rather than (as NaN would) above all of them."""
    backend.store.upsert([rec("zero", [0.0, 0.0, 0.0]), rec("x", X)])
    hits = top(backend.store, X, 2)
    assert [h.id for h in hits] == ["x", "zero"]
    assert hits[1].score == 0.0


def test_store_meta_round_trips(backend):
    s = backend.store
    assert s.meta_get("embed_model") is None
    s.meta_set("embed_model", "fe:BAAI/bge-small-en-v1.5")
    s.meta_set("embed_model", "st:other")
    assert s.meta_get("embed_model") == "st:other"


def test_count_and_projects_without_fetching(backend):
    s = backend.store
    assert s.count({"project": "p"}) == 0 and s.projects() == set()
    s.upsert(
        [
            rec("a", X, relpath="sources/r/a.md"),
            rec("b", Y, relpath="sources/r/b.md"),
            rec("c", Z, relpath="notes.md"),
            Record("d", X, "doc d", {"project": "q", "relpath": "sources/x.md"}),
        ]
    )
    assert s.count({"project": "p"}) == 3
    assert s.count({"project": "p"}, relpath_prefix="sources/") == 2
    assert s.count({}, relpath_prefix="sources/") == 3
    assert s.projects() == {"p", "q"}


def test_apply_writes_upserts_deletes_and_meta_together(backend):
    s = backend.store
    s.upsert([rec("old", X, relpath="n.md"), rec("keep", Y, relpath="n.md")])
    s.apply(
        [rec("new", Z, relpath="n.md")],
        ["old"],
        {"keep": {"project": "p", "relpath": "renamed.md", "tags": ["t"]}},
    )
    assert set(s.get_meta({"project": "p"})) == {"keep", "new"}
    assert s.get_meta({"relpath": "renamed.md"})["keep"]["tags"] == ["t"]
    assert s.count({"project": "p"}, relpath_prefix="renamed") == 1


def test_set_meta_many_rows_in_batches(backend):
    s = backend.store
    s.upsert([rec(f"r{i:03}", X, relpath="a.md") for i in range(450)])
    s.set_meta(
        {f"r{i:03}": {"project": "p", "relpath": "b.md", "i": i} for i in range(450)}
    )
    metas = s.get_meta({"relpath": "b.md"})
    assert len(metas) == 450 and metas["r321"]["i"] == 321


def test_score_ids_scores_stored_vectors(backend):
    s = backend.store
    s.upsert([rec("x", X), rec("y", Y), rec("xy", XY)])
    got = s.score_ids(X, ["x", "xy", "missing"])
    assert set(got) == {"x", "xy"}
    assert got["x"] == pytest.approx(1.0, abs=1e-6)
    assert got["xy"] == pytest.approx(math.sqrt(0.5), abs=1e-6)
    assert s.score_ids(X, []) == {}


def test_in_matches_one_of_several_strings(backend):
    s = backend.store
    s.upsert(
        [
            rec("a", X, kind="note"),
            rec("b", Y, kind="doc"),
            rec("c", Z, kind="design"),
            rec("d", XY, kind=["note"]),
        ]
    )
    assert set(s.get_meta({"kind": {"$in": ["note", "doc"]}})) == {"a", "b"}
    assert {h.id for h in top(s, X, 10, {"kind": {"$in": ["design"]}})} == {"c"}
    assert s.get_meta({"kind": {"$in": []}}) == {}
    with pytest.raises(CribUserError):
        s.get_meta({"kind": {"$in": "note"}})


# ── items (crib.items) ───────────────────────────────────────────────────────


def _item(i, kind="note", **kw):
    from crib.items import Item, facet_of

    return Item(
        i,
        kind,
        kw.pop("project", "p"),
        kw.pop("relpath", f"{i}.md"),
        kw.pop("title", f"title {i}"),
        kw.pop("h", "h"),
        kw.pop("tv", [1.0, 0.0]),
        kw.pop("bv", [0.0, 1.0]),
        facet_of(kind, kw),
    )


def test_items_round_trip_with_their_facet(backend):
    s = backend.store
    s.apply(
        [],
        [],
        {},
        [
            _item("n", "note", tags=["a", "b"], source="manual"),
            _item("d", "design", status="superseded"),
            _item("p", "plan"),  # defaults: status todo
            _item("doc", "doc", relpath="sources/r/README.md"),
        ],
    )
    got = s.items({"project": "p"})
    assert set(got) == {"n", "d", "p", "doc"}
    assert got["n"].facet == {
        "source": "manual",
        "tags": ["a", "b"],
        "source_path": None,
    }
    assert got["d"].facet["status"] == "superseded"
    assert got["p"].facet["status"] == "todo"
    assert got["doc"].facet == {}
    assert got["n"].title_vec == pytest.approx([1.0, 0.0])
    assert set(s.items({"kind": {"$in": ["note", "doc"]}})) == {"n", "doc"}


def test_item_upsert_replaces_and_delete_drops_the_facet_too(backend):
    s = backend.store
    s.apply([], [], {}, [_item("x", "plan", status="todo")])
    s.apply([], [], {}, [_item("x", "plan", status="done", title="renamed")])
    got = s.items({"id": "x"})["x"]
    assert (got.title, got.facet["status"]) == ("renamed", "done")
    s.apply([], [], {}, None, ["x"])
    assert s.items({"id": "x"}) == {}
    s.apply([], [], {}, [_item("x", "note")])  # same id, another kind: no stale facet
    assert s.items({"id": "x"})["x"].facet["source"] is None


def test_items_refuse_a_non_core_filter(backend):
    with pytest.raises(CribUserError):
        backend.store.items({"status": "todo"})


def test_recreate_empties_the_items_too(backend):
    s = backend.store
    s.upsert([rec("a", X)])
    s.apply([], [], {}, [_item("i")])
    s.recreate()
    assert s.items({}) == {}
