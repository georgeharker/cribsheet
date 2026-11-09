"""Move crib's vector index from one store to another WITHOUT re-embedding.

The store is a rebuildable cache (design: "Disk is truth, Chroma is a rebuildable
cache"), so a migration is only ever a fast path: re-embedding from disk is always
valid, and this module refuses cases that need it (an embedding-dimension
mismatch) rather than guessing.

    plan = plan_migration(source, target)   # read-only: what would move
    report = migrate(source, target)        # copy + verify (idempotent)

- **Idempotent and resumable.** A record is copied when the target lacks its id
  or holds it with a different content_hash. A rerun copies nothing new, and an
  interrupted run picks up where it stopped. Records only the target holds are
  removed, so the target ends as an exact mirror of the source.
- **The source is never written.**
- **Verification is part of the run, not optional:** per-(project, store)
  counts, every id's content_hash, an exact vector comparison on a sample, and
  top-k parity on probe queries. A failed check makes `ok` false and says why.
"""

from __future__ import annotations

import math
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .errors import CribUserError
from .store import Record, Store

BATCH = 500
_SAMPLE = 200  # records whose vectors are compared exactly
_PROBES = 40  # probe queries for top-k parity
_K = 10


def _key(meta: dict[str, Any]) -> tuple[str, str]:
    return (str(meta.get("project", "")), str(meta.get("store") or "notes"))


def _hash(meta: dict[str, Any]) -> Any:
    return meta.get("content_hash")


@dataclass
class Plan:
    source_total: int
    target_total: int
    to_copy: int  # missing from the target, or present with a different hash
    to_remove: int  # only in the target
    unchanged: int
    dim: int | None
    by_partition: dict[str, int] = field(default_factory=dict)  # "project/store": n

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_total": self.source_total,
            "target_total": self.target_total,
            "to_copy": self.to_copy,
            "to_remove": self.to_remove,
            "unchanged": self.unchanged,
            "dim": self.dim,
            "by_partition": self.by_partition,
        }


def _check_dims(source: Store, target: Store) -> int | None:
    sd, td = source.current_dim(), target.current_dim()
    if sd is not None and td is not None and sd != td:
        raise CribUserError(
            f"the target store holds {td}-d vectors but the source has {sd}-d: the "
            "embedder differs, so vectors can't be copied. Rebuild the target from "
            "disk instead (switch the backend, then `crib project reconcile`)"
        )
    return sd


