"""DuckDB vector store — the SHARED path, over Quack (DuckDB's client-server protocol).

One crib-owned server process owns the DuckDB file and serves it over Quack on
loopback; every crib process (the daemon, a `--no-daemon` CLI) is a client. The
store is `DuckDBStore`'s SQL unchanged — only where it runs differs:

- **Every statement runs in ONE server-routed session** (`CONNECT`), so a write is
  a single transaction on the server. A client attached without CONNECT cannot
  DELETE or UPDATE remote rows ("not implemented" in duckdb 2.0.0), so staging
  local rows and pushing them across isn't a general option.
- **Parameters are rendered as literals** (`render`), because bound parameters do
  not cross Quack in duckdb 2.0.0 (`?`, `$name` and executemany all fail with
  "Values were not provided"). `to_literal` is the ONE place a value becomes SQL
  text. It renders only the types crib's store binds and refuses anything it
  cannot render exactly. The Store contract suite's hostile-value tests run
  against this path.

The server is locked down after it starts serving: `enable_external_access =
false` (no file reads/writes, ATTACH or extension installs from a client) and
`lock_configuration = true` (a client cannot SET it back). A token holder can then
only reach the one database. The token lives in a 0600 file beside the database.
The bind is loopback-only.
"""

from __future__ import annotations

import argparse
import contextlib
import math
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import proc as spawn
from .errors import CribUserError
from .store import Hit, Record, is_loopback_host
from .store_duckdb import DuckDBStore, local_dirs

# ── literal rendering: the one place a value becomes SQL text ──────────────


def to_literal(value: Any) -> str:
    """`value` as a DuckDB SQL literal that reads back exactly. Only the types
    crib's store binds are accepted; anything else is an error, never a guess."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):  # before int: bool is an int subclass
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CribUserError(f"cannot store a non-finite number ({value!r})")
        # A bare 1.5 parses as an exact DECIMAL (and -0.0 loses its sign); the
        # shortest round-tripping string cast to DOUBLE reads back bit-exact.
        return f"'{value!r}'::DOUBLE"
    if isinstance(value, str):
        if "\x00" in value:
            raise CribUserError("cannot store a string containing a NUL character")
        # Standard SQL string: the only escape is a doubled quote (DuckDB does
        # not treat backslash specially outside E'' strings).
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, (list, tuple)):
        if not value:
            raise CribUserError("cannot render an empty list literal (no element type)")
        return "[" + ", ".join(to_literal(v) for v in value) + "]"
    raise CribUserError(f"cannot render a {type(value).__name__} as a SQL literal")


def render(sql: str, params: Sequence[Any]) -> str:
    """Substitute each `?` placeholder in a CONSTANT statement with the literal of
    its parameter, in order. The statements are crib's own (no `?` inside any
    string literal), so splitting on `?` is exact; a count mismatch is a bug."""
    parts = sql.split("?")
    if len(parts) - 1 != len(params):
        raise AssertionError(
            f"{len(parts) - 1} placeholders but {len(params)} parameters in {sql!r}"
        )
    out = [parts[0]]
    for value, tail in zip(params, parts[1:]):
        out.append(to_literal(value))
        out.append(tail)
    return "".join(out)


# INSERT ... VALUES (?, ?, …): a run_many of these becomes multi-row statements.
_VALUES_TAIL = re.compile(
    r"^(?P<head>.*\bVALUES\s*)(?P<group>\(\s*\?(?:\s*,\s*\?)*\s*\))\s*$", re.S
)
_ROWS_PER_STATEMENT = 200


class _Literal:
    """Runs SQL on the server-routed session with parameters rendered as literals."""

    def __init__(self, cur: Any) -> None:
        self._cur = cur

    # The deliberate literal path (see the module docstring): `sql` is one of
    # store_duckdb's constant statements and every value is rendered by
    # `to_literal`, the single escaping point, covered by the hostile-value tests.
    def run(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        # pi-lens-ignore: python-sql-injection
        return self._cur.execute(render(sql, params)).fetchall()

    def run_many(self, sql: str, rows: list[list[Any]]) -> None:
        m = _VALUES_TAIL.match(sql)
        if m is None:  # e.g. UPDATE … WHERE id = ?: one statement per row
            for row in rows:
                self.run(sql, row)
            return
        head, group = m["head"], m["group"]
        for start in range(0, len(rows), _ROWS_PER_STATEMENT):
            batch = rows[start : start + _ROWS_PER_STATEMENT]
            # pi-lens-ignore: python-sql-injection
            self._cur.execute(head + ", ".join(render(group, row) for row in batch))


# ── the client ──────────────────────────────────────────────────────────────


def _install_quack(conn: Any) -> None:
    """Load Quack, installing it on first use. INSTALL fetches it from
    extensions.duckdb.org into the connection's extension directory; after that
    it loads offline."""
    try:
        conn.execute("LOAD quack")
    except Exception:  # noqa: BLE001 — not installed yet: install, then load
        conn.execute("INSTALL quack")
        conn.execute("LOAD quack")


def _connect_config(
    extension_directory: Path | str | None,
) -> dict[str, str | bool | int | float | list[str]]:
    """crib's DuckDB dirs for a Quack connection (see `local_dirs`): extensions
    at `extension_directory`, secrets beside it. None means crib's own default
    (`Paths.duckdb_extension_dir`), never DuckDB's ~/.duckdb."""
    if not extension_directory:
        from .paths import Paths

        extension_directory = Paths.resolve().duckdb_extension_dir
    return local_dirs(Path(extension_directory).parent)


