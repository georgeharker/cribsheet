"""Code symbols mirrored into the store as items, with their edges.

The resident code index re-parses symbol_index whenever it changes; the same
pass brings the store's `symbol` items (and calls / references / member_of
edges) into line with the records."""

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
    crib._resident_code("p")
    items = crib.store.items({"project": "p", "kind": "symbol"})
    assert set(items) == {
        f"symbol:{e['symbol_ref']}"
        for e in (cls, meth, {"symbol_ref": "util.py#helper"})
    }
    m = items[f"symbol:{meth['symbol_ref']}"]
    assert m.facet["fqn"] == meth["fqn"] and m.facet["description"] == "does spin"
    assert m.relpath == "app.py" and m.title == meth["fqn"]
    edges = {(dst, k) for _s, dst, k, _h in crib.store.edges({"src": m.id})}
    assert edges == {
        ("helper [util.py]", "calls"),
        ("helper [util.py]", "references"),
        (f"symbol:{cls['symbol_ref']}", "member_of"),
    }


def test_a_changed_or_removed_record_is_mirrored_on_the_next_reload(crib):
    _sym(crib, "keep", "a.py")
    gone = _sym(crib, "gone", "a.py")
    crib._resident_code("p")
    _sym(crib, "keep", "a.py", description="does keep, revised")
    SymbolIndex(crib.paths.project_dir("p")).delete(gone["symbol_ref"])
    crib.code.reload("p", crib.code.tok("p"), None)
    items = crib.store.items({"project": "p", "kind": "symbol"})
    assert set(items) == {"symbol:a.py#keep"}
    assert items["symbol:a.py#keep"].facet["description"] == "does keep, revised"


def test_store_postings_score_exactly_like_the_resident_bm25(crib):
    """C2a: the store holds the postings, Python scores them with the SAME
    formula; the scores must match the resident index bit for bit."""
    from crib.retrieve import (
        STOPWORDS,
        _subtokens,
        bm25_from_postings,
        coverage_from_postings,
        tokenize,
    )

    _sym(
        crib, "parse_config", "cfg.py", keywords=["read the config file", "parse toml"]
    )
    _sym(crib, "ConfigLoader", "cfg.py", kind="class", keywords=["load config"])
    _sym(crib, "render_page", "web.py", keywords=["render html page", "template"])
    _sym(crib, "helper", "util.py", description="", keywords=[], name_terms=[])
    rc = crib._resident_code("p")
    for query in (
        "parse config file",
        "render template page",
        "loader",
        "nothing here",
    ):
        qt = tokenize(query) + _subtokens(query)
        resident = {rc.lk_ids[i]: s for i, s in enumerate(rc.bm25.scores(qt)) if s > 0}
        stats = crib.store.postings({"project": "p", "kind": "symbol"}, qt)
        stored = {
            i.removeprefix("symbol:"): s
            for i, s in bm25_from_postings(stats, qt).items()
        }
        assert stored == resident, query  # exact, not approx
        assert stats.n == len(rc.lk)
        Q = {t for t in set(qt) if len(t) > 1 and t not in STOPWORDS}
        cov = coverage_from_postings(stats, Q)
        rcov = dict(zip(rc.lk_ids, rc.coverage(Q)))
        for i, c in cov.items():
            assert c == rcov[i.removeprefix("symbol:")]
