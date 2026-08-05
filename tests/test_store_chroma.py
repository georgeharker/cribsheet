"""ChromaStore's stale-collection-handle recovery (robustness-fixes 1.4).

Chroma binds a collection handle to a collection UUID, so any process still
holding one after another process calls `recreate()` used to fail every op with
"Collection [uuid] does not exist" until restart. The store now re-resolves and
retries once — and `recreate()` no longer swallows non-not-found failures.
"""

from __future__ import annotations

import pytest

from crib.store import ChromaStore, Record, _is_missing_collection

chromadb = pytest.importorskip("chromadb")


def _rec(rid: str, dim: int = 4) -> Record:
    return Record(rid, [1.0] + [0.0] * (dim - 1), f"doc {rid}", {"project": "p"})


@pytest.fixture()
def chroma_dir(tmp_path):
    d = tmp_path / "chroma"
    d.mkdir()
    return str(d)


def test_second_handle_survives_recreate(chroma_dir):
    """Two stores on one dir: A recreates, B keeps working transparently."""
    a = ChromaStore.embedded(chroma_dir)
    b = ChromaStore.embedded(chroma_dir)
    a.upsert([_rec("one")])
    assert b.get_meta({"project": "p"})  # B's handle is live to start with
    stale = b._col

    a.recreate()

    b.upsert([_rec("two")])                      # write through a stale handle
    assert set(b.get_meta({"project": "p"})) == {"two"}
    assert set(b.get_docs({"project": "p"})) == {"two"}
    assert [h.id for h in b.query([1.0, 0.0, 0.0, 0.0], 3)] == ["two"]
    assert b.current_dim() == 4
    b.set_meta({"two": {"project": "p", "tag": "x"}})
    assert b.get_meta({"project": "p"})["two"]["tag"] == "x"
    b.delete(["two"])
    assert b.get_meta({"project": "p"}) == {}
    assert b._col is not stale                   # handle was actually refreshed


def test_recreate_after_dim_change_across_handles(chroma_dir):
    """The live trigger: reindex recreates on an embedder-dim change; the other
    process must then be able to write the new-dimension vectors."""
    a = ChromaStore.embedded(chroma_dir)
    b = ChromaStore.embedded(chroma_dir)
    a.upsert([_rec("small", dim=4)])
    assert b.current_dim() == 4

    a.recreate()
    b.upsert([_rec("big", dim=8)])
    assert b.current_dim() == 8


def test_recreate_propagates_non_not_found_failures(chroma_dir):
    """Auth/connection failures must not be swallowed by the absent-collection
    tolerance in `recreate()`."""
    store = ChromaStore.embedded(chroma_dir)

    class Boom(Exception):
        pass

    def explode(_name):
        raise Boom("connection refused")

    store._client.delete_collection = explode
    with pytest.raises(Boom):
        store.recreate()


def test_op_propagates_non_not_found_failures(chroma_dir):
    """A non-not-found error is raised as-is, with no retry."""
    store = ChromaStore.embedded(chroma_dir)
    calls = []

    class Boom(Exception):
        pass

    def explode(**_kw):
        calls.append(1)
        raise Boom("nope")

    store._col.upsert = explode
    with pytest.raises(Boom):
        store.upsert([_rec("x")])
    assert len(calls) == 1  # no retry


def test_retry_happens_only_once(chroma_dir):
    """If the refreshed handle still reports not-found, the error surfaces."""
    store = ChromaStore.embedded(chroma_dir)
    err = chromadb.errors.NotFoundError("Collection [x] does not exist")
    calls = []

    def always_missing(_col):
        calls.append(1)
        raise err

    with pytest.raises(chromadb.errors.NotFoundError):
        store._run(always_missing)
    assert len(calls) == 2


def test_missing_collection_predicate():
    assert _is_missing_collection(
        chromadb.errors.NotFoundError("Collection [x] does not exist"))
    assert _is_missing_collection(ValueError("Collection foo does not exist."))
    assert not _is_missing_collection(ValueError("bad dimension"))
    assert not _is_missing_collection(
        chromadb.errors.InvalidArgumentError("bad where clause"))
    assert not _is_missing_collection(RuntimeError("connection refused"))
