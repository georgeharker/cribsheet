"""Store robustness under failure (DuckDB store 8), both paths.

- kill -9 mid-write: the file reopens without corruption, every transaction is
  either all there or not at all, and the content-hash gate re-embeds only the
  notes the killed run didn't finish;
- a missing token is refused;
- offline: a pre-installed extension dir starts the shared path with the
  extension repository unreachable;
- a daemon restart under a live server loses no writes.

(Covered elsewhere: a wrong token, off-box binds, the SQL sandbox and its SET
lock, a server killed mid-session, a second embedded opener —
test_store_quack.py / test_store_duckdb.py.)
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from crib import proc as spawn  # noqa: E402

BATCH = 25


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# A child that writes batches until killed. Each batch is ONE transaction of
# BATCH records sharing a batch number; it prints the batch number after each
# commit so the parent knows writing is under way.
_WRITER = textwrap.dedent(
    """
    import sys
    from crib.store import Record
    mode, target = sys.argv[1], sys.argv[2]
    if mode == "embedded":
        from crib.store_duckdb import DuckDBStore
        store = DuckDBStore.embedded(target)
    else:
        from crib.store_quack import QuackStore
        store = QuackStore.connect(target, sys.argv[3])
    b = 0
    while True:
        recs = [Record(f"b{b}-{i}", [float(i + 1), 1.0, 0.5], f"doc {b} {i}",
                       {"project": "p", "batch": str(b)}) for i in range(%d)]
        store.apply(recs, [], {})
        print(b, flush=True)
        b += 1