def plan_migration(source: Store, target: Store) -> Plan:
    """What `migrate` would do. Reads both stores, writes neither."""
    dim = _check_dims(source, target)
    src = source.get_meta({})
    dst = target.get_meta({})
    to_copy = sum(
        1 for i, m in src.items() if i not in dst or _hash(dst[i]) != _hash(m)
    )
    parts = Counter("/".join(_key(m)) for m in src.values())
    return Plan(
        source_total=len(src),
        target_total=len(dst),
        to_copy=to_copy,
        to_remove=sum(1 for i in dst if i not in src),
        unchanged=len(src) - to_copy,
        dim=dim,
        by_partition=dict(sorted(parts.items())),
    )


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def verify(source: Store, target: Store, seed: int = 0) -> dict[str, Any]:
    """Compare the two stores. Returns {ok, checks: {...}, problems: [...]}."""
    problems: list[str] = []
    src = source.get_meta({})
    dst = target.get_meta({})

    src_parts = Counter(_key(m) for m in src.values())
    dst_parts = Counter(_key(m) for m in dst.values())
    if src_parts != dst_parts:
        diff = {
            "/".join(k): (src_parts.get(k, 0), dst_parts.get(k, 0))
            for k in set(src_parts) | set(dst_parts)
            if src_parts.get(k, 0) != dst_parts.get(k, 0)
        }
        problems.append(f"per-partition counts differ (source, target): {diff}")

    missing = [i for i in src if i not in dst]
    extra = [i for i in dst if i not in src]
    changed = [i for i in src if i in dst and _hash(dst[i]) != _hash(src[i])]
    meta_diff = [i for i in src if i in dst and dst[i] != src[i]]
    for label, ids in (
        ("missing from target", missing),
        ("only in target", extra),
        ("content_hash differs", changed),
        ("metadata differs", meta_diff),
    ):
        if ids:
            problems.append(f"{len(ids)} {label} (e.g. {ids[:3]})")

    # exact vector + document comparison on a deterministic sample
    rng = random.Random(seed)
    sample = set(rng.sample(sorted(src), min(_SAMPLE, len(src))))
    src_recs: dict[str, Record] = {}
    dst_recs: dict[str, Record] = {}
    for store, out in ((source, src_recs), (target, dst_recs)):
        for batch in store.iter_records(BATCH):
            out.update((r.id, r) for r in batch if r.id in sample)
    worst = 1.0
    for i in sample:
        a, b = src_recs.get(i), dst_recs.get(i)
        if a is None or b is None:
            continue  # already reported as missing
        if a.document != b.document:
            problems.append(f"document differs for {i}")
        worst = min(worst, _cos(a.embedding, b.embedding))
    if worst < 1 - 1e-6:
        problems.append(f"a sampled vector changed (worst cosine {worst:.8f})")

    # Search checks, with sampled vectors as queries within their own partition.
    # The source may be APPROXIMATE (chroma's HNSW) and the target exact, so the
    # two may return different ids among near-ties: id overlap is reported, not
    # judged. What must hold whatever the source's approximation:
    # - self-retrieval: a record's own vector finds that record at the top
    #   (score ~1; a tie with an identical vector counts);
    # - score dominance: the target's k-th best score is no worse than the
    #   source's (exact search can't lose to approximate search).
    probes = sorted(sample)[:_PROBES]
    overlaps: list[float] = []
    self_misses: list[str] = []
    dominated = 0
    for i in probes:
        rec = src_recs.get(i)
        if rec is None:
            continue
        project, store = _key(rec.metadata)
        where = (
            {"project": project, "store": store}
            if "store" in rec.metadata
            else {"project": project}
        )
        # Store.query: `where` is a metadata filter dict, not SQL.
        # pi-lens-ignore: python-sql-injection
        a = source.query(rec.embedding, _K, where)
        # pi-lens-ignore: python-sql-injection
        b = target.query(rec.embedding, _K, where)
        if not a:
            continue
        overlaps.append(len({h.id for h in a} & {h.id for h in b}) / len(a))
        top = b[0].score if b else -1.0
        if top < 1 - 1e-5 or i not in {h.id for h in b if h.score >= top - 1e-6}:
            self_misses.append(i)
        if len(b) < len(a) or b[len(a) - 1].score < a[-1].score - 1e-5:
            dominated += 1
    if self_misses:
        problems.append(
            f"{len(self_misses)} records did not find themselves in the target "
            f"(e.g. {self_misses[:3]})"
        )
    if dominated:
        problems.append(
            f"on {dominated} probes the target's top-{_K} scored worse than the source's"
        )
    parity = sum(overlaps) / len(overlaps) if overlaps else 1.0

    return {
        "ok": not problems,
        "checks": {
            "source_total": len(src),
            "target_total": len(dst),
            "partitions": len(src_parts),
            "vectors_compared": len(sample),
            "worst_vector_cosine": round(worst, 9),
            "top_k_probes": len(overlaps),
            "top_k_overlap": round(parity, 4),  # informational: see above
            "self_retrieval_misses": len(self_misses),
            "score_dominance_failures": dominated,
        },
        "problems": problems,
    }


def migrate(source: Store, target: Store, *, batch: int = BATCH) -> dict[str, Any]:
    """Copy every record the target lacks (or holds stale), remove what only the
    target holds, then verify. Safe to rerun; a rerun after an interruption is
    the resume."""
    t0 = time.perf_counter()
    _check_dims(source, target)
    dst = {i: _hash(m) for i, m in target.get_meta({}).items()}
    seen: set[str] = set()
    copied = 0
    for page in source.iter_records(batch):
        seen.update(r.id for r in page)
        todo = [r for r in page if dst.get(r.id, object()) != _hash(r.metadata)]
        if todo:
            target.upsert(todo)
            copied += len(todo)
    stale = [i for i in dst if i not in seen]
    for lo in range(0, len(stale), batch):
        target.delete(stale[lo : lo + batch])
    copy_s = time.perf_counter() - t0
    report = verify(source, target)
    report.update(
        copied=copied,
        removed=len(stale),
        copy_seconds=round(copy_s, 2),
        total_seconds=round(time.perf_counter() - t0, 2),
    )
    return report
