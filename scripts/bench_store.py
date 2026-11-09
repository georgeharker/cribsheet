#!/usr/bin/env python3
"""Vector-store benchmark: every crib backend, embedded vs server, exact vs HNSW.

Measures crib's OWN Store classes (so the numbers are what crib sees), plus the
HNSW option as a harness-only variant (index built IN MEMORY at open — never the
experimental persistence flag; design: "Vector store moves to DuckDB on two
paths").

Data: a read-only export of a real crib store (`--data`, made by exporting the live
collection) and real query texts embedded with the configured model. Larger
stores are synthesised from the real vectors (tiled + small noise, renormalised)
so the distribution stays realistic, with a 50-chunk "tiny" project to test a
very selective filter.

    python scripts/bench_store.py --data /tmp/vsbench/real.npz --sizes real,20000,100000

Output: a JSON dump (every number) and a markdown table, under --out.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import resource
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crib import proc  # noqa: E402
from crib.store import ChromaStore, InMemoryStore, Record  # noqa: E402
from crib.store_duckdb import DuckDBStore  # noqa: E402
from crib.store_quack import QuackStore, spawn_server, to_literal  # noqa: E402

DIM = 384
TINY = "tiny-project"  # 50 chunks: the selective-filter case


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ms(seconds: float) -> float:
    return round(seconds * 1000, 3)


def pct(xs: list[float], p: float) -> float:
    return float(np.percentile(xs, p)) if xs else float("nan")


# ── data ─────────────────────────────────────────────────────────────────────


@dataclass
class Dataset:
    name: str
    vecs: np.ndarray  # (n, d) float32, unit rows
    ids: list[str]
    docs: list[str]
    metas: list[dict[str, Any]]
    queries: np.ndarray  # (q, d)

    def records(self, lo: int = 0, hi: int | None = None) -> list[Record]:
        hi = len(self.ids) if hi is None else hi
        return [
            Record(self.ids[i], self.vecs[i].tolist(), self.docs[i], self.metas[i])
            for i in range(lo, hi)
        ]


def load_real(npz: Path) -> Dataset:
    d = np.load(npz)
    meta = json.loads(npz.with_name(npz.stem + "_meta.json").read_text())
    vecs = d["vecs"].astype(np.float32)
    metas = [dict(m) for m in meta["metadatas"]]
    # the tiny project: relabel 50 chunks so a selective filter exists at every size
    for m in metas[:50]:
        m["project"] = TINY
    return Dataset(
        "real",
        vecs,
        list(meta["ids"]),
        list(meta["documents"]),
        metas,
        d["queries"].astype(np.float32),
    )


def synthesise(real: Dataset, n: int, seed: int = 11) -> Dataset:
    """n chunks from the real ones: tiled, with noise so copies aren't duplicates,
    renormalised. Projects are suffixed per tile so filters stay selective."""
    rng = np.random.default_rng(seed)
    reps = -(-n // len(real.ids))
    idx = np.tile(np.arange(len(real.ids)), reps)[:n]
    vecs = real.vecs[idx] + rng.normal(0, 0.02, (n, real.vecs.shape[1])).astype(
        np.float32
    )
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    ids, docs, metas = [], [], []
    for j, i in enumerate(idx):
        tile = j // len(real.ids)
        m = dict(real.metas[i])
        if m["project"] != TINY:
            m["project"] = m["project"] if tile == 0 else f"{m['project']}-{tile}"
        elif tile:
            m["project"] = f"filler-{tile}"  # only ONE tiny project at any size
        ids.append(f"{real.ids[i]}-{tile}")
        docs.append(real.docs[i])
        metas.append(m)
    return Dataset(str(n), vecs.astype(np.float32), ids, docs, metas, real.queries)


def exact_topk(
    ds: Dataset, q: np.ndarray, k: int, where: dict[str, Any] | None
) -> list[str]:
    if where:
        mask = np.fromiter(
            (all(m.get(a) == b for a, b in where.items()) for m in ds.metas),
            bool,
            len(ds.metas),
        )
        idx = np.flatnonzero(mask)
    else:
        idx = np.arange(len(ds.ids))
    sims = ds.vecs[idx] @ q
    order = idx[np.lexsort((np.array([ds.ids[i] for i in idx]), -sims))][:k]
    return [ds.ids[i] for i in order]


# ── backends ─────────────────────────────────────────────────────────────────


@dataclass
class Backend:
    """One store under test. `open()` returns a Store-like object; `cleanup()`
    tears everything down. `hnsw` variants answer `query` approximately."""

    name: str
    open: Callable[[], Any]
    cleanup: Callable[[], None] = lambda: None
    server: bool = False
    notes: list[str] = field(default_factory=list)


def chroma_embedded(root: Path) -> Backend:
    path = str(root / "chroma")
    return Backend("chroma embedded (HNSW)", lambda: ChromaStore.embedded(path))


def chroma_shared(root: Path) -> Backend:
    import chromadb

    port = free_port()
    exe = shutil.which("chroma", path=str(Path(sys.executable).parent)) or "chroma"
    p = proc.popen(
        [
            exe,
            "run",
            "--path",
            str(root / "chroma-srv"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 60
    while True:
        try:
            chromadb.HttpClient(host="127.0.0.1", port=port).heartbeat()
            break
        except Exception:  # noqa: BLE001 — not up yet
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)

    def stop() -> None:
        p.terminate()
        p.wait(30)

    return Backend(
        "chroma shared (HNSW, HTTP)",
        lambda: ChromaStore.shared("127.0.0.1", port),
        stop,
        server=True,
    )


def duckdb_embedded(root: Path) -> Backend:
    path = root / "crib.duckdb"
    return Backend("duckdb embedded (exact)", lambda: DuckDBStore.embedded(path))


def duckdb_quack(root: Path) -> Backend:
    srv = spawn_server(root / "quack.duckdb", free_port(), root / "token", timeout=180)
    return Backend(
        "duckdb quack (exact)",
        lambda: QuackStore.connect(srv.uri, srv.token),
        srv.stop,
        server=True,
    )


class HnswStore:
    """DuckDB + the vss HNSW index, built IN MEMORY when opened (the safe way to
    offer HNSW). Wraps a persistent DuckDBStore for writes; reads go through an
    in-memory copy that carries the index. Harness-only until the option exists."""

    def __init__(
        self, base: Any, conn_factory: Callable[[], Any], remote: bool
    ) -> None:
        self.base = base
        self._factory = conn_factory
        self._remote = remote
        self.build_s = 0.0
        self._cur: Any = None

    def build(self) -> None:
        t0 = time.perf_counter()
        self._cur = self._factory()
        self.build_s = time.perf_counter() - t0

    def query(self, embedding, k, where=None):
        clause, params = "", []
        if where:
            conds = []
            for key, value in where.items():
                conds.append(f"{key} = ?")
                params.append(value)
            clause = " WHERE " + " AND ".join(conds)
        sql = (
            f"SELECT id FROM mem.chunks{clause} "
            f"ORDER BY array_cosine_distance(vec, ?::FLOAT[{DIM}]) LIMIT ?"
        )
        args = [*params, [float(x) for x in embedding], int(k)]
        if self._remote:  # Quack: no bound parameters
            from crib.store_quack import render

            # pi-lens-ignore: python-sql-injection
            return [r[0] for r in self._cur.execute(render(sql, args)).fetchall()]
        # pi-lens-ignore: python-sql-injection
        return [r[0] for r in self._cur.execute(sql, args).fetchall()]

    def __getattr__(self, name):  # writes and other reads: the persistent store
        return getattr(self.base, name)


_HNSW_SETUP = (
    "LOAD vss",
    "ATTACH ':memory:' AS mem",
    "CREATE TABLE mem.chunks AS SELECT id, project, store, vec FROM main.crib_chunks",
    "CREATE INDEX chunks_hnsw ON mem.chunks USING HNSW (vec) WITH (metric = 'cosine')",
)


def duckdb_embedded_hnsw(root: Path, ext: Path) -> Backend:
    import duckdb

    path = root / "crib-hnsw.duckdb"

    def open_() -> Any:
        base = DuckDBStore.embedded(path)

        def factory():
            cur = base._conn.cursor()
            cur.execute(f"SET extension_directory = {to_literal(str(ext))}")
            for s in _HNSW_SETUP:
                # _HNSW_SETUP holds constant statements, no data
                # pi-lens-ignore: python-sql-injection
                cur.execute(s)
            return cur

        return HnswStore(base, factory, remote=False)

    duckdb.connect(":memory:", config={"extension_directory": str(ext)}).execute(
        "INSTALL vss"
    )
    return Backend("duckdb embedded (HNSW in memory)", open_)


def duckdb_quack_hnsw(root: Path, ext: Path) -> Backend:
    """The server holds the in-memory HNSW copy; clients query it over Quack."""
    srv = spawn_server(
        root / "quack-hnsw.duckdb",
        free_port(),
        root / "token-h",
        extension_directory=ext,
        timeout=180,
    )

    def open_() -> Any:
        base = QuackStore.connect(srv.uri, srv.token, ext)

        def factory():
            # the index must live in the SERVER: build it through the session
            cur = base._remote
            for s in _HNSW_SETUP:
                with contextlib.suppress(
                    Exception
                ):  # vss may already be loaded/attached
                    # _HNSW_SETUP holds constant statements, no data
                    # pi-lens-ignore: python-sql-injection
                    cur.execute(s)
            return cur

        return HnswStore(base, factory, remote=True)

    return Backend(
        "duckdb quack (HNSW in server memory)",
        open_,
        srv.stop,
        server=True,
        notes=[
            "needs vss loaded in the server BEFORE lockdown (INSTALL is refused after)"
        ],
    )


def memory_store(root: Path) -> Backend:
    return Backend("in-memory python (exact, floor)", InMemoryStore)


# ── measurements ─────────────────────────────────────────────────────────────


def timed(fn: Callable[[], Any], reps: int) -> list[float]:
    out = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t0)
    return out


def nearest(store: Any, embedding: list[float], k: int, where: dict[str, Any] | None) -> Any:
    """The store's nearest-neighbour query. `where` is a metadata filter dict,
    not SQL; each backend binds or escapes its values itself."""
    # pi-lens-ignore: python-sql-injection
    return store.query(embedding, k, where)


def ids_of(hits: Any) -> list[str]:
    return [h if isinstance(h, str) else h.id for h in hits]


def disk_size(root: Path) -> int:
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


def bench_backend(b: Backend, ds: Dataset, root: Path, nq: int) -> dict[str, Any]:
    res: dict[str, Any] = {
        "backend": b.name,
        "dataset": ds.name,
        "n": len(ds.ids),
        "server": b.server,
        "notes": b.notes,
    }
    store = b.open()
    target = store.base if isinstance(store, HnswStore) else store

    # bulk load, in crib-sized batches
    t0 = time.perf_counter()
    for lo in range(0, len(ds.ids), 500):
        target.upsert(ds.records(lo, min(lo + 500, len(ds.ids))))
    res["bulk_load_s"] = round(time.perf_counter() - t0, 2)

    if isinstance(store, HnswStore):
        store.build()
        res["hnsw_build_ms"] = ms(store.build_s)

    queries = ds.queries[:nq]
    filters = {
        "crib filter (project+store)": {"project": "cribsheet", "store": "notes"},
        "selective (50-chunk project)": {"project": TINY},
        "unfiltered": None,
    }
    for fname, where in filters.items():
        for k in (10, 30):
            lat, recall, short = [], [], 0
            for q in queries:
                t0 = time.perf_counter()
                got = ids_of(nearest(store, q.tolist(), k, where))
                lat.append(time.perf_counter() - t0)
                want = exact_topk(ds, q, k, where)
                if want:
                    recall.append(len(set(got) & set(want)) / len(want))
                short += len(got) < len(want)
            res[f"{fname} k={k}"] = {
                "p50_ms": ms(pct(lat, 50)),
                "p95_ms": ms(pct(lat, 95)),
                "p99_ms": ms(pct(lat, 99)),
                "recall": round(statistics.mean(recall), 4) if recall else None,
                "short_results": short,
            }

    # per-note write costs (a note is ~5 chunks): upsert, set_meta, delete
    note = ds.records(0, 5)
    res["note_upsert_ms_p50"] = ms(
        statistics.median(timed(lambda: target.upsert(note), 30))
    )
    upd = {r.id: {**r.metadata, "title": "renamed"} for r in note}
    res["note_set_meta_ms_p50"] = ms(
        statistics.median(timed(lambda: target.set_meta(upd), 30))
    )
    ids = [r.id for r in note]

    def delete_and_restore():
        target.delete(ids)
        target.upsert(note)

    res["note_delete+reupsert_ms_p50"] = ms(
        statistics.median(timed(delete_and_restore, 20))
    )
    res["get_docs_project_ms"] = ms(
        statistics.median(timed(lambda: target.get_docs({"project": "cribsheet"}), 5))
    )

    # determinism: same query, same ordered ids, after a reopen (HNSW: a rebuild)
    probe = queries[0].tolist()
    first = ids_of(nearest(store, probe, 30, {"project": "cribsheet", "store": "notes"}))
    with contextlib.suppress(Exception):
        target.close()
    t0 = time.perf_counter()
    store = b.open()
    if isinstance(store, HnswStore):
        store.build()
    again = ids_of(nearest(store, probe, 30, {"project": "cribsheet", "store": "notes"}))
    res["cold_open_first_query_ms"] = ms(time.perf_counter() - t0)
    res["identical_after_reopen"] = first == again
    with contextlib.suppress(Exception):
        (store.base if isinstance(store, HnswStore) else store).close()
    res["disk_mb"] = round(disk_size(root) / 1e6, 1)
    return res


# ── report ───────────────────────────────────────────────────────────────────


def table(rows: list[dict[str, Any]]) -> str:
    cols = [
        ("backend", lambda r: r["backend"]),
        ("n", lambda r: f"{r['n']:,}"),
        (
            "crib filter k=30 p50/p95 ms",
            lambda r: "{p50_ms:.2f} / {p95_ms:.2f}".format(
                **r["crib filter (project+store) k=30"]
            ),
        ),
        ("recall", lambda r: f"{r['crib filter (project+store) k=30']['recall']:.3f}"),
        (
            "selective k=30 p50 ms",
            lambda r: f"{r['selective (50-chunk project) k=30']['p50_ms']:.2f}",
        ),
        (
            "sel. recall",
            lambda r: f"{r['selective (50-chunk project) k=30']['recall']:.3f}",
        ),
        ("unfiltered k=10 p50 ms", lambda r: f"{r['unfiltered k=10']['p50_ms']:.2f}"),
        ("unf. recall", lambda r: f"{r['unfiltered k=10']['recall']:.3f}"),
        ("note upsert ms", lambda r: f"{r['note_upsert_ms_p50']:.2f}"),
        ("set_meta ms", lambda r: f"{r['note_set_meta_ms_p50']:.2f}"),
        ("get_docs ms", lambda r: f"{r['get_docs_project_ms']:.1f}"),
        ("bulk load s", lambda r: f"{r['bulk_load_s']:.1f}"),
        ("cold open ms", lambda r: f"{r['cold_open_first_query_ms']:.0f}"),
        (
            "HNSW build ms",
            lambda r: f"{r['hnsw_build_ms']:.0f}" if "hnsw_build_ms" in r else "—",
        ),
        ("same after reopen", lambda r: "yes" if r["identical_after_reopen"] else "NO"),
        ("disk MB", lambda r: f"{r['disk_mb']:.0f}"),
    ]
    out = ["| " + " | ".join(c for c, _ in cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        if "error" in r:
            out.append(
                f"| {r['backend']} | {r.get('n', '?')} | ERROR: {r['error'][:80]} |"
            )
            continue
        out.append("| " + " | ".join(f(r) for _, f in cols) + " |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--sizes", default="real,20000,100000")
    ap.add_argument("--backends", default="all")
    ap.add_argument("--queries", type=int, default=200)
    ap.add_argument("--out", type=Path, default=Path("/tmp/vsbench/results"))
    ap.add_argument("--extension-dir", type=Path, default=Path("/tmp/vsbench/ext"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    real = load_real(args.data)
    makers = {
        "chroma-embedded": lambda r: chroma_embedded(r),
        "chroma-shared": lambda r: chroma_shared(r),
        "duckdb-embedded": lambda r: duckdb_embedded(r),
        "duckdb-quack": lambda r: duckdb_quack(r),
        "duckdb-embedded-hnsw": lambda r: duckdb_embedded_hnsw(r, args.extension_dir),
        "duckdb-quack-hnsw": lambda r: duckdb_quack_hnsw(r, args.extension_dir),
        "memory": lambda r: memory_store(r),
    }
    chosen = list(makers) if args.backends == "all" else args.backends.split(",")
    rows: list[dict[str, Any]] = []
    for size in args.sizes.split(","):
        ds = real if size == "real" else synthesise(real, int(size))
        for name in chosen:
            if name == "memory" and len(ds.ids) > 20000:
                continue  # pure-python floor: pointless at scale
            root = Path(tempfile.mkdtemp(prefix=f"bench-{name}-", dir="/tmp/vsbench"))
            b = None
            try:
                b = makers[name](root)
                r = bench_backend(b, ds, root, args.queries)
            except Exception as e:  # noqa: BLE001 — recorded, the run continues
                r = {
                    "backend": name,
                    "dataset": ds.name,
                    "n": len(ds.ids),
                    "error": f"{type(e).__name__}: {e}",
                }
            finally:
                if b is not None:
                    with contextlib.suppress(Exception):
                        b.cleanup()
                shutil.rmtree(root, ignore_errors=True)
            rows.append(r)
            short = r.get("crib filter (project+store) k=30", {})
            print(
                f"[{ds.name:>6}] {r['backend']:<40} "
                + (
                    f"p50 {short.get('p50_ms')} ms recall {short.get('recall')}"
                    if short
                    else r.get("error", "")[:100]
                ),
                flush=True,
            )
    meta = {
        "host": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "duckdb": __import__("duckdb").__version__,
        "chromadb": __import__("chromadb").__version__,
        "git_sha": proc.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "queries": args.queries,
        "max_rss_mb": round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6, 1
        ),
    }
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (args.out / f"bench-{stamp}.json").write_text(
        json.dumps({"meta": meta, "rows": rows}, indent=2)
    )
    md = f"# store bench {stamp}\n\n`{json.dumps(meta)}`\n\n" + table(rows) + "\n"
    (args.out / f"bench-{stamp}.md").write_text(md)
    print("\n" + md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