"""
    % BATCH
)


def _run_writer(tmp_path: Path, *args: str) -> subprocess.Popen:
    script = tmp_path / "writer.py"
    script.write_text(_WRITER)
    return spawn.popen(
        [sys.executable, str(script), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _kill_after(p: subprocess.Popen, commits: int) -> int:
    """Let `p` commit `commits` batches, then kill -9 it mid-stream."""
    assert p.stdout is not None
    seen = 0
    deadline = time.monotonic() + 120
    while seen < commits and time.monotonic() < deadline:
        line = p.stdout.readline()
        if not line:
            break
        seen += 1
    os.kill(p.pid, signal.SIGKILL)
    p.wait(30)
    assert seen >= commits, (p.stderr.read() if p.stderr else "")[-2000:]
    return seen


def _assert_batches_whole(store, at_least: int) -> None:
    meta = store.get_meta({"project": "p"})
    sizes: dict[str, int] = {}
    for m in meta.values():
        sizes[m["batch"]] = sizes.get(m["batch"], 0) + 1
    assert len(sizes) >= at_least
    assert set(sizes.values()) == {BATCH}, "a transaction was torn"


def test_kill_9_mid_write_embedded(tmp_path):
    from crib.store_duckdb import DuckDBStore

    db = tmp_path / "crib.duckdb"
    p = _run_writer(tmp_path, "embedded", str(db))
    committed = _kill_after(p, 8)
    store = DuckDBStore.embedded(db)  # reopens: no corruption
    try:
        _assert_batches_whole(store, committed)
        store.upsert([])  # and it still writes
    finally:
        store.close()


def test_kill_9_of_the_server_mid_write(tmp_path):
    from crib.store_quack import QuackStore, spawn_server

    port, db, tok = _free_port(), tmp_path / "crib.duckdb", tmp_path / "token"
    try:
        srv = spawn_server(db, port, tok, timeout=180)
    except RuntimeError as e:
        pytest.skip(f"crib DuckDB server did not start: {e}")
    writer = _run_writer(tmp_path, "shared", srv.uri, srv.token)
    try:
        assert writer.stdout is not None
        for _ in range(6):  # writing is under way
            assert writer.stdout.readline()
        os.kill(srv.proc.pid, signal.SIGKILL)  # the SERVER dies mid-stream
        srv.proc.wait(30)
    finally:
        writer.kill()
        writer.wait(30)
    srv = spawn_server(db, port, tok, timeout=180)  # sharedserver's relaunch
    try:
        store = QuackStore.connect(srv.uri, srv.token)
        try:
            _assert_batches_whole(store, 6)
        finally:
            store.close()
    finally:
        srv.stop()


def test_a_killed_reindex_resumes_with_only_what_was_missing(tmp_path, monkeypatch):
    """The content-hash gate: after a reindex is kill -9'd, the next one embeds
    only the notes the killed run never committed."""
    env = {
        **os.environ,
        "CRIB_CONFIG_DIR": str(tmp_path / "config"),
        "CRIB_DATA_DIR": str(tmp_path / "data"),
        "CRIB_INDEX_DIR": str(tmp_path / "index"),
    }
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.toml").write_text(
        '[embed]\nmodel = "hash"\n[duckdb]\nmode = "embedded"\n'
    )
    nd = tmp_path / "data" / "projects" / "p" / "notes"
    nd.mkdir(parents=True)
    n = 400
    for i in range(n):
        (nd / f"n{i:03}.md").write_text(
            f"---\ntitle: note {i}\nid: 01N{i:023}\n---\n\n" + f"Note body {i}. " * 40
        )
    child = tmp_path / "reindex.py"
    child.write_text(
        textwrap.dedent("""
        import asyncio
        from crib.app import Crib
        crib = Crib.open()
        orig = crib.index.index_file
        async def traced(*a, **k):
            r = await orig(*a, **k)
            print("done", flush=True)
            return r
        crib.index.index_file = traced
        asyncio.run(crib.reindex(project="p"))
    """)
    )
    p = spawn.popen(
        [sys.executable, str(child)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _kill_after(p, 60)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import asyncio

    from crib.app import Crib

    crib = Crib.open()
    try:
        done = {
            m["relpath"]
            for m in crib.store.get_meta({"project": "p", "kind": "note"}).values()
        }
        assert 60 <= len(done) < n
        out = asyncio.run(crib.reindex(project="p"))
        assert out["changed"] == n - len(done)  # only what was missing
        assert (
            len(
                {
                    m["relpath"]
                    for m in crib.store.get_meta(
                        {"project": "p", "kind": "note"}
                    ).values()
                }
            )
            == n
        )
    finally:
        crib.close()


def test_a_missing_token_is_refused(tmp_path):
    from crib.store_quack import QuackStore, spawn_server

    try:
        srv = spawn_server(
            tmp_path / "crib.duckdb", _free_port(), tmp_path / "token", timeout=180
        )
    except RuntimeError as e:
        pytest.skip(f"crib DuckDB server did not start: {e}")
    try:
        with pytest.raises(Exception, match="Authentication failed|token"):
            QuackStore.connect(srv.uri, "")
    finally:
        srv.stop()


def test_a_daemon_restart_under_a_live_server_loses_no_writes(tmp_path):
    from crib.store import Record
    from crib.store_quack import QuackStore, spawn_server

    try:
        srv = spawn_server(
            tmp_path / "crib.duckdb", _free_port(), tmp_path / "token", timeout=180
        )
    except RuntimeError as e:
        pytest.skip(f"crib DuckDB server did not start: {e}")
    try:
        first = QuackStore.connect(srv.uri, srv.token)  # daemon 1
        first.upsert(
            [
                Record(f"r{i}", [1.0, float(i)], f"d{i}", {"project": "p"})
                for i in range(50)
            ]
        )
        first.close()  # daemon 1 exits
        second = QuackStore.connect(srv.uri, srv.token)  # daemon 2, same server
        try:
            assert len(second.get_meta({"project": "p"})) == 50
        finally:
            second.close()
    finally:
        srv.stop()


def test_the_shared_path_starts_offline_from_preinstalled_extensions(tmp_path):
    """With the extension repository unreachable, LOAD from a pre-installed
    extension dir works and an empty one fails: starting never needs the
    network once `crib store install-extensions` has run."""
    import duckdb

    from crib.paths import Paths
    from crib.store_quack import _connect_config, _install_quack

    installed = Paths.resolve().duckdb_extension_dir  # crib's own, as the server uses
    if not any(installed.rglob("quack.duckdb_extension")):
        pytest.skip("quack is not installed in crib's extension dir on this host")

    def attempt(ext_dir: Path) -> None:
        conn = duckdb.connect(":memory:", config=_connect_config(ext_dir))
        try:
            conn.execute("SET custom_extension_repository = 'http://127.0.0.1:9'")
            conn.execute("SET autoinstall_known_extensions = false")
            _install_quack(conn)
        finally:
            conn.close()

    attempt(installed)  # offline: loads
    empty = tmp_path / "empty-extensions"
    empty.mkdir()
    with pytest.raises(Exception):
        attempt(empty)  # nothing to load, no network
