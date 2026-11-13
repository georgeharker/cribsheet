"""`doctor`: a project's health, report-only (Stage E1).

Reads what crib already knows (the design/plan graph, the learnings report,
the store's items and chunks) and lists what needs a human or an agent, each
finding naming the verb that resolves it. It never changes anything.

Checks:
- dangling dependencies (the target is in no pillar);
- dependencies on a superseded decision or a declined plan;
- citations whose source section is gone or has changed since recorded;
- learnings whose symbol moved or is gone;
- one id in several files;
- near-duplicate notes / decisions / plans (stored vectors, all pairs);
- symbols waiting for a description (outside generation mode "on").
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from .app import Crib

# Cosine at or above which two items of one kind are reported as near-duplicates.
# Measured on the live corpus (1,141 items, 2026-11-13): an item's nearest
# same-kind neighbour sits at 0.54 (median) to 0.84 (p99); nothing fell between
# 0.88 and 0.93, and every pair from 0.94 up was a real duplicate (leftover
# test plans, a decision stored twice, near-identical notes).
NEAR_DUPLICATE = 0.90

_PILLAR_VERB = {"design": "design", "plan": "plan"}
# An item in one of these states is history: what it depends on no longer
# needs keeping current.
_SETTLED = ("superseded", "declined", "done", "verified")


def diagnose(crib: Crib, proj: str) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    graph = crib.designs._load_graph(proj)

    for node in graph.nodes.values():
        verb = _PILLAR_VERB[node.kind]
        if node.status in _SETTLED:
            continue
        for dep in node.deps:
            target = graph.target(dep)
            if target is None:
                findings.append(
                    {
                        "check": "dangling dependency",
                        "item": node.relpath,
                        "kind": node.kind,
                        "dep": dep,
                        "fix": f"{verb}_dep_remove {node.relpath} {dep} (or restore the target)",
                    }
                )
            elif target.status in ("superseded", "declined"):
                by = graph.nodes.get(target.superseded_by)
                to = by.relpath if by is not None else "<the replacement>"
                fix = (
                    f"re-point it: {verb}_dep_add {node.relpath} {to}, then "
                    f"{verb}_dep_remove {node.relpath} {target.relpath}"
                    if target.status == "superseded"
                    else f"{verb}_dep_remove {node.relpath} {target.relpath} "
                    f"(a declined plan will not happen)"
                )
                findings.append(
                    {
                        "check": f"depends on a {target.status} {target.kind}",
                        "item": node.relpath,
                        "kind": node.kind,
                        "dep": target.relpath,
                        **({"replaced_by": by.relpath} if by is not None else {}),
                        "fix": fix,
                    }
                )
        for src in node.sources:
            if src.get("current") == src.get("hash"):
                continue
            cite = (
                f"{src['ref']}#{src['heading']}" if src.get("heading") else src["ref"]
            )
            gone = src.get("current") is None
            findings.append(
                {
                    "check": "cited section gone" if gone else "cited section changed",
                    "item": node.relpath,
                    "kind": node.kind,
                    "source": cite,
                    "fix": (
                        f"read {cite}, then {verb}_reaffirm {node.relpath}"
                        if not gone
                        else f"{verb}_read {node.relpath} and re-cite or drop the source"
                    ),
                }
            )

    for row in crib.learnings.report(proj):
        if row["status"] == "ok":
            continue
        findings.append(
            {
                "check": f"learning {row['status']}",
                "item": row["relpath"],
                "kind": "learning",
                "symbol": row["symbol"],
                **({"now_in": row["new_file"]} if row.get("new_file") else {}),
                "fix": (
                    f"learning_rehome {row['symbol']} <its new symbol>"
                    if row["status"] == "orphan"
                    else f"learning_rehome {row['symbol']} (its file is now {row['new_file']})"
                ),
            }
        )

    findings += _duplicate_ids(crib, proj)
    findings += _near_duplicates(crib, proj)

    gen = crib._generation_state([{"project": proj, "symbols": 1}])
    pending = (gen.get("pending_descriptions") or {}).get(proj)
    if pending:
        findings.append(
            {
                "check": "descriptions pending",
                "count": pending,
                "mode": gen["mode"],
                "fix": f"project_index {proj} (generation mode is {gen['mode']!r})",
            }
        )

    counts: dict[str, int] = {}
    for f in findings:
        counts[f["check"]] = counts.get(f["check"], 0) + 1
    return {
        "project": proj,
        "healthy": not findings,
        "counts": counts,
        "findings": findings,
    }


def _duplicate_ids(crib: Crib, proj: str) -> list[dict[str, Any]]:
    """One id in several files (an interrupted move): from the chunk metadata."""
    out = []
    for kind in ("note", "design", "plan", "learning"):
        by_id: dict[str, set[str]] = {}
        for m in crib.store.get_meta({"project": proj, "kind": kind}).values():
            if m.get("item_id") and m.get("relpath"):
                by_id.setdefault(str(m["item_id"]), set()).add(str(m["relpath"]))
        for nid, rels in sorted(by_id.items()):
            if len(rels) > 1:
                out.append(
                    {
                        "check": "one id in several files",
                        "kind": kind,
                        "id": nid,
                        "files": sorted(rels),
                        "fix": "keep one copy; move or delete the others (they share a version history)",
                    }
                )
    return out


def _near_duplicates(crib: Crib, proj: str) -> list[dict[str, Any]]:
    """Pairs of one kind whose stored body vectors are at least NEAR_DUPLICATE
    apart (exact, all pairs: a project's items fit one matrix)."""
    out = []
    for kind in ("note", "design", "plan"):
        items = [
            it
            for it in crib.store.items({"project": proj, "kind": kind}).values()
            if it.body_vec and any(it.body_vec)
        ]  # an empty body has no vector
        if len(items) < 2:
            continue
        M = np.asarray([it.body_vec for it in items], dtype=np.float32)
        M /= np.linalg.norm(M, axis=1, keepdims=True)
        S = M @ M.T
        rows, cols = np.nonzero(np.triu(S, 1) >= NEAR_DUPLICATE)
        for i, j in sorted(zip(rows.tolist(), cols.tolist()), key=lambda p: -S[p]):
            a, b = items[i], items[j]
            verb = {"note": "note", "design": "design", "plan": "plan"}[kind]
            out.append(
                {
                    "check": "near-duplicate",
                    "kind": kind,
                    "items": [a.relpath, b.relpath],
                    "titles": [a.title, b.title],
                    "similarity": round(float(S[i, j]), 3),
                    "fix": (
                        f"design_supersede {b.relpath} {a.relpath} if one replaces the other"
                        if kind == "design"
                        else f"merge into one ({verb}_append / {verb}_edit), then delete the other"
                    ),
                }
            )
    return out
