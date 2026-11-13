"""The enrichments facet (crib.enrichments): keyword_index / summary_index TOMLs
mapped into the store. What the store answers must equal what the files say;
alias vectors persist, so a restart embeds nothing again."""

from __future__ import annotations

import os

import pytest

from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.section_index import SectionIndex


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    paths = Paths.resolve().ensure()
    from crib.store_duckdb import DuckDBStore

    store = DuckDBStore.embedded(paths.duckdb_file)
    yield paths, store
    store.close()


def _seed(paths):
    pd = paths.project_dir("p")
    kw, sm = SectionIndex(pd, "keyword_index"), SectionIndex(pd, "summary_index")
    kw.write("keywords", "s1", ["alpha term", "beta"])
    kw.write("keywords", "s2", ["gamma"])
    kw.write("extra", "s1", ["delta"])
    sm.write("summary", "s1", ["first alias", "second alias"])
    sm.write("summary", "s2", ["third alias"])
    sm.write("other", "s2", ["fourth alias"])
    return kw, sm


def _counting(crib, monkeypatch) -> list[str]:
    seen: list[str] = []
    inner = crib.enrichments.embedder.embed

    def embed(texts):
        seen.extend(texts)
        return inner(texts)

    monkeypatch.setattr(crib.enrichments.embedder, "embed", embed)
    return seen


def test_the_store_answers_what_the_files_say(env):
    paths, store = env
    kw, sm = _seed(paths)
    crib = Crib(paths, Config(), store)
    for labels in (("keywords",), ("keywords", "extra"), ("extra", "keywords")):
        got = crib.enrichments.keyword_map("p", labels)
        assert got == {
            sh: kw.terms_for(sh, list(labels))
            for sh in ("s1", "s2")
            if kw.terms_for(sh, list(labels))
        }
    al = crib.enrichments.alias_vectors("p", ("summary", "other"))
    assert {k: len(v) for k, v in al.items()} == {
        ("s1", "summary"): 2,
        ("s2", "summary"): 1,
        ("s2", "other"): 1,
    }
    want = crib.embedder.embed(["first alias", "second alias"])
    for got_v, want_v in zip(al[("s1", "summary")], want):
        assert got_v == pytest.approx(want_v, abs=1e-6)  # line order kept


def test_an_edit_an_addition_and_a_prune_are_picked_up(env):
    paths, store = env
    kw, sm = _seed(paths)
    crib = Crib(paths, Config(), store)
    crib.enrichments.keyword_map("p", ("keywords",))
    p = kw.write("keywords", "s1", ["rewritten"])
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    kw.write("keywords", "s3", ["new section"])
    kw.prune("keywords", {"s1", "s3"})  # s2 is gone
    assert crib.enrichments.keyword_map("p", ("keywords",)) == {
        "s1": ["rewritten"],
        "s3": ["new section"],
    }


def test_alias_vectors_persist_so_a_restart_embeds_nothing(env, monkeypatch):
    paths, store = env
    _seed(paths)
    first = Crib(paths, Config(), store)
    seen = _counting(first, monkeypatch)
    first.enrichments.alias_vectors("p", ("summary",))
    assert sorted(seen) == [
        "first alias",
        "fourth alias",
        "second alias",
        "third alias",
    ]  # each once
    again = Crib(paths, Config(), store)  # "a restart"
    seen2 = _counting(again, monkeypatch)
    assert again.enrichments.alias_vectors("p", ("summary",))
    assert seen2 == []


def test_the_watcher_decodes_nested_enrichment_files(tmp_path):
    from crib.watch import decode_record

    proj = tmp_path / "projects"
    assert decode_record(
        proj, str(proj / "p" / "keyword_index" / "kw" / "s1.toml")
    ) == ("p", "keywords", "kw/s1.toml")
    assert decode_record(
        proj, str(proj / "p" / "summary_index" / "sm" / "s1.toml")
    ) == ("p", "summary", "sm/s1.toml")
    assert decode_record(proj, str(proj / "p" / "keyword_index" / "s1.toml")) is None
    assert (
        decode_record(proj, str(proj / "p" / "symbol_index" / "x" / "a.toml")) is None
    )
