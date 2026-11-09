"""DuckDB vector store — the embedded path (one process owns the file).

Exact search: every filtered query scores the candidate rows with
`array_cosine_similarity` and keeps the top k. At crib's scale (thousands of
chunks) that is about a millisecond, never misses, and needs no extension; an
approximate index is a benchmarked option, not the default (design: "Vector store
moves to DuckDB on two paths").

Layout: one table, created at the first upsert because its vector column is
fixed-size (`FLOAT[dim]`), the dimension of whatever embedder wrote first. The
keys crib filters on constantly (`project`, `store`, `relpath`) are promoted to
real columns; every other metadata key is filtered through the JSON copy, by
type, so `{"n": "1"}` never matches an int 1 (the contract's exact match).

Every value travels as a bound parameter — no SQL text is built from data. Table
and column names are fixed constants; JSON paths are bound too. All SQL runs
through an executor (`_Bound` here); the shared path (`crib.store_quack`) swaps in
one that renders the same statements' parameters as literals, because bound
parameters don't cross Quack in duckdb 2.0.0.
"""

from __future__ import annotations

import contextlib
import json
import random
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .errors import CribUserError
from .store import Hit, Record

TABLE = "crib_chunks"
# The generation ledger (crib.genledger): beside the chunks, NOT part of them, so a
# vector rebuild (recreate) keeps it.
LEDGER = "crib_gen_ledger"
# The derived-index record cache (crib.recordcache): parsed TOML records keyed by
# tier directory + filename + (mtime, size). Like the ledger, outside crib_chunks.
RECORD_CACHE = "crib_record_cache"
# The embedding cache (crib.embedcache): text hash -> vector per model; plain
# vectors, no index (search builds what it needs in memory).
EMBED_CACHE = "crib_embed_cache"
PROMOTED = ("project", "store", "relpath")  # filtered constantly: real columns
# The only column lists a read may select — never caller text.
_SELECT_COLUMNS = MappingProxyType({"meta": "id, meta", "docs": "id, document, meta"})


def local_dirs(base: Path | str) -> dict[str, str | bool | int | float | list[str]]:
    """DuckDB connection settings that keep its state in crib's own dir, never
    the user's ~/.duckdb: downloaded extensions and the secrets directory
    (which quack_serve reads) sit under `base`. DuckDB has no environment
    variable for these, so every crib connection passes them explicitly."""
    base = Path(base)
    secrets = base / "secrets"
    secrets.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {
        "extension_directory": str(base / "extensions"),
        "secret_directory": str(secrets),
    }


def _lock_error(path: Path, exc: Exception) -> CribUserError:
    return CribUserError(
        f"the DuckDB store {path} is open in another process ({exc}). Embedded mode "
        "allows ONE process per file: run crib through its daemon (the default — "
        "`crib <verb>` attaches to it), or switch to shared mode so several "
        "processes can use the store at once"
    )


def _json_path(key: str) -> str:
    """A JSON path naming `key` exactly, whatever characters it contains."""
    return "$." + json.dumps(key)


def _typed_match(key: str, value: Any) -> tuple[str, list[Any]]:
    """SQL condition + params matching metadata `key` == `value` EXACTLY, type
    included — the JSON copy's type must agree before the value is compared."""
    path = _json_path(key)
    if isinstance(value, bool):  # before int: bool is an int subclass
        return (
            "(json_type(meta, ?) = 'BOOLEAN' AND CAST(json_extract(meta, ?) AS BOOLEAN) = ?)",
            [path, path, value],
        )
    if isinstance(value, int):
        return (
            "(json_type(meta, ?) IN ('BIGINT', 'UBIGINT') "
            "AND CAST(json_extract(meta, ?) AS HUGEINT) = ?)",
            [path, path, value],
        )
    if isinstance(value, float):
        return (
            "(json_type(meta, ?) = 'DOUBLE' AND CAST(json_extract(meta, ?) AS DOUBLE) = ?)",
            [path, path, value],
        )
    if isinstance(value, str):
        return (
            "(json_type(meta, ?) = 'VARCHAR' AND json_extract_string(meta, ?) = ?)",
            [path, path, value],
        )
    # Lists/dicts/None aren't metadata crib writes; a filter on one matches nothing.
    return ("FALSE", [])


