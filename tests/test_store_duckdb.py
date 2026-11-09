"""DuckDB store specifics the shared contract suite can't express: the
one-process-per-file rule, the backend switch, and how a crib instance builds and
releases the store. The Store behaviour itself is covered by
tests/test_store_contract.py, run against this backend like every other."""

from __future__ import annotations

import subprocess
import sys

import pytest

from crib import proc as spawn
from crib.errors import CribUserError
from crib.store import Record

pytest.importorskip("duckdb")

from crib.store_duckdb import DuckDBStore  # noqa: E402


def _hold_open(path) -> subprocess.Popen:
    """Another process with the store open, until its stdin closes."""
    code = (
        "import sys, duckdb; c = duckdb.connect(sys.argv[1]); "
        "print('open', flush=True); sys.stdin.read()"
    )
    proc = spawn.popen(
        [sys.executable, "-I", "-c", code, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "open"
    return proc


def test_second_process_gets_a_crib_error_naming_the_fix(tmp_path):
    path = tmp_path / "crib.duckdb"
    DuckDBStore.embedded(path).close()  # create the file
    holder = _hold_open(path)
    try:
        with pytest.raises(CribUserError) as exc:
            DuckDBStore.embedded(path)
        msg = str(exc.value)
        assert "another process" in msg
        assert "daemon" in msg and "shared mode" in msg
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(30)
    DuckDBStore.embedded(path).close()  # released: opens again


def test_close_releases_the_file_for_another_process(tmp_path):
    path = tmp_path / "crib.duckdb"
    store = DuckDBStore.embedded(path)
    store.upsert([Record("a", [1.0, 0.0], "doc", {"project": "p"})])
    store.close()
    code = (
        "import sys, duckdb; c = duckdb.connect(sys.argv[1]); "
        "print(c.execute('SELECT count(*) FROM crib_chunks').fetchone()[0])"
    )
    out = spawn.run(
        [sys.executable, "-I", "-c", code, str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.stdout.strip() == "1", out.stderr


def test_mixed_dimensions_in_one_upsert_are_refused(tmp_path):
    store = DuckDBStore.embedded(tmp_path / "crib.duckdb")
    try:
        with pytest.raises(CribUserError, match="mixed vector dimensions"):
            store.upsert(
                [
                    Record("a", [1.0, 0.0], "a", {"project": "p"}),
                    Record("b", [1.0, 0.0, 0.0], "b", {"project": "p"}),
                ]
            )
        assert store.current_dim() is None  # nothing half-written
    finally:
        store.close()


def test_backend_switch_builds_and_releases_the_duckdb_store(tmp_path, monkeypatch):
    from crib.app import _build_store
    from crib.config import Config
    from crib.paths import Paths

    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    paths = Paths.resolve().ensure()
    cfg = Config()
    cfg.store.backend = "duckdb"

    store, on_close = _build_store(paths, cfg)
    assert isinstance(store, DuckDBStore)
    assert paths.duckdb_file.exists()
    assert on_close is not None
    on_close()
    DuckDBStore.embedded(paths.duckdb_file).close()  # the file was released


def test_unknown_backend_is_a_clear_error(tmp_path, monkeypatch):
    from crib.app import _build_store
    from crib.config import Config
    from crib.paths import Paths

    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    cfg = Config()
    cfg.store.backend = "duckbd"
    with pytest.raises(CribUserError, match="not a store crib knows"):
        _build_store(Paths.resolve(), cfg)


def test_the_removed_json_backend_warns_and_uses_duckdb(tmp_path, monkeypatch, capsys):
    from crib.app import _build_store
    from crib.config import Config
    from crib.paths import Paths

    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    cfg = Config()
    cfg.store.backend = "json"
    store, close = _build_store(Paths.resolve(), cfg)
    try:
        assert isinstance(store, DuckDBStore)
        assert "no longer supported" in capsys.readouterr().err
    finally:
        if close:
            close()


def test_config_file_selects_the_backend(tmp_path):
    from crib.config import Config

    f = tmp_path / "config.toml"
    f.write_text('[store]\nbackend = "duckdb"\n')
    assert Config.load(f).store.backend == "duckdb"
    assert Config.load(tmp_path / "absent.toml").store.backend == "duckdb"


# ── crib_meta: versions recorded in the file ─────────────────────────────────


def _rows(path, sql):
    import duckdb

    con = duckdb.connect(str(path))
    try:
        # A test's own constant statement, not data.
        # pi-lens-ignore: python-sql-injection
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _seed(path):
    s = DuckDBStore.embedded(path)
    s.upsert([Record("a", [1.0, 0.0], "doc a", {"project": "p"})])
    s.rc_put("dir", [("x.toml", 1, 2, "{}")])
    s.ec_put("m#2", [("h", [1.0, 0.0])])
    s.close()


def test_a_new_file_records_its_versions(tmp_path):
    from crib.recordcache import RECORD_FORMAT
    from crib.store_duckdb import SCHEMA_VERSION

    s = DuckDBStore.embedded(tmp_path / "crib.duckdb")
    assert s.meta_get("schema_version") == SCHEMA_VERSION
    assert s.meta_get("record_format") == RECORD_FORMAT
    s.close()


def test_a_file_from_before_crib_meta_is_adopted_not_wiped(tmp_path):
    path = tmp_path / "crib.duckdb"
    _seed(path)
    _rows(path, "DROP TABLE crib_meta")
    s = DuckDBStore.embedded(path)
    assert s.current_dim() == 2  # chunks kept
    assert s.rc_load("dir")  # caches kept
    s.close()


def test_a_schema_change_drops_every_rebuildable_table(tmp_path):
    path = tmp_path / "crib.duckdb"
    _seed(path)
    _rows(path, "UPDATE crib_meta SET value = '0' WHERE key = 'schema_version'")
    s = DuckDBStore.embedded(path)
    assert s.current_dim() is None
    assert s.rc_load("dir") == {}
    assert s.ec_get("m#2", ["h"]) == {}
    s.close()


def test_a_record_format_change_empties_only_the_record_cache(tmp_path):
    path = tmp_path / "crib.duckdb"
    _seed(path)
    _rows(path, "UPDATE crib_meta SET value = 'old' WHERE key = 'record_format'")
    s = DuckDBStore.embedded(path)
    assert s.rc_load("dir") == {}
    assert s.current_dim() == 2
    assert s.ec_get("m#2", ["h"])
    s.close()


def test_stats_reports_rows_versions_and_sizes(tmp_path):
    from crib.store_duckdb import SCHEMA_VERSION

    s = DuckDBStore.embedded(tmp_path / "crib.duckdb")
    try:
        s.upsert([Record("a", [1.0, 0.0], "doc a", {"project": "p"})])
        st = s.stats()
        assert st["rows"]["crib_chunks"] == 1
        assert st["rows"]["crib_embed_cache"] == 0
        assert st["schema_version"] == SCHEMA_VERSION
        assert st["file_bytes"] > 0
    finally:
        s.close()