def _open_session(uri: str, token: str, extension_directory: Path | str | None):
    """(connection, server-routed cursor) for one Quack session."""
    import duckdb  # lazy

    conn = duckdb.connect(":memory:", config=_connect_config(extension_directory))
    try:
        _install_quack(conn)
        # The token travels as a literal: Quack's ATTACH takes no parameters.
        conn.execute(
            f"ATTACH {to_literal(uri)} AS crib_remote (TOKEN {to_literal(token)})"
        )
        remote = conn.cursor()
        remote.execute("CONNECT crib_remote")
    except Exception:
        conn.close()
        raise
    return conn, remote


def _session_lost(exc: BaseException) -> bool:
    """The server went away (IOException: can't reach it) or was restarted (our
    session id is unknown to the new process). Anything else — a constraint, a
    dimension mismatch, a refused token — is a real error and is not retried."""
    import duckdb  # lazy

    if isinstance(exc, duckdb.IOException):
        return True
    return isinstance(exc, duckdb.InvalidInputException) and "connection id" in str(exc)


class QuackStore(DuckDBStore):
    """A client of the crib DuckDB server. Thread-safe: the daemon calls the store
    from worker threads, and every operation runs on the ONE server-routed session
    under a lock (a write's BEGIN…COMMIT must not interleave with another
    thread's statements on the same session).

    Survives a server restart: when the session is lost mid-operation the client
    (optionally asking `relaunch` to bring the server back first) opens a fresh
    session and runs the operation ONCE more. That is safe because every
    operation is a read or one atomic transaction — a write the dead server never
    committed is simply done again."""

    def __init__(
        self,
        conn: Any,
        remote: Any,
        uri: str,
        token: str,
        extension_directory: Path | str | None = None,
        relaunch: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(conn)
        self._remote = remote
        self._uri = uri
        self._token = token
        self._ext = extension_directory
        self._relaunch = relaunch
        self._session = threading.RLock()

    @classmethod
    def connect(
        cls,
        uri: str,
        token: str,
        extension_directory: Path | str | None = None,
        relaunch: Callable[[], None] | None = None,
    ) -> "QuackStore":
        conn, remote = _open_session(uri, token, extension_directory)
        return cls(conn, remote, uri, token, extension_directory, relaunch)

    @contextlib.contextmanager
    def _reading(self) -> Iterator[Any]:
        with self._session:
            yield _Literal(self._remote)

    @contextlib.contextmanager
    def _writing(self) -> Iterator[Any]:
        with self._session:
            yield _Literal(self._remote)

    def _reconnect(self) -> None:
        with self._session:
            with contextlib.suppress(Exception):  # the old session is dead anyway
                self._conn.close()
            if self._relaunch is not None:
                self._relaunch()
            self._conn, self._remote = _open_session(self._uri, self._token, self._ext)

    def _call(self, op: Callable[..., Any], *args: Any) -> Any:
        try:
            return op(*args)
        except Exception as exc:  # noqa: BLE001 — classified below
            failure = exc
        if not _session_lost(failure):
            raise failure
        self._reconnect()
        return op(*args)

    def upsert(self, records: list[Record]) -> None:
        self._call(super().upsert, records)

    def delete(self, ids: list[str]) -> None:
        self._call(super().delete, ids)

    def set_meta(self, updates: dict[str, dict[str, Any]]) -> None:
        self._call(super().set_meta, updates)

    def get_meta(self, where: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return self._call(super().get_meta, where)

    def get_docs(self, where: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
        return self._call(super().get_docs, where)

    def query(
        self, embedding: list[float], k: int, where: dict[str, Any] | None = None
    ) -> list[Hit]:
        return self._call(super().query, embedding, k, where)

    def current_dim(self) -> int | None:
        return self._call(super().current_dim)

    def recreate(self) -> None:
        self._call(super().recreate)

    def _page(self, after: str, n: int) -> list[Record]:
        # iter_records is a generator, so the reconnect wraps each PAGE: an
        # export carries on across a server restart.
        return self._call(super()._page, after, n)

    def close(self) -> None:
        with self._session:
            with contextlib.suppress(Exception):  # the server may be gone already
                self._remote.execute("DISCONNECT")
            self._conn.close()


# ── the server ──────────────────────────────────────────────────────────────


def quack_uri(host: str, port: int) -> str:
    return f"quack:[{host}]:{port}" if ":" in host else f"quack:{host}:{port}"


def ensure_token(path: Path) -> str:
    """The server's auth token: read it, or create it ONCE as a 0600 file. Clients
    read the same file, so it is never in config, argv or logs."""
    with contextlib.suppress(FileNotFoundError):
        token = path.read_text().strip()
        if token:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(24)
    # Write a private temp file in full, then hard-link it into place: the link
    # is atomic and fails if the token already exists, so a reader never sees a
    # half-written token and two creators agree on whichever linked first.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        try:
            os.link(tmp, path)
        except FileExistsError:  # another process got there first: use theirs
            return path.read_text().strip()
        return token
    finally:
        tmp.unlink(missing_ok=True)


def check_shared_bind(host: str) -> None:
    """The shared DuckDB server binds to loopback only. A Quack token holder gets
    the store's SQL; off-box exposure would need TLS and a reverse proxy, which
    crib doesn't set up, so it is refused rather than opted into."""
    if not is_loopback_host(host):
        raise CribUserError(
            f"[duckdb] host = {host!r} is not loopback. The shared DuckDB server "
            "only binds to this machine (127.0.0.1, ::1 or localhost)"
        )


def serve(
    db_path: Path | str,
    host: str,
    port: int,
    token: str,
    extension_directory: Path | str | None = None,
) -> Any:
    """Open the database, start Quack on loopback, then lock the server down.
    Returns the connection; the server runs while it stays open."""
    import duckdb  # lazy

    check_shared_bind(host)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path), config=_connect_config(extension_directory))
    _install_quack(conn)
    uri = quack_uri(host, port)
    conn.execute(f"CALL quack_serve({to_literal(uri)}, token = {to_literal(token)})")
    # Lock down AFTER serving: no file reads/writes, ATTACH or extension installs
    # from a client, and no client can SET it back. It can't go first:
    # quack_serve needs file access (httpfs, the secrets dir), and allowlisting
    # those would leave a token holder a directory to read and write. The cost
    # is the microseconds between serve and lockdown; only a holder of the 0600
    # token could try to race it, and READY is printed only after.
    conn.execute("SET enable_external_access = false")
    conn.execute("SET lock_configuration = true")
    return conn


def server_command(
    db_path: Path,
    host: str,
    port: int,
    token_file: Path,
    extension_directory: Path | None,
) -> list[str]:
    """The argv sharedserver launches for the shared store."""
    cmd = [
        sys.executable,
        "-m",
        "crib.store_quack",
        "--db",
        str(db_path),
        "--host",
        host,
        "--port",
        str(port),
        "--token-file",
        str(token_file),
    ]
    if extension_directory is not None:
        cmd += ["--extension-dir", str(extension_directory)]
    return cmd


READY = "crib-duckdb ready"


def main(argv: list[str] | None = None) -> int:
    """`python -m crib.store_quack`: serve crib's DuckDB store over Quack. Every
    option defaults to crib's own config and paths ([duckdb] host/port, the
    store file, token file and extension dir under the index dir), so an
    external supervisor can run it bare and it serves exactly what crib expects."""
    from .config import Config
    from .paths import Paths

    paths = Paths.resolve()
    cfg = Config.load(paths.config_file).duckdb
    ap = argparse.ArgumentParser(prog="python -m crib.store_quack")
    ap.add_argument("--db", type=Path, default=paths.duckdb_file)
    ap.add_argument("--host", default=cfg.host)
    ap.add_argument("--port", type=int, default=cfg.port)
    ap.add_argument("--token-file", type=Path, default=paths.duckdb_token_file)
    ap.add_argument("--extension-dir", type=Path, default=paths.duckdb_extension_dir)
    args = ap.parse_args(argv)
    try:
        conn = serve(
            args.db,
            args.host,
            args.port,
            ensure_token(args.token_file),
            args.extension_dir,
        )
    except CribUserError as e:
        print(f"crib-duckdb: {e}", file=sys.stderr)
        return 2
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    print(f"{READY} {quack_uri(args.host, args.port)}", flush=True)
    stop.wait()
    conn.close()  # checkpoints and releases the file
    return 0


# ── a server for tests and benchmarks ───────────────────────────────────────


@dataclass
class ServerHandle:
    proc: subprocess.Popen
    uri: str
    token: str

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)


def spawn_server(
    db_path: Path,
    port: int,
    token_file: Path,
    extension_directory: Path | None = None,
    host: str = "127.0.0.1",
    timeout: float = 120.0,
) -> ServerHandle:
    """Start the server as a child process and wait until it is serving."""
    # Spawned, never forked: see `crib.proc`.
    proc = spawn.popen(
        server_command(db_path, host, port, token_file, extension_directory),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    line = ""
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line.startswith(READY) or proc.poll() is not None:
            break
    if not line.startswith(READY):
        proc.kill()
        err = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"crib-duckdb server did not start: {line!r} {err[-500:]}")
    return ServerHandle(proc, quack_uri(host, port), token_file.read_text().strip())


if __name__ == "__main__":
    sys.exit(main())
