"""DuckDB vector store — the embedded path (one process owns the file).

Exact search: every filtered query scores the candidate rows with
`array_cosine_similarity` and keeps the top k. At crib's scale (thousands of
chunks) that is about a millisecond, never misses, and needs no extension; an
approximate index is a benchmarked option, not the default (design: "Vector store
moves to DuckDB on two paths").

Layout: one table, created at the first upsert because its vector column is
fixed-size (`FLOAT[dim]`), the dimension of whatever embedder wrote first. The
keys crib filters on constantly (`project`, `kind`, `relpath`, `item_id`) are promoted to
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
from .items import FACETS, Edge, Item, facet_table
from .store import Hit, Record, _check_edge_where, _check_item_where

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
# Versions and identities the rest of the file depends on: schema_version (bump it
# when any table's layout changes; a mismatch drops every crib table, since all
# of them are rebuildable), record_format (crib.recordcache's parse output) and
# embed_model (which embedder wrote crib_chunks; see `Crib._maybe_recreate_for_dim`).
META = "crib_meta"
# 2: crib_chunks' `store` column is `kind`; 3: crib_items and the facet tables;
# 4: crib_chunks' `item_id` column (the item a chunk belongs to); 5: crib_edges
SCHEMA_VERSION = "5"
# Links between items (crib.items.Edge), replaced with their source item.
EDGES = "crib_edges"
# One row per item (crib.items), plus one table per kind for that kind's own
# fields, joined on the item id. Created at open; the vectors are variable
# length, so no table depends on the embedder's dimension.
ITEMS = "crib_items"
_ITEM_COLUMNS = (
    "id",
    "kind",
    "project",
    "relpath",
    "title",
    "content_hash",
    "title_vec",
    "body_vec",
)
_FACET_TABLES = MappingProxyType(
    {kind: (facet_table(kind), tuple(fields)) for kind, fields in FACETS.items()}
)
# The tables that don't depend on the embedder's dimension, created at open.
_FIXED_TABLES = MappingProxyType(
    {
        EMBED_CACHE: "model VARCHAR, h VARCHAR, vec FLOAT[], PRIMARY KEY (model, h)",
        RECORD_CACHE: "key VARCHAR, name VARCHAR, mtime_ns BIGINT, size BIGINT, "
        "rec VARCHAR, PRIMARY KEY (key, name)",
        LEDGER: "purpose VARCHAR, target VARCHAR, input_hash VARCHAR, "
        "provider VARCHAR, attempts INTEGER, last_error VARCHAR, "
        "last_attempt DOUBLE, next_retry DOUBLE, PRIMARY KEY (purpose, target)",
        EDGES: "src VARCHAR, dst VARCHAR, kind VARCHAR, recorded_hash VARCHAR",
        ITEMS: "id VARCHAR PRIMARY KEY, kind VARCHAR, project VARCHAR, "
        "relpath VARCHAR, title VARCHAR, content_hash VARCHAR, "
        "title_vec FLOAT[], body_vec FLOAT[]",
        **{
            facet_table(kind): "id VARCHAR PRIMARY KEY, "
            + ", ".join(f"{name} {sqltype}" for name, sqltype in fields.items())
            for kind, fields in FACETS.items()
        },
    }
)
PROMOTED = (
    "project",
    "kind",
    "relpath",
    "item_id",
)  # filtered constantly: real columns
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


def _any_match(key: str, wanted: Any) -> tuple[str, list[Any]]:
    """`{key: {"$any": [...]}}`: the metadata list at `key` shares at least one
    string with `wanted`. A missing key, or one that isn't a list, matches
    nothing (TRY_CAST makes a non-list NULL; AND doesn't short-circuit)."""
    if not isinstance(wanted, (list, tuple)) or not all(
        isinstance(v, str) for v in wanted
    ):
        raise CribUserError(f'"$any" takes a list of strings, got {wanted!r}')
    if not wanted:
        return ("FALSE", [])
    path = _json_path(key)
    return (
        "coalesce(json_type(meta, ?) = 'ARRAY' AND list_has_any("
        "TRY_CAST(json_extract(meta, ?) AS VARCHAR[]), ?::VARCHAR[]), FALSE)",
        [path, path, list(wanted)],
    )


def _in_match(key: str, wanted: Any) -> tuple[str, list[Any]]:
    """`{key: {"$in": [...]}}`: the string at `key` is one of `wanted`."""
    if not isinstance(wanted, (list, tuple)) or not all(
        isinstance(v, str) for v in wanted
    ):
        raise CribUserError(f'"$in" takes a list of strings, got {wanted!r}')
    if not wanted:
        return ("FALSE", [])
    if key in PROMOTED:  # key is one of the constant column names
        return (f"list_contains(?::VARCHAR[], {key})", [list(wanted)])
    path = _json_path(key)
    return (
        "(json_type(meta, ?) = 'VARCHAR' "
        "AND list_contains(?::VARCHAR[], json_extract_string(meta, ?)))",
        [path, list(wanted), path],
    )


def _where(where: dict[str, Any] | None) -> tuple[str, list[Any]]:
    """`WHERE …` for a metadata filter (all keys must match): a plain value is an
    exact, type-checked match; `{"$any": [...]}` is list membership (the stored
    value is a list); `{"$in": [...]}` is one-of (the stored value is a string)."""
    if not where:
        return "", []
    conds: list[str] = []
    params: list[Any] = []
    for key, value in where.items():
        if isinstance(value, dict):
            if set(value) == {"$any"}:
                cond, ps = _any_match(key, value["$any"])
            elif set(value) == {"$in"}:
                cond, ps = _in_match(key, value["$in"])
            else:
                raise CribUserError(f"unsupported filter for {key!r}: {value!r}")
            conds.append(cond)
            params.extend(ps)
        elif key in PROMOTED and isinstance(value, str):
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


def _put_meta(ex: Any, key: str, value: str) -> None:
    ex.run(f"DELETE FROM {META} WHERE key = ?", [key])
    ex.run(f"INSERT INTO {META} VALUES (?, ?)", [key, value])


def _dim_of(data_type: str) -> int:
    """'FLOAT[384]' -> 384."""
    return int(data_type.rsplit("[", 1)[1].rstrip("]"))


def _item_clause(where: dict[str, Any]) -> tuple[str, list[Any]]:
    """`WHERE …` over crib_items (aliased `i`) for an items/item_hashes filter."""
    _check_item_where(where)
    conds, params = [], []
    for key, value in where.items():  # key is one of the ITEM_KEYS columns
        if isinstance(value, dict):
            (op, wanted), = value.items()
            if op != "$in":
                raise CribUserError(f"unsupported item filter {value!r}")
            conds.append(f"list_contains(?::VARCHAR[], i.{key})")
            params.append(list(wanted))
        else:
            conds.append(f"i.{key} = ?")
            params.append(value)
    return ((" WHERE " + " AND ".join(conds)) if conds else ""), params


def _delete_items_step(ids: list[str]) -> Callable[[Any], None]:
    def write(ex: Any) -> None:
        for table in (ITEMS, *(t for t, _ in _FACET_TABLES.values())):
            # pi-lens-ignore: python-sql-injection
            ex.run(f"DELETE FROM {table} WHERE id = ANY(?)", [ids])
        # pi-lens-ignore: python-sql-injection
        ex.run(f"DELETE FROM {EDGES} WHERE src = ANY(?)", [ids])

    return write


def _facet_value(value: Any) -> Any:
    return (value or None) if isinstance(value, list) else value


def _insert_items_step(items: list[Item]) -> Callable[[Any], None]:
    core = [
        [
            it.id,
            it.kind,
            it.project,
            it.relpath,
            it.title,
            it.content_hash,
            [float(x) for x in it.title_vec] if it.title_vec is not None else None,
            [float(x) for x in it.body_vec] if it.body_vec is not None else None,
        ]
        for it in items
    ]

    def write(ex: Any) -> None:
        # pi-lens-ignore: python-sql-injection
        ex.run_many(f"INSERT INTO {ITEMS} VALUES (?, ?, ?, ?, ?, ?, ?, ?)", core)
        for kind, (table, fields) in _FACET_TABLES.items():
            # An empty list is stored as NULL (and read back as []): an empty
            # literal has no element type, so it can't cross Quack.
            rows = [
                [it.id, *(_facet_value(it.facet.get(f)) for f in fields)]
                for it in items
                if it.kind == kind
            ]
            if rows:
                marks = ", ".join("?" for _ in range(len(fields) + 1))
                # pi-lens-ignore: python-sql-injection
                ex.run_many(f"INSERT INTO {table} VALUES ({marks})", rows)
        edges = [[it.id, e.dst, e.kind, e.recorded_hash] for it in items for e in it.edges]
        if edges:
            # pi-lens-ignore: python-sql-injection
            ex.run_many(f"INSERT INTO {EDGES} VALUES (?, ?, ?, ?)", edges)

    return write


def _upsert_step(store: "DuckDBStore", records: list[Record]) -> Callable[[Any], None]:
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
        store._ensure_table(ex, dim)
        ex.run(f"DELETE FROM {TABLE} WHERE id = ANY(?)", [ids])
        # TABLE is a module constant; every value is a parameter.
        # pi-lens-ignore: python-sql-injection
        ex.run_many(f"INSERT INTO {TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)

    return write


_META_ROWS_PER_STATEMENT = 200


def _set_meta_step(
    store: "DuckDBStore", updates: dict[str, dict[str, Any]]
) -> Callable[[Any], None]:
    """One UPDATE … FROM (VALUES …) per batch, not one UPDATE per row (which
    over Quack was a round trip each)."""
    rows = [[i, json.dumps(m), *_promoted(m)] for i, m in updates.items()]

    def write(ex: Any) -> None:
        if store._table_dim(ex) is None:
            return
        for start in range(0, len(rows), _META_ROWS_PER_STATEMENT):
            batch = rows[start : start + _META_ROWS_PER_STATEMENT]
            groups = ", ".join("(?, ?, ?, ?, ?, ?)" for _ in batch)
            # TABLE is a module constant; every value is a parameter.
            # pi-lens-ignore: python-sql-injection
            ex.run(
                f"UPDATE {TABLE} SET meta = v.meta::JSON, project = v.project, "
                "kind = v.kind, relpath = v.relpath, item_id = v.item_id "
                f"FROM (VALUES {groups}) "
                "AS v(id, meta, project, kind, relpath, item_id) "
                f"WHERE {TABLE}.id = v.id",
                [x for row in batch for x in row],
            )

    return write


def _backfill_labels(store: "DuckDBStore", ex: Any) -> None:
    """Chunks written before 0.22.0 kept `tags` as a comma-joined string and had
    no `labels` list, so a `tags=` lookup (which filters on `labels` in the
    query) would miss them until the next reconcile re-stamped them. Convert
    them here, at open, the way `Chunk.metadata` now writes them."""
    if store._table_dim(ex) is None:
        return
    # pi-lens-ignore: python-sql-injection
    rows = ex.run(
        f"SELECT id, meta FROM {TABLE} WHERE json_type(meta, '$.tags') = 'VARCHAR' "
        "AND json_type(meta, '$.labels') IS NULL"
    )
    if not rows:
        return
    updates: dict[str, dict[str, Any]] = {}
    for cid, raw in rows:
        m = json.loads(raw)
        tags = [t for t in str(m.get("tags") or "").split(",") if t]
        m["tags"] = tags
        m["labels"] = [t for t in dict.fromkeys([*tags, m.get("type") or ""]) if t]
        updates[cid] = m
    _set_meta_step(store, updates)(ex)


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
        self._prepare()

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
        with self._write:  # never close under a writer mid-transaction
            self._conn.close()

    @contextlib.contextmanager
    def _reading(self) -> Iterator[Any]:
        with contextlib.closing(self._conn.cursor()) as cur:
            yield _Bound(cur)

    @contextlib.contextmanager
    def _writing(self) -> Iterator[Any]:
        with self._write, contextlib.closing(self._conn.cursor()) as cur:
            yield _Bound(cur)

    # ── schema ───────────────────────────────────────────────────────────────

    def _prepare(self) -> None:
        """Check the file's versions and create the fixed tables, once at open.

        A file from before crib_meta existed is adopted as version 1, the layout
        it already has. A different schema_version drops every crib table (all
        rebuildable caches); a different record_format empties the record
        cache, whose rows hold the old parse."""
        from .recordcache import RECORD_FORMAT

        def prepare(ex: Any) -> None:
            ex.run(
                f"CREATE TABLE IF NOT EXISTS {META} (key VARCHAR PRIMARY KEY, value VARCHAR)"
            )
            meta = dict(ex.run(f"SELECT key, value FROM {META}"))
            if meta.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
                for table in (TABLE, *_FIXED_TABLES):
                    ex.run(f"DROP TABLE IF EXISTS {table}")
                ex.run(f"DELETE FROM {META}")
                meta = {}
            for table, columns in _FIXED_TABLES.items():
                ex.run(f"CREATE TABLE IF NOT EXISTS {table} ({columns})")
            if meta.get("record_format", RECORD_FORMAT) != RECORD_FORMAT:
                ex.run(f"DELETE FROM {RECORD_CACHE}")
            for key, value in (
                ("schema_version", SCHEMA_VERSION),
                ("record_format", RECORD_FORMAT),
            ):
                if meta.get(key) != value:
                    _put_meta(ex, key, value)
            _backfill_labels(self, ex)

        self._transaction(prepare)

    def checkpoint(self) -> None:
        """Write the WAL into the database file (DuckDB's CHECKPOINT)."""
        with self._writing() as ex:
            ex.run("CHECKPOINT")

    def stats(self) -> dict[str, Any]:
        """What's in the file, for `status`: rows per crib table, the recorded
        versions, and the file and WAL sizes (embedded only)."""
        with self._reading() as ex:
            tables = {t for (t,) in ex.run("SELECT table_name FROM duckdb_tables()")}
            rows = {}
            for table in (TABLE, EMBED_CACHE, RECORD_CACHE, LEDGER):
                if table in tables:
                    # pi-lens-ignore: python-sql-injection
                    ((n,),) = ex.run(f"SELECT count(*) FROM {table}")
                    rows[table] = int(n)
            meta = dict(ex.run(f"SELECT key, value FROM {META}"))
        out: dict[str, Any] = {"rows": rows, **meta}
        if self._path is not None:
            for label, path in (
                ("file_bytes", self._path),
                ("wal_bytes", self._path.with_name(self._path.name + ".wal")),
            ):
                with contextlib.suppress(OSError):
                    out[label] = path.stat().st_size
        return out

    def meta_get(self, key: str) -> str | None:
        with self._reading() as ex:
            rows = ex.run(f"SELECT value FROM {META} WHERE key = ?", [key])
        return rows[0][0] if rows else None

    def meta_set(self, key: str, value: str) -> None:
        self._transaction(lambda ex: _put_meta(ex, key, value))

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
                "project VARCHAR, kind VARCHAR, relpath VARCHAR, item_id VARCHAR, "
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
        def drop(ex: Any) -> None:
            ex.run(f"DROP TABLE IF EXISTS {TABLE}")
            # item vectors came from the same embedder: empty them too
            for table in (ITEMS, EDGES, *(t for t, _ in _FACET_TABLES.values())):
                # pi-lens-ignore: python-sql-injection
                ex.run(f"DELETE FROM {table}")

        self._transaction(drop)

    # ── writes ───────────────────────────────────────────────────────────────

    def upsert(self, records: list[Record]) -> None:
        self.apply(records, [], {})

    def delete(self, ids: list[str]) -> None:
        self.apply([], ids, {})

    def set_meta(self, updates: dict[str, dict[str, Any]]) -> None:
        """REPLACE each record's metadata (the contract): keys absent from the new
        dict are gone. Vectors and documents are untouched."""
        self.apply([], [], updates)

    def apply(
        self,
        records: list[Record],
        deletes: list[str],
        meta: dict[str, dict[str, Any]],
        items: list[Item] | None = None,
        item_deletes: list[str] | None = None,
    ) -> None:
        """Upserts, deletes and metadata replacements, and the files' item rows,
        as ONE transaction: a note write never leaves its new chunks beside its
        stale ones (or an item row that disagrees with them), and over Quack it
        costs one BEGIN/COMMIT instead of several."""
        steps: list[Callable[[Any], None]] = []
        if records:
            steps.append(_upsert_step(self, records))
        if deletes:
            wanted = list(deletes)

            def delete(ex: Any) -> None:
                if self._table_dim(ex) is not None:
                    ex.run(f"DELETE FROM {TABLE} WHERE id = ANY(?)", [wanted])

            steps.append(delete)
        if meta:
            steps.append(_set_meta_step(self, meta))
        drop = list(item_deletes or ()) + [it.id for it in items or ()]
        if drop:
            steps.append(_delete_items_step(drop))
        if items:
            steps.append(_insert_items_step(list(items)))
        if not steps:
            return

        def write(ex: Any) -> None:
            for step in steps:
                step(ex)

        self._transaction(write)

    # ── reads ────────────────────────────────────────────────────────────────

    def edges(self, where: dict[str, Any]) -> list[tuple[str, str, str, str | None]]:
        _check_edge_where(where)
        conds, params = [], []
        for key, value in where.items():  # key is one of the EDGE_KEYS columns
            col = "r.kind" if key == "kind" else f"r.{key}"
            if isinstance(value, dict):
                (op, wanted), = value.items()
                if op != "$in":
                    raise CribUserError(f"unsupported edge filter {value!r}")
                conds.append(f"list_contains(?::VARCHAR[], {col})")
                params.append(list(wanted))
            else:
                conds.append(f"{col} = ?")
                params.append(value)
        clause = (" WHERE " + " AND ".join(conds)) if conds else ""
        with self._reading() as ex:
            # A `dep` edge takes its target's default kind (crib.items.dep_kind_default).
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                "SELECT r.src, r.dst, r.kind, r.recorded_hash FROM ("
                "SELECT e.src, e.dst, e.recorded_hash, CASE WHEN e.kind = 'dep' THEN "
                "CASE t.kind WHEN 'plan' THEN 'follows' WHEN 'design' THEN 'rests_on' "
                "ELSE 'informed_by' END ELSE e.kind END AS kind "
                f"FROM {EDGES} e LEFT JOIN {ITEMS} t ON t.id = e.dst) r{clause} "
                "ORDER BY r.src, r.dst, r.kind",
                params,
            )
        return [(a, b, c, d) for a, b, c, d in rows]

    def item_hashes(self, where: dict[str, Any]) -> dict[str, str]:
        clause, params = _item_clause(where)
        with self._reading() as ex:
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(f"SELECT i.id, i.content_hash FROM {ITEMS} i{clause}", params)
        return {i: h for i, h in rows}

    def items(self, where: dict[str, Any]) -> dict[str, Item]:
        clause, params = _item_clause(where)
        with self._reading() as ex:
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                f"SELECT {', '.join('i.' + c for c in _ITEM_COLUMNS)} "
                f"FROM {ITEMS} i{clause}",
                params,
            )
            out = {r[0]: r for r in rows}
            facets: dict[str, dict[str, Any]] = {}
            for kind, (table, fields) in _FACET_TABLES.items():
                ids = [i for i, r in out.items() if r[1] == kind]
                if not ids:
                    continue
                # pi-lens-ignore: python-sql-injection
                for row in ex.run(
                    f"SELECT id, {', '.join(fields)} FROM {table} WHERE id = ANY(?)",
                    [ids],
                ):
                    types = FACETS[kind]
                    facets[row[0]] = {
                        f: (list(v or []) if types[f] == "VARCHAR[]" else v)
                        for f, v in zip(fields, row[1:])
                    }
            edge_rows = (
                # pi-lens-ignore: python-sql-injection
                ex.run(
                    f"SELECT src, dst, kind, recorded_hash FROM {EDGES} "
                    "WHERE src = ANY(?) ORDER BY rowid",
                    [list(out)],
                )
                if out
                else []
            )
        edges_of: dict[str, list[Edge]] = {}
        for src, dst, kind, rh in edge_rows:
            edges_of.setdefault(src, []).append(Edge(dst, kind, rh))
        return {
            i: Item(
                id=r[0],
                kind=r[1],
                project=r[2],
                relpath=r[3],
                title=r[4],
                content_hash=r[5],
                title_vec=[float(x) for x in r[6]] if r[6] is not None else None,
                body_vec=[float(x) for x in r[7]] if r[7] is not None else None,
                facet=facets.get(i, {}),
                edges=tuple(edges_of.get(i, ())),
            )
            for i, r in out.items()
        }

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

    def count(self, where: dict[str, Any], relpath_prefix: str = "") -> int:
        with self._reading() as ex:
            if self._table_dim(ex) is None:
                return 0
            clause, params = _where(where)
            if relpath_prefix:
                clause += (" AND " if clause else " WHERE ") + "starts_with(relpath, ?)"
                params = [*params, relpath_prefix]
            # pi-lens-ignore: python-sql-injection
            ((n,),) = ex.run(f"SELECT count(*) FROM {TABLE}{clause}", params)
        return int(n)

    def projects(self) -> set[str]:
        with self._reading() as ex:
            if self._table_dim(ex) is None:
                return set()
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                f"SELECT DISTINCT project FROM {TABLE} WHERE project IS NOT NULL"
            )
        return {p for (p,) in rows}

    # ── the embedding cache (crib.embedcache) ────────────────────────────────

    def ec_get(self, model: str, hashes: list[str]) -> dict[str, list[float]]:
        if not hashes:
            return {}
        with self._reading() as ex:
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                f"SELECT h, vec FROM {EMBED_CACHE} WHERE model = ? AND h = ANY(?)",
                [model, list(hashes)],
            )
        return {h: [float(x) for x in v] for h, v in rows}

    def ec_prune(self, keep_model: str) -> int:
        """Drop cached vectors from every embedder but `keep_model`; how many."""

        def write(ex: Any) -> None:
            # pi-lens-ignore: python-sql-injection
            ((n,),) = ex.run(
                f"SELECT count(*) FROM {EMBED_CACHE} WHERE model <> ?", [keep_model]
            )
            dropped[0] = int(n)
            if n:
                # pi-lens-ignore: python-sql-injection
                ex.run(f"DELETE FROM {EMBED_CACHE} WHERE model <> ?", [keep_model])

        dropped = [0]
        self._transaction(write)
        return dropped[0]

    def ec_put(self, model: str, rows: list[tuple[str, list[float]]]) -> None:
        if not rows:
            return
        data = [[model, h, [float(x) for x in v]] for h, v in rows]

        def write(ex: Any) -> None:
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
            # pi-lens-ignore: python-sql-injection
            ex.run(
                f"DELETE FROM {RECORD_CACHE} WHERE key = ? AND name = ANY(?)",
                [key, list(names)],
            )

        self._transaction(write)

    # ── the generation ledger (crib.genledger) ───────────────────────────────

    def ledger_rows(self, purpose: str, targets: list[str]) -> dict[str, Any]:
        from .genledger import Entry

        if not targets:
            return {}
        with self._reading() as ex:
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
            # pi-lens-ignore: python-sql-injection
            ex.run(
                f"DELETE FROM {LEDGER} WHERE purpose = ? AND target = ANY(?)",
                [purpose, list(targets)],
            )

        self._transaction(write)

    def ledger_prune(self, purpose: str, prefix: str, keep: list[str]) -> None:
        """Drop `purpose` rows under `prefix` whose target isn't in `keep`: the
        file or section was deleted, renamed or since succeeded elsewhere."""

        def write(ex: Any) -> None:
            # pi-lens-ignore: python-sql-injection
            ex.run(
                f"DELETE FROM {LEDGER} WHERE purpose = ? AND starts_with(target, ?) "
                "AND NOT (target = ANY(?))",
                [purpose, prefix, list(keep)],
            )

        self._transaction(write)

    def ledger_counts(self, now: float) -> dict[str, dict[str, int]]:
        with self._reading() as ex:
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                f"SELECT purpose, count(*), count(*) FILTER (WHERE next_retry > ?) "
                f"FROM {LEDGER} GROUP BY purpose ORDER BY purpose",
                [float(now)],
            )
        return {p: {"failing": int(n), "backing_off": int(b)} for p, n, b in rows}

    def score_ids(self, embedding: list[float], ids: list[str]) -> dict[str, float]:
        if not ids:
            return {}
        with self._reading() as ex:
            dim = self._table_dim(ex)
            if dim is None:
                return {}
            if len(embedding) != dim:
                raise CribUserError(
                    f"embedding dimension mismatch: query has {len(embedding)}, "
                    f"stored vectors have {dim}; run `crib project reconcile`"
                )
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                f"SELECT id, array_cosine_similarity(vec, ?::FLOAT[{int(dim)}]) "
                f"FROM {TABLE} WHERE id = ANY(?)",
                [[float(x) for x in embedding], list(ids)],
            )
        # A zero vector's NaN scores 0.0, as in `query`.
        return {i: (0.0 if s != s else float(s)) for i, s in rows}

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
            # A zero vector's cosine is NaN, which DuckDB sorts ABOVE 1.0; score it
            # 0.0 instead, as the in-memory store does.
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                f"SELECT id, document, meta, CASE WHEN isnan(s) THEN 0.0 ELSE s END "
                f"AS score FROM (SELECT id, document, meta, "
                f"array_cosine_similarity(vec, ?::FLOAT[{int(dim)}]) AS s "
                f"FROM {TABLE}{clause}) ORDER BY score DESC, id LIMIT ?",
                [[float(x) for x in embedding], *params, int(k)],
            )
        return [Hit(i, d, json.loads(m), float(s)) for i, d, m, s in rows]