def _where(where: dict[str, Any] | None) -> tuple[str, list[Any]]:
    """`WHERE …` for an exact-match metadata filter (all keys must match)."""
    if not where:
        return "", []
    conds: list[str] = []
    params: list[Any] = []
    for key, value in where.items():
        if key in PROMOTED and isinstance(value, str):
            conds.append(f"{key} = ?")  # key is one of the constant column names
            params.append(value)
        else:
            cond, ps = _typed_match(key, value)
            conds.append(cond)
            params.extend(ps)
    return " WHERE " + " AND ".join(conds), params


def _promoted(meta: dict[str, Any]) -> list[Any]:
    """The promoted columns' values; a non-string (or absent) value is NULL so
    the column filter never matches it and the JSON path decides instead."""
    return [v if isinstance(v := meta.get(k), str) else None for k in PROMOTED]


class _Bound:
    """Runs SQL on one DuckDB cursor with BOUND parameters (the embedded path)."""

    def __init__(self, cur: Any) -> None:
        self._cur = cur

    # `sql` is always one of this module's constant statements; every value
    # arrives in `params`/`rows` and is bound by DuckDB, never spliced in.
    def run(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        # pi-lens-ignore: python-sql-injection
        return self._cur.execute(sql, list(params)).fetchall()

    def run_many(self, sql: str, rows: list[list[Any]]) -> None:
        # pi-lens-ignore: python-sql-injection
        self._cur.executemany(sql, rows)


# DuckDB's concurrency is optimistic: when two transactions touch the same rows
# (or both create the table), one is rolled back with a TransactionException and
# must be retried. Only the shared path has several writers; the embedded path
# is serialized in-process, but retrying costs it nothing.
_CONFLICT_RETRIES = 8


def _retryable(exc: BaseException, attempt: int) -> bool:
    import duckdb  # lazy

    return attempt < _CONFLICT_RETRIES and isinstance(exc, duckdb.TransactionException)


def _backoff(attempt: int) -> float:
    """Jittered exponential backoff: ~5 ms, doubling, capped at ~0.2 s."""
    return min(0.2, 0.005 * 2**attempt) * (0.5 + random.random())


def _dim_of(data_type: str) -> int:
    """'FLOAT[384]' -> 384."""
    return int(data_type.rsplit("[", 1)[1].rstrip("]"))


class DuckDBStore:
    """Embedded DuckDB: the daemon is the only process with the file open.

    Thread-safe: the daemon calls the store from worker threads. Reads each run
    on their own cursor (DuckDB cursors are separate connections into one
    database); writes are serialized so two writers never race a first-upsert
    CREATE TABLE or conflict mid-transaction.

    Subclasses change WHERE the SQL runs, never WHAT it is: they override
    `_reading` / `_writing` to hand out a different executor."""

    def __init__(self, conn: Any, path: Path | None = None) -> None:
        self._conn = conn
        self._path = path
        self._write = threading.Lock()

    @classmethod
    def embedded(cls, path: Path | str) -> "DuckDBStore":
        import duckdb  # lazy: only this backend needs it

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = duckdb.connect(str(path), config=local_dirs(path.parent))
        except duckdb.IOException as exc:
            if "lock" in str(exc).lower():
                raise _lock_error(path, exc) from exc
            raise
        return cls(conn, path)

    def close(self) -> None:
        self._conn.close()

    @contextlib.contextmanager
    def _reading(self) -> Iterator[Any]:
        yield _Bound(self._conn.cursor())

    @contextlib.contextmanager
    def _writing(self) -> Iterator[Any]:
        with self._write:
            yield _Bound(self._conn.cursor())

    # ── schema ───────────────────────────────────────────────────────────────

    def current_dim(self) -> int | None:
        """The stored vectors' dimension, or None when the store is EMPTY (the
        contract) — an empty table still has a typed column, so check for rows."""
        with self._reading() as ex:
            dim = self._table_dim(ex)
            if dim is None:
                return None
            # pi-lens-ignore: python-sql-injection
            return dim if ex.run(f"SELECT 1 FROM {TABLE} LIMIT 1") else None

    @staticmethod
    def _table_dim(ex: Any) -> int | None:
        """The vector column's dimension, rows or not (None: no table yet)."""
        rows = ex.run(
            "SELECT data_type FROM duckdb_columns() "
            "WHERE table_name = ? AND column_name = 'vec'",
            [TABLE],
        )
        return _dim_of(rows[0][0]) if rows else None

    def _ensure_table(self, ex: Any, dim: int) -> None:
        have = self._table_dim(ex)
        if have is None:
            ex.run(
                f"CREATE TABLE IF NOT EXISTS {TABLE} ("
                "id VARCHAR PRIMARY KEY, document VARCHAR, meta JSON, "
                "project VARCHAR, store VARCHAR, relpath VARCHAR, "
                f"vec FLOAT[{int(dim)}])"
            )
        elif have != dim:
            raise CribUserError(
                f"embedding dimension mismatch: the store holds {have}-d vectors, "
                f"this write has {dim}-d — the embedder changed; run "
                "`crib project reconcile` to re-embed at the current dimension"
            )

    def _transaction(self, body: Callable[[Any], None]) -> None:
        """Run `body(executor)` as ONE transaction, retrying it from the start when
        DuckDB reports a conflict with a concurrent writer (see `_retryable`)."""
        attempt = 0
        while True:
            failure: BaseException | None = None
            with self._writing() as ex:
                ex.run("BEGIN")
                try:
                    body(ex)
                    ex.run("COMMIT")
                except BaseException as exc:  # noqa: BLE001 — re-raised below
                    with contextlib.suppress(
                        Exception
                    ):  # a failed COMMIT already rolled back
                        ex.run("ROLLBACK")
                    failure = exc
            if failure is None:
                return
            if not _retryable(failure, attempt):
                raise failure
            time.sleep(_backoff(attempt))
            attempt += 1

    def recreate(self) -> None:
        self._transaction(lambda ex: ex.run(f"DROP TABLE IF EXISTS {TABLE}"))

    # ── writes ───────────────────────────────────────────────────────────────

    def upsert(self, records: list[Record]) -> None:
        if not records:
            return
        dims = {len(r.embedding) for r in records}
        if len(dims) != 1:
            raise CribUserError(f"one upsert mixed vector dimensions {sorted(dims)}")
        (dim,) = dims
        rows = [
            [
                r.id,
                r.document,
                json.dumps(r.metadata),
                *_promoted(r.metadata),
                [float(x) for x in r.embedding],
            ]
            for r in records
        ]
        ids = [r.id for r in records]

        def write(ex: Any) -> None:
            self._ensure_table(ex, dim)
            ex.run(f"DELETE FROM {TABLE} WHERE id = ANY(?)", [ids])
            # TABLE is a module constant; every value is a parameter.
            # pi-lens-ignore: python-sql-injection
            ex.run_many(f"INSERT INTO {TABLE} VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

        self._transaction(write)

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        wanted = list(ids)

        def write(ex: Any) -> None:
            if self._table_dim(ex) is not None:
                ex.run(f"DELETE FROM {TABLE} WHERE id = ANY(?)", [wanted])

        self._transaction(write)

    def set_meta(self, updates: dict[str, dict[str, Any]]) -> None:
        """REPLACE each record's metadata (the contract): keys absent from the new
        dict are gone. Vectors and documents are untouched."""
        if not updates:
            return
        rows = [[json.dumps(m), *_promoted(m), i] for i, m in updates.items()]

        def write(ex: Any) -> None:
            if self._table_dim(ex) is None:
                return
            # TABLE is a module constant; every value is a parameter.
            # pi-lens-ignore: python-sql-injection
            ex.run_many(
                f"UPDATE {TABLE} SET meta = ?, project = ?, store = ?, relpath = ? "
                "WHERE id = ?",
                rows,
            )

        self._transaction(write)

    # ── reads ────────────────────────────────────────────────────────────────

    def _select(self, which: str, where: dict[str, Any] | None) -> list[tuple]:
        columns = _SELECT_COLUMNS[which]  # allowlisted: never caller text
        with self._reading() as ex:
            if self._table_dim(ex) is None:
                return []
            clause, params = _where(where)
            # columns/TABLE are constants and `clause` is built from constant
            # column names and `?` placeholders only; every value is in `params`.
            # pi-lens-ignore: python-sql-injection
            return ex.run(f"SELECT {columns} FROM {TABLE}{clause}", params)

    def get_meta(self, where: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {i: json.loads(m) for i, m in self._select("meta", where)}

    def get_docs(self, where: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
        return {i: (d, json.loads(m)) for i, d, m in self._select("docs", where)}

    # ── the embedding cache (crib.embedcache) ────────────────────────────────

    def ec_get(self, model: str, hashes: list[str]) -> dict[str, list[float]]:
        if not hashes:
            return {}
        with self._reading() as ex:
            if not ex.run(
                "SELECT 1 FROM duckdb_tables() WHERE table_name = ?", [EMBED_CACHE]
            ):
                return {}
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                f"SELECT h, vec FROM {EMBED_CACHE} WHERE model = ? AND h = ANY(?)",
                [model, list(hashes)],
            )
        return {h: [float(x) for x in v] for h, v in rows}

    def ec_put(self, model: str, rows: list[tuple[str, list[float]]]) -> None:
        if not rows:
            return
        data = [[model, h, [float(x) for x in v]] for h, v in rows]

        def write(ex: Any) -> None:
            ex.run(
                f"CREATE TABLE IF NOT EXISTS {EMBED_CACHE} ("
                "model VARCHAR, h VARCHAR, vec FLOAT[], PRIMARY KEY (model, h))"
            )
            # pi-lens-ignore: python-sql-injection
            ex.run(
                f"DELETE FROM {EMBED_CACHE} WHERE model = ? AND h = ANY(?)",
                [model, [h for h, _ in rows]],
            )
            # pi-lens-ignore: python-sql-injection
            ex.run_many(f"INSERT INTO {EMBED_CACHE} VALUES (?, ?, ?)", data)

        self._transaction(write)

    # ── the derived-index record cache (crib.recordcache) ────────────────────

    def rc_load(self, key: str) -> dict[str, tuple[int, int, str]]:
        with self._reading() as ex:
            if not ex.run(
                "SELECT 1 FROM duckdb_tables() WHERE table_name = ?", [RECORD_CACHE]
            ):
                return {}
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                f"SELECT name, mtime_ns, size, rec FROM {RECORD_CACHE} WHERE key = ?",
                [key],
            )
        return {n: (int(m), int(s), r) for n, m, s, r in rows}

    def rc_put(self, key: str, rows: list[tuple[str, int, int, str]]) -> None:
        if not rows:
            return
        data = [[key, n, int(m), int(s), r] for n, m, s, r in rows]

        def write(ex: Any) -> None:
            ex.run(
                f"CREATE TABLE IF NOT EXISTS {RECORD_CACHE} ("
                "key VARCHAR, name VARCHAR, mtime_ns BIGINT, size BIGINT, rec VARCHAR, "
                "PRIMARY KEY (key, name))"
            )
            # pi-lens-ignore: python-sql-injection
            ex.run(
                f"DELETE FROM {RECORD_CACHE} WHERE key = ? AND name = ANY(?)",
                [key, [r[0] for r in rows]],
            )
            # pi-lens-ignore: python-sql-injection
            ex.run_many(f"INSERT INTO {RECORD_CACHE} VALUES (?, ?, ?, ?, ?)", data)

        self._transaction(write)

    def rc_delete(self, key: str, names: list[str]) -> None:
        if not names:
            return

        def write(ex: Any) -> None:
            if ex.run(
                "SELECT 1 FROM duckdb_tables() WHERE table_name = ?", [RECORD_CACHE]
            ):
                # pi-lens-ignore: python-sql-injection
                ex.run(
                    f"DELETE FROM {RECORD_CACHE} WHERE key = ? AND name = ANY(?)",
                    [key, list(names)],
                )

        self._transaction(write)

    # ── the generation ledger (crib.genledger) ───────────────────────────────

    def _ensure_ledger(self, ex: Any) -> None:
        ex.run(
            f"CREATE TABLE IF NOT EXISTS {LEDGER} ("
            "purpose VARCHAR, target VARCHAR, input_hash VARCHAR, provider VARCHAR, "
            "attempts INTEGER, last_error VARCHAR, last_attempt DOUBLE, "
            "next_retry DOUBLE, PRIMARY KEY (purpose, target))"
        )

    def ledger_rows(self, purpose: str, targets: list[str]) -> dict[str, Any]:
        from .genledger import Entry

        if not targets:
            return {}
        with self._reading() as ex:
            if not ex.run(
                "SELECT 1 FROM duckdb_tables() WHERE table_name = ?", [LEDGER]
            ):
                return {}
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                f"SELECT purpose, target, input_hash, provider, attempts, last_error, "
                f"last_attempt, next_retry FROM {LEDGER} "
                "WHERE purpose = ? AND target = ANY(?)",
                [purpose, list(targets)],
            )
        return {r[1]: Entry(*r) for r in rows}

    def ledger_put(self, entries: list[Any]) -> None:
        if not entries:
            return
        rows = [
            [
                e.purpose,
                e.target,
                e.input_hash,
                e.provider,
                int(e.attempts),
                e.last_error,
                float(e.last_attempt),
                float(e.next_retry),
            ]
            for e in entries
        ]

        def write(ex: Any) -> None:
            self._ensure_ledger(ex)
            for purpose in {e.purpose for e in entries}:
                # pi-lens-ignore: python-sql-injection
                ex.run(
                    f"DELETE FROM {LEDGER} WHERE purpose = ? AND target = ANY(?)",
                    [purpose, [e.target for e in entries if e.purpose == purpose]],
                )
            # pi-lens-ignore: python-sql-injection
            ex.run_many(f"INSERT INTO {LEDGER} VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)

        self._transaction(write)

    def ledger_delete(self, purpose: str, targets: list[str]) -> None:
        if not targets:
            return

        def write(ex: Any) -> None:
            self._ensure_ledger(ex)
            # pi-lens-ignore: python-sql-injection
            ex.run(
                f"DELETE FROM {LEDGER} WHERE purpose = ? AND target = ANY(?)",
                [purpose, list(targets)],
            )

        self._transaction(write)

    def ledger_counts(self, now: float) -> dict[str, dict[str, int]]:
        with self._reading() as ex:
            if not ex.run(
                "SELECT 1 FROM duckdb_tables() WHERE table_name = ?", [LEDGER]
            ):
                return {}
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                f"SELECT purpose, count(*), count(*) FILTER (WHERE next_retry > ?) "
                f"FROM {LEDGER} GROUP BY purpose ORDER BY purpose",
                [float(now)],
            )
        return {p: {"failing": int(n), "backing_off": int(b)} for p, n, b in rows}

    def iter_records(self, batch_size: int = 500) -> Iterator[list[Record]]:
        """Pages by id (keyset, not OFFSET): each page is one range scan, and a
        record written mid-export is either in or out, never twice."""
        after = ""
        while page := self._page(after, batch_size):
            yield page
            after = page[-1].id

    def _page(self, after: str, n: int) -> list[Record]:
        """The next `n` records by id after `after` (one read: retryable)."""
        with self._reading() as ex:
            if self._table_dim(ex) is None:
                return []
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                f"SELECT id, vec, document, meta FROM {TABLE} "
                "WHERE id > ? ORDER BY id LIMIT ?",
                [after, int(n)],
            )
        return [
            Record(i, [float(x) for x in v], d, json.loads(m)) for i, v, d, m in rows
        ]

    def query(
        self, embedding: list[float], k: int, where: dict[str, Any] | None = None
    ) -> list[Hit]:
        with self._reading() as ex:
            dim = self._table_dim(ex)
            if dim is None or k <= 0:
                return []
            if len(embedding) != dim:
                raise CribUserError(
                    f"embedding dimension mismatch: query has {len(embedding)}, "
                    f"stored vectors have {dim} — the store holds vectors from a "
                    "different embedder; run `crib project reconcile` to re-embed "
                    "at the current dimension"
                )
            clause, params = _where(where)
            # Ties break on id so the order is total: retrieval stays deterministic.
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                f"SELECT id, document, meta, "
                f"array_cosine_similarity(vec, ?::FLOAT[{int(dim)}]) AS score "
                f"FROM {TABLE}{clause} ORDER BY score DESC, id LIMIT ?",
                [[float(x) for x in embedding], *params, int(k)],
            )
        return [Hit(i, d, json.loads(m), float(s)) for i, d, m, s in rows]
