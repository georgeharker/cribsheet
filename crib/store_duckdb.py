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
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .errors import CribUserError
from .items import FACETS, Edge, Item, facet_table
from .retrieve import PostingStats
from .store import Hit, Record, _check_edge_where, _check_item_where

TABLE = "crib_chunks"
# The generation ledger (crib.genledger): beside the chunks, NOT part of them, so a
# vector rebuild (recreate) keeps it.
LEDGER = "crib_gen_ledger"
# Tables an older crib made and this one no longer does: dropped at open.
# crib_record_cache held parsed symbol records; the symbol items replaced it.
_RETIRED_TABLES = ("crib_record_cache",)
_RETIRED_META = ("record_format",)
# The embedding cache (crib.embedcache): text hash -> vector per model; plain
# vectors, no index (search builds what it needs in memory).
EMBED_CACHE = "crib_embed_cache"
# Versions and identities the rest of the file depends on: schema_version (bump it
# when any table's layout changes; a mismatch drops every crib table, since all
# of them are rebuildable) and embed_model (which embedder wrote crib_chunks; see `Crib._maybe_recreate_for_dim`).
META = "crib_meta"
# Kept when the schema version changes: rows keyed by what they are (model and
# text; purpose and target), not by any layout of the other tables.
_KEPT_ACROSS_VERSIONS = (EMBED_CACHE, LEDGER, META)
# How long a conversion waits out other connections' conflicting writes.
MIGRATION_WAIT_S = 120.0
# 2: crib_chunks' `store` column is `kind`; 3: crib_items and the facet tables;
# 4: crib_chunks' `item_id` column (the item a chunk belongs to); 5: crib_edges;
# 6: crib_postings; 7: crib_files and crib_items.embed_text; 8: the symbol
# facet's legacy `fqname`; 9: design/plan facets' body_hash, rank (design),
# trigger as JSON; 10: the keywords and summary facets (crib.enrichments)
SCHEMA_VERSION = "10"
# An item's lexical postings (crib.items.Item.terms), replaced with the item.
POSTINGS = "crib_postings"
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
    "embed_text",
)
# Per facet and file: what the file's stamp was when it was last mapped into the
# store, and the items it produced (crib.filesync). Change detection, nothing more.
FILES = "crib_files"
_FACET_TABLES = MappingProxyType(
    {kind: (facet_table(kind), tuple(fields)) for kind, fields in FACETS.items()}
)
# The tables that don't depend on the embedder's dimension, created at open.
_FIXED_TABLES = MappingProxyType(
    {
        EMBED_CACHE: "model VARCHAR, h VARCHAR, vec FLOAT[], PRIMARY KEY (model, h)",
        LEDGER: "purpose VARCHAR, target VARCHAR, input_hash VARCHAR, "
        "provider VARCHAR, attempts INTEGER, last_error VARCHAR, "
        "last_attempt DOUBLE, next_retry DOUBLE, PRIMARY KEY (purpose, target)",
        EDGES: "src VARCHAR, dst VARCHAR, kind VARCHAR, recorded_hash VARCHAR",
        POSTINGS: "item_id VARCHAR, term VARCHAR, tf DOUBLE",
        ITEMS: "id VARCHAR PRIMARY KEY, kind VARCHAR, project VARCHAR, "
        "relpath VARCHAR, title VARCHAR, content_hash VARCHAR, "
        "title_vec FLOAT[], body_vec FLOAT[], embed_text VARCHAR",
        FILES: "facet VARCHAR, project VARCHAR, file VARCHAR, mtime_ns BIGINT, "
        "size BIGINT, item_ids VARCHAR[], PRIMARY KEY (facet, project, file)",
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


# What DuckDB says about a file it cannot use AS a database (as opposed to one it
# can't reach: a lock, a permission), matched case-insensitively.
_UNREADABLE = ("corrupt", "checksum", "not a valid", "invalid database",
               "serializ", "storage version", "magic bytes")


def _unreadable(exc: BaseException) -> bool:
    """Is `exc` DuckDB saying the file itself is unusable (so it can be set
    aside and rebuilt), rather than that it is locked or not permitted?"""
    import duckdb  # lazy

    if not isinstance(exc, duckdb.Error):
        return False
    msg = str(exc).lower()
    if "lock" in msg or "permission" in msg:
        return False
    return isinstance(exc, (duckdb.SerializationException, duckdb.InternalException)) or any(
        w in msg for w in _UNREADABLE)


def set_aside(path: Path, why: BaseException) -> Path:
    """Move an unusable store file (and its WAL) out of the way, keeping it as
    `<name>.broken-<stamp>`: the store is a cache rebuilt from the files, so a
    fresh one costs a rebuild, and the old one stays for inspection."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    kept = path.with_name(f"{path.name}.broken-{stamp}")
    for p, dest in ((path, kept),
                    (path.with_name(path.name + ".wal"),
                     path.with_name(f"{path.name}.wal.broken-{stamp}"))):
        if p.exists():
            p.rename(dest)
    print(f"[crib] the DuckDB store {path} could not be opened ({why}); it was moved "
          f"to {kept} and a fresh store is being built from your files",
          file=sys.stderr)
    return kept


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
        """`sql` once per row. An `INSERT … VALUES (?, …)` runs as multi-row
        statements, `_ROWS_PER_STATEMENT` rows at a time, still bound: DuckDB's
        executemany goes row by row (about 5x slower on a cold symbol refresh)."""
        m = _VALUES_TAIL.match(sql)
        if m is None:
            # pi-lens-ignore: python-sql-injection
            self._cur.executemany(sql, rows)
            return
        head, group = m["head"], m["group"]
        for start in range(0, len(rows), _ROWS_PER_STATEMENT):
            batch = rows[start : start + _ROWS_PER_STATEMENT]
            # pi-lens-ignore: python-sql-injection
            self._cur.execute(head + ", ".join([group] * len(batch)),
                              [v for row in batch for v in row])

    def insert_columns(self, table: str, types: Sequence[str],
                       rows: list[list[Any]]) -> None:
        """Insert `rows` into `table`, whose columns have `types`, as ONE
        columnar statement: each column bound as one typed list and unnested
        side by side (in row order; DuckDB keeps insertion order). About 7x
        faster than multi-row VALUES on a cold refresh's item rows."""
        if not rows:
            return
        cols = [list(c) for c in zip(*rows)]
        sel = ", ".join(f"unnest(?::{t}[])" for t in types)
        # pi-lens-ignore: python-sql-injection
        self._cur.execute(f"INSERT INTO {table} SELECT {sel}", cols)


# An INSERT whose tail is one `VALUES (?, …)` group: both executors batch it
# into multi-row statements (bound here, rendered as literals over Quack).
_VALUES_TAIL = re.compile(
    r"^(?P<head>.*\bVALUES\s*)(?P<group>\(\s*\?(?:\s*,\s*\?)*\s*\))\s*$", re.S
)
_ROWS_PER_STATEMENT = 200


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


def _carry_vectors_step(items: list[Item]) -> Callable[[Any], None]:
    """Before items are rewritten: a new row with no body vector, whose
    embed_text is unchanged from the stored row, keeps the stored vector. (The
    vector step only ever embeds text that changed.) Mutates `items` in place."""

    def run(ex: Any) -> None:
        from dataclasses import replace

        want = [it.id for it in items if it.body_vec is None and it.embed_text]
        if not want:
            return
        # pi-lens-ignore: python-sql-injection
        old = {i: (t, v) for i, t, v in ex.run(
            f"SELECT id, embed_text, body_vec FROM {ITEMS} WHERE id = ANY(?) "
            "AND body_vec IS NOT NULL",
            [want],
        )}
        for n, it in enumerate(items):
            o = old.get(it.id)
            if o is not None and it.body_vec is None and o[0] == it.embed_text:
                items[n] = replace(it, body_vec=[float(x) for x in o[1]])

    return run


def _delete_items_step(ids: list[str]) -> Callable[[Any], None]:
    def write(ex: Any) -> None:
        for table in (ITEMS, *(t for t, _ in _FACET_TABLES.values())):
            # pi-lens-ignore: python-sql-injection
            ex.run(f"DELETE FROM {table} WHERE id = ANY(?)", [ids])
        # pi-lens-ignore: python-sql-injection
        ex.run(f"DELETE FROM {EDGES} WHERE src = ANY(?)", [ids])
        # pi-lens-ignore: python-sql-injection
        ex.run(f"DELETE FROM {POSTINGS} WHERE item_id = ANY(?)", [ids])

    return write


# The column types of the tables the item steps insert into, in column order
# (Executor.insert_columns binds each column as one typed list).
_ITEM_TYPES = ("VARCHAR",) * 6 + ("FLOAT[]", "FLOAT[]", "VARCHAR")
_POSTING_TYPES = ("VARCHAR", "VARCHAR", "DOUBLE")
_EDGE_TYPES = ("VARCHAR",) * 4


def _insert_items_step(items: list[Item]) -> Callable[[Any], None]:
    """Insert `items`. The rows are built when the step RUNS, not when it is
    made: `_carry_vectors_step`, earlier in the same transaction, may have just
    filled body vectors into the list."""

    def write(ex: Any) -> None:
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
                it.embed_text,
            ]
            for it in items
        ]
        ex.insert_columns(ITEMS, _ITEM_TYPES, core)
        for kind, (table, fields) in _FACET_TABLES.items():
            rows = [
                [it.id, *(it.facet.get(f) for f in fields)]
                for it in items
                if it.kind == kind
            ]
            types = ("VARCHAR", *FACETS[kind].values())
            ex.insert_columns(table, types, rows)
        postings = [[it.id, t, float(f)] for it in items for t, f in it.terms]
        ex.insert_columns(POSTINGS, _POSTING_TYPES, postings)
        edges = [[it.id, e.dst, e.kind, e.recorded_hash] for it in items for e in it.edges]
        ex.insert_columns(EDGES, _EDGE_TYPES, edges)

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


