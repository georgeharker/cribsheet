"""The design/plan graph is built from the store (crib.designs._nodes_from_store):
the facet refreshes its items from the files that changed, by stamp, and the
nodes come back from the items, their facet and their edges. The store must
give exactly the graph the files give."""

from __future__ import annotations

import asyncio
import dataclasses
import os

import pytest

from crib import notes
from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore


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


def run(coro):
    return asyncio.run(coro)


def _from_files(crib, proj="p"):
    out = {}
    for kind, store in crib.designs._stores.items():
        d = store.root(proj)
        if d.exists():
            for n in crib.designs._nodes_from_files(kind, d):
                out[n.id] = n
    return out


def _same(crib):
    graph = crib.designs._load_graph("p")
    files = _from_files(crib)
    assert list(graph.nodes) == list(files)
    for nid, node in graph.nodes.items():
        want = dataclasses.asdict(files[nid])
        got = dataclasses.asdict(node)
        for src in got["sources"]:
            src.pop("current", None)  # resolved by the load, not stored
        # dep_kinds: the load adds each default kind afterwards; compare explicit
        got["dep_kinds"] = {
            k: v for k, v in got["dep_kinds"].items() if k in files[nid].dep_kinds
        }
        assert got == want, nid


def test_the_store_gives_the_graph_the_files_give(crib):
    d1 = run(crib.design_add("Ground", "The base decision.", project="p"))
    d2 = run(
        crib.design_add(
            "Built on it", "Rests on the base.", deps=[d1["relpath"]], project="p"
        )
    )
    p1 = run(crib.plan_add("First step", project="p"))
    p2 = run(
        crib.plan_add(
            "Second",
            "after the first",
            deps=[p1["relpath"]],
            informed_by=[d2["relpath"]],
            project="p",
        )
    )
    run(crib.plan_park(p2["relpath"], project="p", triggers=[d1["relpath"]]))
    run(crib.design_edit(d1["relpath"], "The base decision, reworded.", project="p"))
    d3 = run(crib.design_add("Replacement", "Instead of the second.", project="p"))
    run(crib.design_supersede(d2["relpath"], d3["relpath"], project="p"))
    _same(crib)
    node = crib.designs._load_graph("p").nodes[p2["id"]]
    assert node.trigger and node.trigger[0]["ref"] == d1["id"]
    assert crib.designs._load_graph("p").nodes[d2["id"]].superseded_by == d3["id"]


def test_an_edit_behind_crib_is_seen_and_only_it_is_parsed(crib, monkeypatch):
    d1 = run(crib.design_add("Ground", "The base decision.", project="p"))
    run(crib.design_add("Other", "Unrelated.", project="p"))
    crib.designs._load_graph("p")  # stamps every file
    path = crib.designs._stores["design"].root("p") / d1["relpath"]
    note = notes.load(path)
    note.frontmatter["status"] = "proposed"
    note.body = "Edited by hand."
    notes.save_atomic(note)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    parsed: list[str] = []
    real = notes.parse

    def counting(text, p=None):
        parsed.append(str(getattr(p, "name", p)))
        return real(text, p)

    monkeypatch.setattr(notes, "parse", counting)
    node = crib.designs._load_graph("p").nodes[d1["id"]]
    assert node.status == "proposed"
    assert parsed == [d1["relpath"]]
    monkeypatch.setattr(notes, "parse", real)
    _same(crib)


def test_a_removed_file_leaves_the_graph(crib):
    d1 = run(crib.design_add("Ground", "The base decision.", project="p"))
    crib.designs._load_graph("p")
    (crib.designs._stores["design"].root("p") / d1["relpath"]).unlink()
    assert d1["id"] not in crib.designs._load_graph("p").nodes
    assert not crib.store.items({"id": d1["id"]})
