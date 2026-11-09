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
and column names are fixed constants; JSON paths are bound too.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .errors import CribUserError
from .store import Hit, Record

TABLE = "crib_chunks"
PROMOTED = ("project", "store", "relpath")  # filtered constantly: real columns
# The only column lists a read may select — never caller text.
_SELECT_COLUMNS = MappingProxyType({"meta": "id, meta", "docs": "id, document, meta"})


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


class DuckDBStore:
    """Embedded DuckDB: the daemon is the only process with the file open.

    Thread-safe: the daemon calls the store from worker threads. Each operation
    runs on its own cursor (DuckDB cursors are separate connections into one
    database), and writes are serialized so two writers never race a
    first-upsert CREATE TABLE or conflict mid-transaction."""

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
            conn = duckdb.connect(str(path))
        except duckdb.IOException as exc:
            if "lock" in str(exc).lower():
                raise _lock_error(path, exc) from exc
            raise
        return cls(conn, path)

    def close(self) -> None:
        self._conn.close()

    # ── schema ───────────────────────────────────────────────────────────────

    def current_dim(self) -> int | None:
        """The stored vectors' dimension, or None when the store is EMPTY (the
        contract) — an empty table still has a typed column, so check for rows."""
        cur = self._conn.cursor()
        dim = self._table_dim(cur)
        if dim is None:
            return None
        # pi-lens-ignore: python-sql-injection
        return dim if cur.execute(f"SELECT 1 FROM {TABLE} LIMIT 1").fetchall() else None

    def _table_dim(self, cur: Any) -> int | None:
        """The vector column's dimension, rows or not (None: no table yet)."""
        rows = cur.execute(
            "SELECT data_type FROM duckdb_columns() "
            "WHERE table_name = ? AND column_name = 'vec'",
            [TABLE],
        ).fetchall()
        return int(rows[0][0].rsplit("[", 1)[1].rstrip("]")) if rows else None

    def _ensure_table(self, cur: Any, dim: int) -> None:
        have = self._table_dim(cur)
        if have is None:
            cur.execute(
                f"CREATE TABLE {TABLE} ("
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

    def recreate(self) -> None:
        with self._write:
            self._conn.cursor().execute(f"DROP TABLE IF EXISTS {TABLE}")

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
                r.embedding,
            ]
            for r in records
        ]
        with self._write:
            cur = self._conn.cursor()
            self._ensure_table(cur, dim)
            cur.execute("BEGIN")
            try:
                cur.execute(
                    f"DELETE FROM {TABLE} WHERE id = ANY(?)", [[r.id for r in records]]
                )
                # TABLE is a module constant; every value is a bound parameter.
                # pi-lens-ignore: python-sql-injection
                cur.executemany(
                    f"INSERT INTO {TABLE} VALUES (?, ?, ?, ?, ?, ?, ?)", rows
                )
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._write:
            cur = self._conn.cursor()
            if self._table_dim(cur) is not None:
                cur.execute(f"DELETE FROM {TABLE} WHERE id = ANY(?)", [list(ids)])

    def set_meta(self, updates: dict[str, dict[str, Any]]) -> None:
        """REPLACE each record's metadata (the contract): keys absent from the new
        dict are gone. Vectors and documents are untouched."""
        if not updates:
            return
        rows = [[json.dumps(m), *_promoted(m), i] for i, m in updates.items()]
        with self._write:
            cur = self._conn.cursor()
            if self._table_dim(cur) is None:
                return
            # TABLE is a module constant; every value is a bound parameter.
            # pi-lens-ignore: python-sql-injection
            cur.executemany(
                f"UPDATE {TABLE} SET meta = ?, project = ?, store = ?, relpath = ? "
                "WHERE id = ?",
                rows,
            )

    # ── reads ────────────────────────────────────────────────────────────────

    def _select(self, which: str, where: dict[str, Any] | None) -> list[tuple]:
        columns = _SELECT_COLUMNS[which]  # allowlisted: never caller text
        cur = self._conn.cursor()
        if self._table_dim(cur) is None:
            return []
        clause, params = _where(where)
        # columns/TABLE are constants and `clause` is built from constant column
        # names and `?` placeholders only; every value is in `params`.
        # pi-lens-ignore: python-sql-injection
        return cur.execute(f"SELECT {columns} FROM {TABLE}{clause}", params).fetchall()

    def get_meta(self, where: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {i: json.loads(m) for i, m in self._select("meta", where)}

    def get_docs(self, where: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
        return {i: (d, json.loads(m)) for i, d, m in self._select("docs", where)}

    def query(
        self, embedding: list[float], k: int, where: dict[str, Any] | None = None
    ) -> list[Hit]:
        cur = self._conn.cursor()
        dim = self._table_dim(cur)
        if dim is None or k <= 0:
            return []
        if len(embedding) != dim:
            raise CribUserError(
                f"embedding dimension mismatch: query has {len(embedding)}, stored "
                f"vectors have {dim} — the store holds vectors from a different "
                "embedder; run `crib project reconcile` to re-embed at the current "
                "dimension"
            )
        clause, params = _where(where)
        # Ties break on id so the order is total: retrieval stays deterministic.
        rows = cur.execute(
            f"SELECT id, document, meta, "
            f"array_cosine_similarity(vec, ?::FLOAT[{int(dim)}]) AS score "
            f"FROM {TABLE}{clause} ORDER BY score DESC, id LIMIT ?",
            [embedding, *params, int(k)],
        ).fetchall()
        return [Hit(i, d, json.loads(m), float(s)) for i, d, m, s in rows]