@contextlib.contextmanager
def _migration_lock(path: Path | None) -> Iterator[None]:
    """Exclusive, machine-wide: one crib process converts the store at a time
    (every crib process on a machine shares the store's directory; the shared
    server is loopback-only). No path (an in-memory store): no lock needed."""
    if path is None:
        yield
        return
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        said = False
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not said:
                    print("[crib] waiting for another crib process to finish "
                          "converting the store", file=sys.stderr)
                    said = True
                time.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _backfill_pending(store: "DuckDBStore", ex: Any) -> bool:
    if store._table_dim(ex) is None:
        return False
    # pi-lens-ignore: python-sql-injection
    return bool(ex.run(
        f"SELECT 1 FROM {TABLE} WHERE json_type(meta, '$.tags') = 'VARCHAR' "
        "AND json_type(meta, '$.labels') IS NULL LIMIT 1"))


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

        def connect() -> Any:
            try:
                return duckdb.connect(str(path), config=local_dirs(path.parent))
            except duckdb.IOException as exc:
                if "lock" in str(exc).lower():
                    raise _lock_error(path, exc) from exc
                raise

        try:
            conn = connect()
        except duckdb.Error as exc:
            if not _unreadable(exc):
                raise
            set_aside(path, exc)
            conn = connect()
        try:
            return cls(conn, path)
        except duckdb.Error as exc:  # opened, but its contents can't be used
            conn.close()
            if not _unreadable(exc):
                raise
            set_aside(path, exc)
            return cls(connect(), path)

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
        """Bring the file to this version's layout, once at open — safely.

        A normal open WRITES NOTHING: `_schema_current` reads the version, the
        tables and what is pending, and when nothing needs doing that is the
        end of it, so an open can't conflict with the store's other users.
        Only a real change (a new file, a version change, a retired table, a
        backfill) writes, and then:

        - under a machine-wide lock beside the store (`_migration_lock`), so
          one crib process converts and the rest wait and find it done;
        - as ONE transaction, with the version stamped in it, so a conversion
          is all there or not at all, whatever kills the process;
        - retried while another connection's writes conflict with it, for up
          to MIGRATION_WAIT_S, then refused with what to do, rather than spun
          on silently.

        A different schema_version drops the rebuildable tables, but keeps the
        embedding cache and the generation ledger: their rows are keyed by
        model and text (by purpose and target), not by any layout, so a
        conversion never costs re-embedding or re-generating everything. A
        file from before crib_meta existed is adopted as version 1."""
        if self._schema_current():
            return
        with _migration_lock(self._migration_lock_path()):
            if self._schema_current():
                return  # another process converted it while we waited
            self._converting(self._prepare_step)

    def _prepare_step(self, ex: Any) -> None:
        ex.run(
            f"CREATE TABLE IF NOT EXISTS {META} (key VARCHAR PRIMARY KEY, value VARCHAR)"
        )
        meta = dict(ex.run(f"SELECT key, value FROM {META}"))
        if meta.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
            for table in (TABLE, *_FIXED_TABLES):
                if table not in _KEPT_ACROSS_VERSIONS:
                    ex.run(f"DROP TABLE IF EXISTS {table}")
            ex.run(f"DELETE FROM {META}")
            meta = {}
        for table in _RETIRED_TABLES:
            ex.run(f"DROP TABLE IF EXISTS {table}")
        for key in _RETIRED_META:
            if key in meta:
                ex.run(f"DELETE FROM {META} WHERE key = ?", [key])
        for table, columns in _FIXED_TABLES.items():
            ex.run(f"CREATE TABLE IF NOT EXISTS {table} ({columns})")
        if meta.get("schema_version") != SCHEMA_VERSION:
            _put_meta(ex, "schema_version", SCHEMA_VERSION)
        _backfill_labels(self, ex)

    def _schema_current(self) -> bool:
        """Read-only: is the file already this version's layout, with nothing
        to retire or backfill? (A file that can't even be read is not.)"""
        try:
            with self._reading() as ex:
                tables = {t for (t,) in ex.run(
                    "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'")}
                if META not in tables:
                    return False
                meta = dict(ex.run(f"SELECT key, value FROM {META}"))
                if (meta.get("schema_version") != SCHEMA_VERSION
                        or any(t not in tables for t in _FIXED_TABLES)
                        or any(t in tables for t in _RETIRED_TABLES)
                        or any(k in meta for k in _RETIRED_META)):
                    return False
                return not _backfill_pending(self, ex)
        except Exception:  # noqa: BLE001 — anything odd: take the careful path
            return False

    def _migration_lock_path(self) -> Path | None:
        return self._path.with_name(self._path.name + ".migrate.lock") if self._path else None

    def _converting(self, body: Callable[[Any], None]) -> None:
        """`_transaction(body)`, kept trying while other connections' writes
        conflict with it (they are the store's users finishing their work),
        up to MIGRATION_WAIT_S; then a clear refusal."""
        import duckdb  # lazy

        deadline = time.monotonic() + MIGRATION_WAIT_S
        said = False
        while True:
            try:
                self._transaction(body)
                return
            except duckdb.TransactionException as e:
                if time.monotonic() >= deadline:
                    raise CribUserError(
                        "crib could not convert its DuckDB store to this version: "
                        f"another connection kept writing for {MIGRATION_WAIT_S:.0f}s "
                        f"({e}). Stop every crib process (and the crib DuckDB server), "
                        "then start one; the store is a cache, so moving its file "
                        "aside is also safe (it is rebuilt from your notes)"
                    ) from e
                if not said:
                    print("[crib] store conversion is waiting for another connection's "
                          "writes to finish", file=sys.stderr)
                    said = True
                time.sleep(1.0)

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
            for table in (TABLE, EMBED_CACHE, LEDGER, ITEMS, FILES):
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
            # item vectors came from the same embedder: empty them too, and the
            # file stamps, or every facet would think its files already mapped
            for table in (ITEMS, EDGES, POSTINGS, FILES,
                          *(t for t, _ in _FACET_TABLES.values())):
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
        steps = self._apply_steps(records, deletes, meta, items, item_deletes)
        if not steps:
            return

        def write(ex: Any) -> None:
            for step in steps:
                step(ex)

        self._transaction(write)

    def _apply_steps(
        self,
        records: list[Record],
        deletes: list[str],
        meta: dict[str, dict[str, Any]],
        items: list[Item] | None,
        item_deletes: list[str] | None,
    ) -> list[Callable[[Any], None]]:
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
        holder = list(items or ())
        if holder:
            steps.append(_carry_vectors_step(holder))
        drop = list(item_deletes or ()) + [it.id for it in holder]
        if drop:
            steps.append(_delete_items_step(drop))
        if holder:
            steps.append(_insert_items_step(holder))
        return steps

    # ── per-file change detection (crib.filesync) ────────────────────────────

    def file_stamps(self, facet: str, project: str) -> dict[str, tuple[int, int, list[str]]]:
        with self._reading() as ex:
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                f"SELECT file, mtime_ns, size, item_ids FROM {FILES} "
                "WHERE facet = ? AND project = ?",
                [facet, project],
            )
        return {f: (int(m), int(z), list(ids or [])) for f, m, z, ids in rows}

    def replace_files(
        self,
        facet: str,
        project: str,
        files: list[tuple[str, int, int, list[Item]]],
        removed: list[str],
    ) -> None:
        if not files and not removed:
            return
        names = [f for f, *_ in files] + list(removed)
        new_items = [it for *_, its in files for it in its]

        def write(ex: Any) -> None:
            # pi-lens-ignore: python-sql-injection
            old = ex.run(
                f"SELECT item_ids FROM {FILES} WHERE facet = ? AND project = ? "
                "AND file = ANY(?)",
                [facet, project, names],
            )
            stale = sorted({i for (ids,) in old for i in (ids or [])})
            for step in self._apply_steps([], [], {}, new_items, stale):
                step(ex)
            # pi-lens-ignore: python-sql-injection
            ex.run(
                f"DELETE FROM {FILES} WHERE facet = ? AND project = ? AND file = ANY(?)",
                [facet, project, names],
            )
            rows = [[facet, project, f, int(m), int(z), [it.id for it in its]]
                    for f, m, z, its in files]
            if rows:
                # pi-lens-ignore: python-sql-injection
                ex.run_many(f"INSERT INTO {FILES} VALUES (?, ?, ?, ?, ?, ?)", rows)

        self._transaction(write)

    def missing_body_vectors(self, where: dict[str, Any]) -> dict[str, str]:
        clause, params = _item_clause(where)
        clause += ((" AND " if clause else " WHERE ")
                   + "i.embed_text IS NOT NULL AND i.body_vec IS NULL")
        with self._reading() as ex:
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(f"SELECT i.id, i.embed_text FROM {ITEMS} i{clause}", params)
        return dict(rows)

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

    def set_body_vectors(self, vectors: dict[str, list[float]]) -> None:
        rows = [[i, [float(x) for x in v]] for i, v in vectors.items()]
        if not rows:
            return

        def write(ex: Any) -> None:
            for start in range(0, len(rows), _META_ROWS_PER_STATEMENT):
                batch = rows[start : start + _META_ROWS_PER_STATEMENT]
                groups = ", ".join("(?, ?)" for _ in batch)
                # pi-lens-ignore: python-sql-injection
                ex.run(
                    f"UPDATE {ITEMS} SET body_vec = v.vec::FLOAT[] "
                    f"FROM (VALUES {groups}) AS v(id, vec) WHERE {ITEMS}.id = v.id",
                    [x for row in batch for x in row],
                )

        self._transaction(write)

    def lacking_body_vectors(
        self, where: dict[str, Any], candidates_only: bool = False
    ) -> list[str]:
        clause, params = _item_clause(where)
        clause += (" AND " if clause else " WHERE ") + "i.body_vec IS NULL"
        if candidates_only:
            # pi-lens-ignore: python-sql-injection
            clause += f" AND EXISTS (SELECT 1 FROM {POSTINGS} p WHERE p.item_id = i.id)"
        with self._reading() as ex:
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(f"SELECT i.id FROM {ITEMS} i{clause} ORDER BY i.id", params)
        return [r[0] for r in rows]

    def body_vector_top(
        self, where: dict[str, Any], vec: list[float], n: int
    ) -> list[tuple[str, float]]:
        if n <= 0:
            return []
        clause, params = _item_clause(where)
        clause += (" AND " if clause else " WHERE ") + "i.body_vec IS NOT NULL"
        with self._reading() as ex:
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                f"SELECT i.id, list_inner_product(i.body_vec, ?::FLOAT[]) AS s "
                f"FROM {ITEMS} i{clause} ORDER BY s DESC, i.id LIMIT ?",
                [[float(x) for x in vec], *params, int(n)],
            )
        return [(i, float(s)) for i, s in rows]

    def body_vectors(self, ids: list[str]) -> dict[str, list[float]]:
        if not ids:
            return {}
        with self._reading() as ex:
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                f"SELECT id, body_vec FROM {ITEMS} WHERE id = ANY(?) "
                "AND body_vec IS NOT NULL",
                [list(ids)],
            )
        return {i: [float(x) for x in v] for i, v in rows}

    def postings(self, where: dict[str, Any], terms: list[str]) -> PostingStats:
        clause, params = _item_clause(where)
        scope = (
            f"SELECT p.item_id, p.term, p.tf FROM {POSTINGS} p "
            f"JOIN {ITEMS} i ON i.id = p.item_id{clause}"
        )
        with self._reading() as ex:
            # pi-lens-ignore: python-sql-injection
            ((n, total),) = ex.run(
                f"SELECT count(DISTINCT item_id), coalesce(sum(tf), 0) FROM ({scope})",
                params,
            )
            hits: list[tuple] = []
            lengths: dict[str, float] = {}
            if terms:
                # pi-lens-ignore: python-sql-injection
                hits = ex.run(
                    f"SELECT item_id, term, tf FROM ({scope}) WHERE term = ANY(?)",
                    [*params, list(dict.fromkeys(terms))],
                )
            ids = sorted({r[0] for r in hits})
            if ids:
                # pi-lens-ignore: python-sql-injection
                lengths = dict(ex.run(
                    f"SELECT item_id, sum(tf) FROM {POSTINGS} WHERE item_id = ANY(?) "
                    "GROUP BY item_id",
                    [ids],
                ))
        df: dict[str, int] = {}
        docs: dict[str, tuple[float, dict[str, float]]] = {}
        for item_id, term, tf in hits:
            df[term] = df.get(term, 0) + 1
            docs.setdefault(item_id, (float(lengths[item_id]), {}))[1][term] = float(tf)
        return PostingStats(int(n), float(total), df, docs)

    def facet_values(self, where: dict[str, Any], fields: list[str]) -> list[dict[str, Any]]:
        kind = where.get("kind")
        if not isinstance(kind, str) or kind not in _FACET_TABLES:
            raise CribUserError("facet_values needs one `kind` that has a facet")
        table, known = _FACET_TABLES[kind]
        if bad := [f for f in fields if f not in known]:
            raise CribUserError(f"{kind} items have no facet field {bad}")
        clause, params = _item_clause(where)
        with self._reading() as ex:
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                f"SELECT {', '.join('f.' + f for f in fields)} FROM {ITEMS} i "
                f"JOIN {table} f ON f.id = i.id{clause}",
                params,
            )
        return [dict(zip(fields, r)) for r in rows]

    def item_hashes(self, where: dict[str, Any]) -> dict[str, str]:
        clause, params = _item_clause(where)
        with self._reading() as ex:
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(f"SELECT i.id, i.content_hash FROM {ITEMS} i{clause}", params)
        return {i: h for i, h in rows}

    def items(
        self,
        where: dict[str, Any],
        facet: dict[str, Any] | None = None,
        vectors: bool = True,
    ) -> dict[str, Item]:
        clause, params = _item_clause(where)
        join = ""
        if facet:
            kind = where.get("kind")
            if not isinstance(kind, str) or kind not in _FACET_TABLES:
                raise CribUserError("a facet filter needs one `kind` that has a facet")
            table, fields = _FACET_TABLES[kind]
            conds = []
            for name, value in facet.items():
                if name not in fields:
                    raise CribUserError(f"{kind} items have no facet field {name!r}")
                (op, wanted), = value.items() if isinstance(value, dict) else (("=", value),)
                if op == "$in":
                    conds.append(f"list_contains(?::VARCHAR[], f.{name})")
                    params.append(list(wanted))
                elif op == "=":
                    conds.append(f"f.{name} = ?")
                    params.append(wanted)
                else:
                    raise CribUserError(f"unsupported facet filter {value!r}")
            join = f" JOIN {table} f ON f.id = i.id"
            clause += (" AND " if clause else " WHERE ") + " AND ".join(conds)
        cols = [("i." + c) if vectors or c not in ("title_vec", "body_vec") else "NULL"
                for c in _ITEM_COLUMNS]
        with self._reading() as ex:
            # pi-lens-ignore: python-sql-injection
            rows = ex.run(
                f"SELECT {', '.join(cols)} FROM {ITEMS} i{join}{clause}",
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
                        # a list column reads back as stored: NULL (absent) is
                        # None, an empty list stays []
                        f: (list(v) if types[f] == "VARCHAR[]" and v is not None else v)
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
                embed_text=r[8],
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
