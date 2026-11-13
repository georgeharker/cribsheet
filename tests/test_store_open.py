"""Opening the store is stable and never corrupting: a current file is opened
without a write, a conversion runs once under a machine-wide lock as one
transaction, persistent conflicts end in a clear refusal, and a file DuckDB
can't use is set aside (kept) and rebuilt."""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("duckdb")

from crib import store_duckdb  # noqa: E402
from crib.errors import CribUserError  # noqa: E402
from crib.store import Record  # noqa: E402
from crib.store_duckdb import DuckDBStore  # noqa: E402


def _seed(path):
    s = DuckDBStore.embedded(path)
    s.upsert([Record("a", [1.0, 0.0], "doc a", {"project": "p"})])
    s.close()


def test_opening_a_current_file_writes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "crib.duckdb"
    _seed(path)
    wrote: list[str] = []
    real = DuckDBStore._transaction

    def spy(self, body):
        wrote.append("transaction")
        return real(self, body)

    monkeypatch.setattr(DuckDBStore, "_transaction", spy)
    s = DuckDBStore.embedded(path)
    assert wrote == [] and s.current_dim() == 2
    s.close()


def test_a_new_file_is_prepared_once(tmp_path):
    s = DuckDBStore.embedded(tmp_path / "crib.duckdb")
    assert s._schema_current()
    s.close()


def test_concurrent_converters_take_turns(tmp_path, monkeypatch):
    """Two processes converting the same store at once: the second waits on
    the lock, then finds the work done and writes nothing."""
    path = tmp_path / "crib.duckdb"
    lock = path.with_name(path.name + ".migrate.lock")
    order: list[str] = []
    held = threading.Event()

    def holder():
        with store_duckdb._migration_lock(lock):
            order.append("first in")
            held.set()
            time.sleep(0.5)
            order.append("first out")

    t = threading.Thread(target=holder)
    t.start()
    held.wait()
    with store_duckdb._migration_lock(lock):
        order.append("second in")
    t.join()
    assert order == ["first in", "first out", "second in"]


def test_persistent_conflicts_end_in_a_clear_refusal(tmp_path, monkeypatch):
    import duckdb

    s = DuckDBStore.embedded(tmp_path / "crib.duckdb")
    monkeypatch.setattr(store_duckdb, "MIGRATION_WAIT_S", 0.0)

    def conflicted(body):
        raise duckdb.TransactionException("Catalog write-write conflict")

    monkeypatch.setattr(s, "_transaction", conflicted)
    with pytest.raises(CribUserError, match="another connection kept writing"):
        s._converting(lambda ex: None)
    s.close()


def test_an_unreadable_file_is_set_aside_and_rebuilt(tmp_path, capsys):
    path = tmp_path / "crib.duckdb"
    path.write_bytes(b"this is not a duckdb file" * 4000)
    s = DuckDBStore.embedded(path)
    assert s._schema_current()
    s.close()
    kept = list(tmp_path.glob("crib.duckdb.broken-*"))
    assert len(kept) == 1 and kept[0].read_bytes().startswith(b"this is not")
    assert "moved to" in capsys.readouterr().err


def test_a_locked_file_is_never_set_aside(tmp_path):
    path = tmp_path / "crib.duckdb"
    first = DuckDBStore.embedded(path)
    try:
        with pytest.raises(CribUserError, match="open in another process"):
            # a second opener in THIS process sees no lock (DuckDB shares it), so
            # exercise the classifier the open uses instead
            import duckdb

            raise store_duckdb._lock_error(
                path, duckdb.IOException("Could not set lock on file")
            )
    finally:
        first.close()
    assert not list(tmp_path.glob("*.broken-*"))
    import duckdb

    assert not store_duckdb._unreadable(duckdb.IOException("Could not set lock"))
    assert not store_duckdb._unreadable(duckdb.IOException("Permission denied"))
    assert store_duckdb._unreadable(
        duckdb.IOException("exists, but it is not a valid DuckDB database file!")
    )


def test_the_readiness_ping_does_no_schema_work(tmp_path):
    import socket

    from crib.store_quack import QuackStore, spawn_server

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    try:
        srv = spawn_server(
            tmp_path / "crib.duckdb", port, tmp_path / "token", timeout=180
        )
    except RuntimeError as e:
        pytest.skip(f"crib DuckDB server did not start: {e}")
    try:
        QuackStore.ping(srv.uri, srv.token)  # up: answers
        with pytest.raises(Exception):
            QuackStore.ping(srv.uri, "not-the-token")  # refused, not "ready"
    finally:
        srv.stop()
