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
from .items import Item
from .retrieve import PostingStats
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
            # Untyped, and that's fine: DuckDB coerces `[]` to the column (or the
            # cast) it lands in. Empty FILTER lists never get here; they are FALSE.
            return "[]"
        if all(type(v) is float for v in value):
            # A vector: ONE string literal cast to DOUBLE[], not one literal per
            # element. DuckDB parses it ~20x faster (a 384-d vector is one token,
            # not 384 casts). The text holds only repr() of finite floats, so it
            # cannot contain a quote; DOUBLE keeps every value bit-exact, and the
            # column's own cast to FLOAT rounds exactly as a bound parameter does.
            for v in value:
                if not math.isfinite(v):
                    raise CribUserError(f"cannot store a non-finite number ({v!r})")
            return "'[" + ", ".join(repr(v) for v in value) + "]'::DOUBLE[]"
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


# What the shared path loads: quack for the protocol, httpfs because
# quack_serve uses it. Embedded mode needs neither.
SHARED_EXTENSIONS = ("quack", "httpfs")


def install_extensions(extension_directory: Path | str | None = None) -> list[str]:
    """Download the shared path's DuckDB extensions into crib's extension dir
    now, so a container image or an offline machine never fetches them at
    first use. Returns the extensions installed (already present: a no-op)."""
    import duckdb  # lazy

    conn = duckdb.connect(":memory:", config=_connect_config(extension_directory))
    try:
        for name in SHARED_EXTENSIONS:
            conn.execute(f"INSTALL {name}")
            conn.execute(f"LOAD {name}")
        return list(SHARED_EXTENSIONS)
    finally:
        conn.close()


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
    """The server went away (IOException: can't reach it), was restarted (our
    session id is unknown to the new process), or an earlier reconnect failed
    and left this client's own connection closed (ConnectionException). Anything
    else — a constraint, a dimension mismatch, a refused token — is a real
    error and is not retried."""
    import duckdb  # lazy

    if isinstance(exc, (duckdb.IOException, duckdb.ConnectionException)):
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
        # Set before super().__init__, which prepares the schema through them.
        self._remote = remote
        self._uri = uri
        self._token = token
        self._ext = extension_directory
        self._relaunch = relaunch
        self._session = threading.RLock()
        # Bumped by each successful reconnect, so threads that lost the same
        # session reconnect (and relaunch) once between them, not once each.
        self._generation = 0
        self.reconnects = 0  # successful reconnects, for status
        super().__init__(conn)

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

    def _reconnect(self, lost_generation: int) -> None:
        """Replace the session lost at `lost_generation`. The new session is
        opened BEFORE the old one is dropped: if the server isn't back yet this
        raises and the client is unchanged, so the next operation fails as
        session-lost again and retries, rather than wedging on a closed
        connection."""
        with self._session:
            if self._generation != lost_generation:
                return  # another thread already reconnected
            if self._relaunch is not None:
                self._relaunch()
            conn, remote = _open_session(self._uri, self._token, self._ext)
            with contextlib.suppress(Exception):  # the old session is dead anyway
                self._conn.close()
            self._conn, self._remote = conn, remote
            self._generation += 1
            self.reconnects += 1
            print(f"[crib] DuckDB server session re-established ({self._uri})",
                  file=sys.stderr)

    def _call(self, op: Callable[..., Any], *args: Any) -> Any:
        generation = self._generation
        try:
            return op(*args)
        except Exception as exc:  # noqa: BLE001 — classified below
            failure = exc
        if not _session_lost(failure):
            raise failure
        self._reconnect(generation)
        return op(*args)

    def upsert(self, records: list[Record]) -> None:
        self._call(super().upsert, records)

    def delete(self, ids: list[str]) -> None:
        self._call(super().delete, ids)

    def apply(
        self,
        records: list[Record],
        deletes: list[str],
        meta: dict[str, dict[str, Any]],
        items: list[Item] | None = None,
        item_deletes: list[str] | None = None,
    ) -> None:
        self._call(super().apply, records, deletes, meta, items, item_deletes)

    def items(self, where: dict[str, Any]) -> dict[str, Item]:
        return self._call(super().items, where)

    def edges(self, where: dict[str, Any]) -> list[tuple[str, str, str, str | None]]:
        return self._call(super().edges, where)

    def item_hashes(self, where: dict[str, Any]) -> dict[str, str]:
        return self._call(super().item_hashes, where)

    def postings(self, where: dict[str, Any], terms: list[str]) -> PostingStats:
        return self._call(super().postings, where, terms)

    def set_body_vectors(self, vectors: dict[str, list[float]]) -> None:
        self._call(super().set_body_vectors, vectors)

    def lacking_body_vectors(
        self, where: dict[str, Any], candidates_only: bool = False
    ) -> list[str]:
        return self._call(super().lacking_body_vectors, where, candidates_only)

    def body_vector_top(
        self, where: dict[str, Any], vec: list[float], n: int
    ) -> list[tuple[str, float]]:
        return self._call(super().body_vector_top, where, vec, n)

    def body_vectors(self, ids: list[str]) -> dict[str, list[float]]:
        return self._call(super().body_vectors, ids)

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

    def score_ids(self, embedding: list[float], ids: list[str]) -> dict[str, float]:
        return self._call(super().score_ids, embedding, ids)

    def meta_get(self, key: str) -> str | None:
        return self._call(super().meta_get, key)

    def checkpoint(self) -> None:
        self._call(super().checkpoint)

    def stats(self) -> dict[str, Any]:
        return {**self._call(super().stats), "server": self._uri,
                "reconnects": self.reconnects}

    def count(self, where: dict[str, Any], relpath_prefix: str = "") -> int:
        return self._call(super().count, where, relpath_prefix)

    def projects(self) -> set[str]:
        return self._call(super().projects)

    def meta_set(self, key: str, value: str) -> None:
        self._call(super().meta_set, key, value)

    def recreate(self) -> None:
        self._call(super().recreate)

    def ec_get(self, model: str, hashes: list[str]) -> dict[str, list[float]]:
        return self._call(super().ec_get, model, hashes)

    def ec_put(self, model: str, rows: list[tuple[str, list[float]]]) -> None:
        self._call(super().ec_put, model, rows)

    def rc_load(self, key: str) -> dict[str, tuple[int, int, str]]:
        return self._call(super().rc_load, key)

    def rc_put(self, key: str, rows: list[tuple[str, int, int, str]]) -> None:
        self._call(super().rc_put, key, rows)

    def rc_delete(self, key: str, names: list[str]) -> None:
        self._call(super().rc_delete, key, names)

    def ledger_rows(self, purpose: str, targets: list[str]) -> dict[str, Any]:
        return self._call(super().ledger_rows, purpose, targets)

    def ledger_put(self, entries: list[Any]) -> None:
        self._call(super().ledger_put, entries)

    def ledger_delete(self, purpose: str, targets: list[str]) -> None:
        self._call(super().ledger_delete, purpose, targets)

    def ledger_prune(self, purpose: str, prefix: str, keep: list[str]) -> None:
        self._call(super().ledger_prune, purpose, prefix, keep)

    def ec_prune(self, keep_model: str) -> int:
        return self._call(super().ec_prune, keep_model)

    def ledger_counts(self, now: float) -> dict[str, dict[str, int]]:
        return self._call(super().ledger_counts, now)

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
