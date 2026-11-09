"""The derived-index record cache (crib.recordcache): TOMLs stay the truth; the
cache only saves re-reading and re-parsing files whose (mtime, size) didn't change,
and seeds a fresh process from the store's DuckDB copy."""

from __future__ import annotations

import os
import socket
import time

import pytest

from crib.recordcache import RecordCache


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CountingParse:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self, text: str) -> dict:
        self.n += 1
        k, _, v = text.partition("=")
        return {"key": k.strip(), "value": v.strip(), "nested": {"list": [1, 2]}}


def _write(d, name, text):
    p = d / name
    p.write_text(text)
    return p


def _bump(p):
    """Change a file AND its mtime, so the stamp differs whatever the fs resolution."""
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))


@pytest.fixture
def tier(tmp_path):
    d = tmp_path / "symbol_index"
    d.mkdir()
    for i in range(5):
        _write(d, f"r{i}.toml", f"k{i} = v{i}")
    _write(d, ".schema", "3")  # crib metadata: no .toml suffix, not a record
    _write(d, "notes.txt", "x")  # other suffixes are not records
    return d


def test_dot_prefixed_records_are_records(tier):
    """A source under a dot dir (.config/nvim/..., .build/...) slugs to a
    dot-prefixed filename. It's a real record (1,971 of them on George's store)."""
    _write(tier, ".config-nvim-init.lua-setup-1234.toml", "kd = vd")
    names = [n for n, _, _ in RecordCache().load(tier, CountingParse())]
    assert ".config-nvim-init.lua-setup-1234.toml" in names and ".schema" not in names


def test_parses_everything_once_then_only_what_changed(tier):
    rc, parse = RecordCache(), CountingParse()
    first = rc.load(tier, parse)
    assert [n for n, _, _ in first] == [f"r{i}.toml" for i in range(5)]  # not .schema
    assert parse.n == 5
    rc.load(tier, parse)
    assert parse.n == 5  # nothing changed: nothing re-parsed
    p = _write(tier, "r2.toml", "k2 = edited")
    _bump(p)
    out = {n: r for n, _, r in rc.load(tier, parse)}
    assert parse.n == 6 and out["r2.toml"]["value"] == "edited"


def test_added_and_removed_files_are_seen(tier):
    rc, parse = RecordCache(), CountingParse()
    rc.load(tier, parse)
    (tier / "r0.toml").unlink()
    _write(tier, "r9.toml", "k9 = v9")
    names = [n for n, _, _ in rc.load(tier, parse)]
    assert "r0.toml" not in names and "r9.toml" in names
    assert parse.n == 6  # only the new file


def test_returned_records_are_private_copies(tier):
    rc, parse = RecordCache(), CountingParse()
    rec = rc.load(tier, parse)[0][2]
    rec["value"] = "mutated by a caller"
    rec["nested"]["list"].append(99)
    again = rc.load(tier, parse)[0][2]
    assert again["value"] == "v0" and again["nested"]["list"] == [1, 2]


def test_missing_dir_is_empty(tmp_path):
    assert RecordCache().load(tmp_path / "absent", CountingParse()) == []


# ── persistence: a fresh process seeds from the store ───────────────────────


@pytest.fixture(scope="module")
def quack_server(tmp_path_factory):
    from crib.store_quack import spawn_server

    root = tmp_path_factory.mktemp("rc-quack")
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
        opened = []

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


def test_a_fresh_process_seeds_from_the_store(tier, store_factory):
    parse = CountingParse()
    RecordCache(store_factory()).load(tier, parse)
    assert parse.n == 5
    fresh_parse = CountingParse()
    out = RecordCache(store_factory()).load(tier, fresh_parse)  # "next daemon start"
    assert fresh_parse.n == 0  # every record came from DuckDB
    assert [r["value"] for _, _, r in out] == [f"v{i}" for i in range(5)]


def test_a_file_changed_while_down_is_re_parsed(tier, store_factory):
    RecordCache(store_factory()).load(tier, CountingParse())
    p = _write(tier, "r3.toml", "k3 = changed while crib was down")
    _bump(p)
    (tier / "r4.toml").unlink()
    parse = CountingParse()
    out = {n: r for n, _, r in RecordCache(store_factory()).load(tier, parse)}
    assert parse.n == 1 and out["r3.toml"]["value"] == "changed while crib was down"
    assert "r4.toml" not in out


def test_hostile_record_text_round_trips(tmp_path, store_factory):
    d = tmp_path / "hostile"
    d.mkdir()
    nasty = "'); DROP TABLE crib_record_cache; -- naïve 🦆 \\ \" ?"
    _write(d, "x.toml", f"k = {nasty}")
    RecordCache(store_factory()).load(d, CountingParse())
    out = RecordCache(store_factory()).load(d, CountingParse())
    assert out[0][2]["value"] == nasty


# ── SymbolIndex goes through it, unchanged in behaviour ──────────────────────


def test_symbol_index_all_matches_a_direct_parse(tmp_path):
    from crib import recordcache
    from crib.codeindex import SymbolIndex, _parse

    si = SymbolIndex(tmp_path / "proj")
    for i in range(6):
        si.write(
            {
                "fqname": f"m.f{i}",
                "name": f"f{i}",
                "kind": "function",
                "lang": "python",
                "module": "m",
                "parent": "",
                "content_hash": f"h{i}",
                "file": "m.py",
                "line": i,
                "signature": "",
                "description": f"does {i}",
                "container": [],
                "calls": [],
                "called_by": [],
                "references": [],
                "name_terms": [f"f{i}"],
            }
        )
    recordcache.attach(None)  # memo-only, fresh
    direct = sorted(
        (
            e
            for e in (_parse(p.read_text()) for p in si.root.glob("*.toml"))
            if e.get("file") and e.get("name")
        ),
        key=lambda e: e["name"],
    )
    assert sorted(si.all(), key=lambda e: e["name"]) == direct
    e = si.all()[0]
    e["description"] = "edited"
    time.sleep(0.01)
    si.write(e)
    assert any(x["description"] == "edited" for x in si.all())  # a write is seen
