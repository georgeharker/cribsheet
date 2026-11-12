"""The numpy cosine paths == the pure-Python dot products they replaced.

Both resident vector structures score a query by one matmul against a float32
matrix instead of a per-row Python dot product: `_ResidentCode.dense_matrix` for
code symbols (`crib/codequery.py::_lookup_one`) and `SummaryVectorCache._matrix`
for note alias vectors. At 10-16k symbols x 384 dims the Python loop was the whole
cost of a `code_lookup` (~90-140ms, vs ~0.2ms for `M @ q`).

The matmul is only allowed to be faster, never different, so every test here pins
it against a straightforward reference implementation written the old way --- and
the sets include a symbol with NO vector (the `-1.0` floor) and exact ties (which
must still break by ascending index).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from crib.codequery import _topk
from crib.codestore import _ResidentCode
from crib.embed import HashEmbedder

# The vectors every embedder hands back are L2-normalised, which is what makes a
# plain dot product the cosine. The reference says so by construction.
EMB = HashEmbedder(dim=48)


def _ref_dot(qv: list[float], v: list[float] | None) -> float:
    """One cosine, the way `_lookup_one` used to compute it. A symbol with no
    description has no vector and scores below any real cosine."""
    return sum(a * b for a, b in zip(qv, v)) if v else -1.0


def _entry(name: str, description: str) -> dict[str, Any]:
    return {
        "fqname": f"pkg.{name}",
        "name": name,
        "kind": "function",
        "lang": "python",
        "module": "pkg",
        "parent": "",
        "file": "pkg/mod.py",
        "line": 1,
        "signature": f"def {name}():",
        "description": description,
        "content_hash": f"h_{name}",
        "container": [],
        "calls": [],
        "called_by": [],
        "name_terms": [name],
        "keywords": [],
    }


# A synthetic index: two distinct descriptions, one EXACT DUPLICATE of the first
# (identical vector -> exactly equal cosine, so tie-breaking is observable), and
# one symbol with no description at all (no row in the matrix).
ENTRIES = [
    _entry("alpha", "computes the alpha metric"),
    _entry("beta", "renders the beta view"),
    _entry("gamma", "computes the alpha metric"),
    _entry("delta", ""),
]


@pytest.fixture()
def resident() -> _ResidentCode:
    return _ResidentCode("tok", [dict(e) for e in ENTRIES], {})


def _scores(rc: _ResidentCode, qv: list[float]) -> Any:
    """The scatter `_lookup_one` does: matmul over the rows that HAVE vectors,
    every other symbol left at the -1.0 floor."""
    M, rows = rc.dense_matrix(EMB)
    dense = np.full(len(rc.lk), -1.0, dtype=np.float32)
    if rows.size:
        dense[rows] = M @ np.asarray(qv, dtype=np.float32)
    return dense


def test_dense_matrix_covers_exactly_the_symbols_with_a_vector(resident):
    M, rows = resident.dense_matrix(EMB)
    dense_v = resident.dense(EMB)
    assert rows.tolist() == [i for i, v in enumerate(dense_v) if v]
    assert M.shape == (3, EMB.dim)  # `delta` has no description, so no row
    assert M.dtype == np.float32
    # the rows ARE the vectors, in `lk` order
    for j, i in enumerate(rows.tolist()):
        assert M[j].tolist() == pytest.approx(dense_v[i], abs=1e-6)


def test_dense_matrix_is_built_once(resident):
    first = resident.dense_matrix(EMB)
    assert resident.dense_matrix(EMB) is first  # same tuple, same arrays


def test_matmul_scores_match_the_python_dot_products(resident):
    dense_v = resident.dense(EMB)
    for query in ("alpha metric", "renders the beta view", "nothing in common here"):
        qv = EMB.embed_query([query])[0]
        want = [_ref_dot(qv, v) for v in dense_v]
        assert _scores(resident, qv).tolist() == pytest.approx(want, abs=1e-6)


def test_ranking_and_tie_breaking_match_the_python_sort(resident):
    """Same order, including the exact tie between the two identical descriptions
    and the no-vector symbol pinned last."""
    dense_v = resident.dense(EMB)
    for query in ("alpha metric", "beta", "unrelated"):
        qv = EMB.embed_query([query])[0]
        ref = [_ref_dot(qv, v) for v in dense_v]
        want = sorted(range(len(ref)), key=lambda i: ref[i], reverse=True)
        got = _topk(_scores(resident, qv), len(ref))
        assert got == want
    # `alpha` and `gamma` share a description, so they tie exactly -- the lower
    # index must come first, in both implementations, for the pool to be stable.
    qv = EMB.embed_query(["computes the alpha metric"])[0]
    dense = _scores(resident, qv)
    assert float(dense[0]) == float(dense[2])
    assert _topk(dense, 2) == [0, 2]
    assert _topk(dense, 4)[-1] == 3  # the symbol with no vector, last


def test_an_index_with_no_vectors_at_all_has_an_empty_matrix():
    rc = _ResidentCode("tok", [_entry("solo", "")], {})
    M, rows = rc.dense_matrix(EMB)
    assert rows.size == 0 and M.size == 0  # caller falls back to all-zero scores


@pytest.mark.parametrize("k", [1, 2, 3, 7, 10, 50])
def test_topk_equals_the_stable_python_sort(k):
    """`_topk` over values with heavy ties and a k that straddles the tie boundary
    --- argpartition alone would be free to return any of the tied indices."""
    rng = np.random.default_rng(7)
    vals = np.round(rng.uniform(-1.0, 1.0, size=10), 1).astype(np.float32)
    vals[3] = vals[8] = vals[0]  # force exact ties
    want = sorted(range(vals.shape[0]), key=lambda i: vals[i], reverse=True)[:k]
    assert _topk(vals, k) == want


# --- the alias-vector cache --------------------------------------------------
class _DocStore:
    """The one Store fake these cache tests need: `get_docs` canned, every other
    protocol member stubbed (never called — the cache only reads docs)."""

    def __init__(self, docs: dict[str, tuple[str, dict[str, Any]]]) -> None:
        self._docs = docs

    def get_docs(self, where: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
        return self._docs

    def upsert(self, records: Any) -> None:
        raise AssertionError("not called in these tests")

    def delete(self, ids: Any) -> None:
        raise AssertionError("not called in these tests")

    def set_meta(self, updates: Any) -> None:
        raise AssertionError("not called in these tests")

    def apply(self, records: Any, deletes: Any, meta: Any, items: Any = None,
              item_deletes: Any = None) -> None:
        raise AssertionError("not called in these tests")

    def items(self, where: Any) -> dict[str, Any]:
        return {}

    def edges(self, where: Any) -> list[Any]:
        return []

    def item_hashes(self, where: Any) -> dict[str, str]:
        return {}

    def postings(self, where: Any, terms: Any) -> Any:
        raise AssertionError("not called in these tests")

    def set_body_vectors(self, vectors: Any) -> None:
        raise AssertionError("not called in these tests")

    def lacking_body_vectors(self, where: Any, candidates_only: Any = False) -> list[str]:
        return []

    def body_vector_top(self, where: Any, vec: Any, n: Any) -> list[Any]:
        return []

    def body_vectors(self, ids: Any) -> dict[str, Any]:
        return {}

    def get_meta(self, where: Any) -> dict[str, dict[str, Any]]:
        return {i: m for i, (_d, m) in self.get_docs(where).items()}

    def count(self, where: Any, relpath_prefix: str = "") -> int:
        return len(self.get_meta(where))

    def projects(self) -> set[str]:
        return set()

    def query(self, embedding: Any, k: Any, where: Any = None) -> list:
        return []

    def current_dim(self) -> int | None:
        return None

    def score_ids(self, embedding: Any, ids: Any) -> dict[str, float]:
        return {}

    def recreate(self) -> None:
        pass

    def meta_get(self, key: str) -> str | None:
        return None

    def meta_set(self, key: str, value: str) -> None:
        raise AssertionError("not called in these tests")


def _alias_cache():
    from crib.retrieve import SummaryVectorCache

    docs = {
        "c1": ("body one", {"project": "p", "section_hash": "s1"}),
        "c2": ("body two", {"project": "p", "section_hash": "s2"}),
        "c3": ("body three", {"project": "p", "section_hash": "s3"}),
    }

    def summaries(_project: str, sh: str, labels: tuple[str, ...]) -> list[str]:
        # s1 gets TWO aliases per label (the max matters), s3 gets none at all --
        # a section with no vector must simply not appear.
        if sh == "s1":
            return [f"kubernetes orchestration {labels[0]}", "feline domestic animals"]
        if sh == "s2":
            return [f"shipping container scheduling {labels[0]}"]
        return []

    return SummaryVectorCache(_DocStore(docs), EMB, summaries)


def _ref_best(vecs, qv):
    """`best_cosines`' reduction, written the old way: strict `>`, so the first
    row of a section wins a tie and sections come out in first-appearance order."""
    best: dict[str, tuple[float, str]] = {}
    for sh, label, v in vecs:
        c = _ref_dot(qv, v)
        if c > best.get(sh, (-2.0, ""))[0]:
            best[sh] = (c, label)
    return best


@pytest.mark.parametrize(
    "query",
    ["kubernetes orchestration summary", "shipping container", "quite unrelated"],
)
def test_best_cosines_matches_the_python_reduction(query):
    cache = _alias_cache()
    labels = ("summary", "keywords")
    qv = EMB.embed_query([query])[0]
    _reps, vecs = cache.get("p", labels)
    want = _ref_best(vecs, qv)
    got = cache.best_cosines("p", labels, qv)

    # keyed by the section's REPRESENTATIVE chunk id, in the same order
    assert list(got) == [f"c{sh[1:]}" for sh in want]
    for cid, (cos, label) in got.items():
        wcos, wlabel = want[f"s{cid[1:]}"]
        assert cos == pytest.approx(wcos, abs=1e-6)
        assert label == wlabel  # the WINNING alias's label, not just any
    assert "c3" not in got  # a section with no alias has no vector and no entry


def test_alias_matrix_is_built_once_and_dropped_on_invalidate():
    cache = _alias_cache()
    labels = ("summary",)
    key = ("p", "notes", labels)
    qv = EMB.embed_query(["kubernetes orchestration summary"])[0]
    cache.best_cosines("p", labels, qv)
    built = cache._mats[key]
    assert cache.best_cosines("p", labels, qv)  # second query reuses the matrix
    assert cache._mats[key] is built
    cache.invalidate("p")  # a write drops the vectors AND the matrix over them
    assert key not in cache._mats and key not in cache._entries
    cache.best_cosines("p", labels, qv)
    assert cache._mats[key] is not built  # rebuilt from the fresh vectors
