"""Code symbols mirrored into the store as items, with their edges.

The symbol facet's refresh (CodeStore.refresh) maps the record files that
changed since the store last saw them into `symbol` items (and calls /
references / member_of edges); a vanished record's item goes."""

from __future__ import annotations

from typing import Any

import pytest

from crib.app import Crib
from crib.codeindex import SymbolIndex
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore
from crib.symbols import fqn, scope_of, symbol_ref


@pytest.fixture(params=["memory", "duckdb"])
def crib(request, tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    paths = Paths.resolve().ensure()
    if request.param == "duckdb":
        from crib.store_duckdb import DuckDBStore

        store = DuckDBStore.embedded(paths.duckdb_file)
        request.addfinalizer(store.close)
    else:
        store = InMemoryStore()
    return Crib(paths, Config(), store)


def _sym(crib, name, file, container=(), **over) -> dict[str, Any]:
    e: dict[str, Any] = {
        "name": name,
        "kind": "function",
        "lang": "python",
        "file": file,
        "line": 1,
        "content_hash": f"h_{name}",
        "signature": f"def {name}():",
        "description": f"does {name}",
        "container": list(container),
        "calls": [],
        "called_by": [],
        "references": [],
        "name_terms": [name],
        **over,
    }
    e["scope"] = scope_of(e["lang"], e["file"], e["container"])
    e["symbol_ref"] = symbol_ref(e["file"], e["container"], e["name"], e["lang"])
    e["fqn"] = fqn(e["scope"], e["name"], e["lang"], e["file"], e["container"])
    SymbolIndex(crib.paths.project_dir("p")).write(e)
    return e


def test_symbols_become_items_with_their_edges(crib):
    cls = _sym(crib, "Widget", "app.py", kind="class")
    meth = _sym(
        crib,
        "spin",
        "app.py",
        container=["Widget"],
        calls=["helper [util.py]"],
        references=["helper [util.py]"],
    )
    _sym(crib, "helper", "util.py", called_by=["spin [app.py]"])
    crib.code.refresh("p")
    items = crib.store.items({"project": "p", "kind": "symbol"})
    assert set(items) == {
        f"symbol:p:{e['symbol_ref']}"
        for e in (cls, meth, {"symbol_ref": "util.py#helper"})
    }
    m = items[f"symbol:p:{meth['symbol_ref']}"]
    assert m.facet["fqn"] == meth["fqn"] and m.facet["description"] == "does spin"
    assert m.relpath == "app.py" and m.title == meth["fqn"]
    edges = {(dst, k) for _s, dst, k, _h in crib.store.edges({"src": m.id})}
    assert edges == {
        ("helper [util.py]", "calls"),
        ("helper [util.py]", "references"),
        (f"symbol:p:{cls['symbol_ref']}", "member_of"),
    }


def test_a_changed_or_removed_record_is_mirrored_on_the_next_refresh(crib):
    _sym(crib, "keep", "a.py")
    gone = _sym(crib, "gone", "a.py")
    crib.code.refresh("p")
    _sym(crib, "keep", "a.py", description="does keep, revised")
    SymbolIndex(crib.paths.project_dir("p")).delete(gone["symbol_ref"])
    crib.code.refresh("p")
    items = crib.store.items({"project": "p", "kind": "symbol"})
    assert set(items) == {"symbol:p:a.py#keep"}
    assert items["symbol:p:a.py#keep"].facet["description"] == "does keep, revised"


def test_store_postings_score_exactly_like_an_in_memory_bm25(crib):
    """C2a: the store holds the postings, Python scores them with the SAME
    formula; the scores must match a BM25 built in memory over the same fields
    (what the resident index was) bit for bit."""
    from crib.codestore import symbol_key
    from crib.retrieve import (
        BM25,
        STOPWORDS,
        _as_tf,
        _subtokens,
        bm25_from_postings,
        code_candidate,
        code_field_terms,
        coverage_from_postings,
        tokenize,
    )

    _sym(
        crib, "parse_config", "cfg.py", keywords=["read the config file", "parse toml"]
    )
    _sym(crib, "ConfigLoader", "cfg.py", kind="class", keywords=["load config"])
    _sym(crib, "render_page", "web.py", keywords=["render html page", "template"])
    _sym(crib, "helper", "util.py", description="", keywords=[], name_terms=[])
    lk = [e for e in SymbolIndex(crib.paths.project_dir("p")).all() if code_candidate(e)]
    lk_ids = [symbol_key(e) for e in lk]
    bm25 = BM25([_as_tf(code_field_terms(e)) for e in lk])
    crib.code.refresh("p")
    for query in (
        "parse config file",
        "render template page",
        "loader",
        "nothing here",
    ):
        qt = tokenize(query) + _subtokens(query)
        resident = {lk_ids[i]: s for i, s in enumerate(bm25.scores(qt)) if s > 0}
        stats = crib.store.postings({"project": "p", "kind": "symbol"}, qt)
        stored = {
            i.removeprefix("symbol:p:"): s
            for i, s in bm25_from_postings(stats, qt).items()
        }
        assert stored == resident, query  # exact, not approx
        assert stats.n == len(lk)
        Q = {t for t in set(qt) if len(t) > 1 and t not in STOPWORDS}
        cov = coverage_from_postings(stats, Q)
        rcov = dict(zip(lk_ids, bm25.coverage(Q)))
        for i, c in cov.items():
            assert c == rcov[i.removeprefix("symbol:p:")]


def test_concurrent_first_queries_all_see_every_symbol(crib):
    """Several queries' first touch at once: one maps the records, the others
    wait for it and find the stamps current (no conflicting transactions, no
    query reading a half-mapped project)."""
    from concurrent.futures import ThreadPoolExecutor

    names = [f"sym{i}" for i in range(40)]
    for n in names:
        _sym(crib, n, "m.py")

    def one(n: str) -> int:
        return len(crib.code_xref(n, project="p"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(one, names)) == [1] * len(names)


def test_the_same_symbol_ref_in_two_projects_is_two_items(crib):
    """Vendored files give several projects one symbol_ref; each project keeps
    its own item, whichever refreshes last."""
    for proj in ("p", "q"):
        e = _sym(crib, "BAND_Y", "vendored.mjs", kind="constant")
        if proj == "q":
            SymbolIndex(crib.paths.project_dir("q")).write(e)
    crib.code.refresh("p")
    crib.code.refresh("q")
    assert set(crib.store.items({"kind": "symbol"})) == {
        "symbol:p:vendored.mjs#BAND_Y", "symbol:q:vendored.mjs#BAND_Y"}
    assert crib.code_xref("BAND_Y", project="p") and crib.code_xref("BAND_Y", project="q")
