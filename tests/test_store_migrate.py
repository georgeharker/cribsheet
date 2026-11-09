"""Store migration (crib.store_migrate): copy the index between backends without
re-embedding, idempotently, and prove the copy matches.

Sources: chroma (the real migration), in-memory and json. Targets: DuckDB
embedded, plus in-memory as a cheap control. The Quack target is covered by the
same engine through the Store contract (QuackStore is a DuckDBStore)."""

from __future__ import annotations

import math

import pytest

from crib.errors import CribUserError
from crib.store import InMemoryStore, JsonStore, Record
from crib.store_migrate import migrate, plan_migration, verify


def unit(*xs: float) -> list[float]:
    n = math.sqrt(sum(x * x for x in xs))
    return [x / n for x in xs]


def corpus(n: int = 60, dim: int = 8) -> list[Record]:
    """Chunks shaped like crib's: several projects and pillar stores, real
    metadata keys, content hashes."""
    out = []
    for i in range(n):
        v = unit(*[math.sin(i * (j + 1) * 0.7) + 1.1 for j in range(dim)])
        meta = {
            "project": ("cribsheet", "toucan", "un-bien")[i % 3],
            "store": ("notes", "design", "plans")[(i // 3) % 3],
            "relpath": f"note-{i // 4}.md",
            "title": f'Note {i // 4} — it\'s "quoted"',
            "content_hash": f"h{i}",
            "occurrence": i % 2,
            "file_mtime": 1731097200.0 + i / 7,
        }
        out.append(Record(f"chunk-{i:03d}", v, f"body {i}", meta))
    return out


def chroma_source(tmp_path):
    from crib.store import ChromaStore

    return ChromaStore.embedded(str(tmp_path / "chroma"))


def duckdb_target(tmp_path):
    from crib.store_duckdb import DuckDBStore

    return DuckDBStore.embedded(tmp_path / "crib.duckdb")


SOURCES = {
    "chroma": chroma_source,
    "memory": lambda tmp_path: InMemoryStore(),
    "json": lambda tmp_path: JsonStore(tmp_path / "store.json"),
}
TARGETS = {
    "duckdb": duckdb_target,
    "memory": lambda tmp_path: InMemoryStore(),
}


@pytest.fixture(params=list(SOURCES))
def source(request, tmp_path):
    s = SOURCES[request.param](tmp_path)
    s.upsert(corpus())
    return s


@pytest.fixture(params=list(TARGETS))
def target(request, tmp_path):
    t = TARGETS[request.param](tmp_path)
    if hasattr(t, "close"):
        request.addfinalizer(t.close)
    return t


def test_plan_is_read_only_and_counts(source, target):
    before = source.get_meta({})
    plan = plan_migration(source, target)
    assert (plan.source_total, plan.target_total, plan.to_copy) == (60, 0, 60)
    assert plan.dim == 8
    assert sum(plan.by_partition.values()) == 60 and len(plan.by_partition) == 9
    assert target.get_meta({}) == {}  # nothing written
    assert source.get_meta({}) == before


def test_migrate_copies_everything_and_verifies(source, target):
    report = migrate(source, target, batch=7)  # several batches
    assert report["ok"], report["problems"]
    assert (report["copied"], report["removed"]) == (60, 0)
    assert target.get_docs({}) == source.get_docs({})
    c = report["checks"]
    assert c["worst_vector_cosine"] > 1 - 1e-6
    assert (c["self_retrieval_misses"], c["score_dominance_failures"]) == (0, 0)


def test_rerun_is_a_no_op(source, target):
    migrate(source, target)
    again = migrate(source, target)
    assert again["ok"] and (again["copied"], again["removed"]) == (0, 0)


def test_interrupted_migration_resumes_to_the_same_result(source, target):
    """Kill a migration partway (simulated: only the first batches land), then
    rerun: only the remainder is copied and the result is identical."""
    first = next(source.iter_records(25))
    target.upsert(first)  # what an interrupted run left behind
    report = migrate(source, target)
    assert report["ok"], report["problems"]
    assert report["copied"] == 60 - len(first)
    assert target.get_docs({}) == source.get_docs({})


def test_changed_and_deleted_records_bring_the_target_back_in_line(source, target):
    migrate(source, target)
    recs = {r.id: r for b in source.iter_records() for r in b}
    edited = recs["chunk-005"]
    source.upsert(
        [
            Record(
                edited.id,
                unit(*[1.0] * 8),
                "edited body",
                {**edited.metadata, "content_hash": "h5-edited"},
            )
        ]
    )
    source.delete(["chunk-010", "chunk-011"])
    plan = plan_migration(source, target)
    assert (plan.to_copy, plan.to_remove) == (1, 2)
    report = migrate(source, target)
    assert report["ok"], report["problems"]
    assert (report["copied"], report["removed"]) == (1, 2)
    assert (
        target.get_docs({"project": edited.metadata["project"]})[edited.id][0]
        == "edited body"
    )


def test_source_is_never_written(source, target):
    before = source.get_docs({})
    migrate(source, target)
    assert source.get_docs({}) == before


def test_dimension_mismatch_is_refused_and_points_at_a_rebuild(source, target):
    target.upsert([Record("other", unit(1, 0), "x", {"project": "p"})])  # 2-d
    with pytest.raises(CribUserError, match="reconcile"):
        plan_migration(source, target)
    with pytest.raises(CribUserError, match="dimension|\\d+-d"):
        migrate(source, target)


def test_empty_source_migrates_to_an_empty_target(tmp_path, target):
    report = migrate(InMemoryStore(), target)
    assert report["ok"] and report["copied"] == 0 and target.get_meta({}) == {}


def test_verify_catches_a_tampered_target(source, target):
    migrate(source, target)
    recs = {r.id: r for b in source.iter_records() for r in b}
    r = recs["chunk-020"]
    target.set_meta({r.id: {**r.metadata, "title": "tampered"}})
    target.delete(["chunk-021"])
    result = verify(source, target)
    assert not result["ok"]
    text = " ".join(result["problems"])
    assert "missing from target" in text and "metadata differs" in text


def test_verify_catches_vectors_scrambled_in_the_copy(source, target):
    """A copy whose vectors went to the wrong records (same ids, hashes and
    metadata) passes every bookkeeping check, so the search checks must
    catch it."""
    migrate(source, target)
    recs = sorted((r for b in source.iter_records() for r in b), key=lambda r: r.id)
    shifted = [Record(r.id, recs[(n + 7) % len(recs)].embedding, r.document, r.metadata)
               for n, r in enumerate(recs)]
    target.upsert(shifted)
    result = verify(source, target)
    assert not result["ok"]
    assert result["checks"]["self_retrieval_misses"] > 0


def test_hostile_metadata_survives_migration(tmp_path, target):
    src = InMemoryStore()
    nasty = "'); DROP TABLE crib_chunks; -- naïve 🦆 \\ \" $1 ?"
    src.upsert(
        [
            Record(
                f"id {nasty}",
                unit(1, 2, 3),
                f"doc {nasty}",
                {
                    "project": nasty,
                    "store": "notes",
                    "title": nasty,
                    "content_hash": "h",
                },
            )
        ]
    )
    report = migrate(src, target)
    assert report["ok"], report["problems"]
    assert target.get_docs({"project": nasty}) == src.get_docs({"project": nasty})


def test_chroma_to_duckdb_at_scale(tmp_path):
    """The real direction at a realistic size: 3,000 chunks of 384-d."""
    from crib.store import ChromaStore
    from crib.store_duckdb import DuckDBStore

    src = ChromaStore.embedded(str(tmp_path / "chroma"))
    recs = []
    for i in range(3000):
        v = unit(*[math.sin(i * 0.013 * (j + 1)) + 1.05 for j in range(384)])
        recs.append(
            Record(
                f"c{i:05d}",
                v,
                f"body {i}",
                {
                    "project": f"p{i % 7}",
                    "store": ("notes", "design")[i % 2],
                    "content_hash": f"h{i}",
                    "relpath": f"n{i // 5}.md",
                },
            )
        )
    for lo in range(0, 3000, 500):
        src.upsert(recs[lo : lo + 500])
    dst = DuckDBStore.embedded(tmp_path / "crib.duckdb")
    try:
        report = migrate(src, dst)
        assert report["ok"], report["problems"]
        assert report["copied"] == 3000
        assert report["checks"]["vectors_compared"] == 200
    finally:
        dst.close()
