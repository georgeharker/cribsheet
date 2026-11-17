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
- a declared facet's field value that doesn't fit its type (`timeout: 30s`);
- a declared facet's item with no title (none of its `title_from` sources);
- near-duplicate notes / decisions / plans (stored vectors, all pairs);
- symbols waiting for a description (outside generation mode "on").
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from . import facets

if TYPE_CHECKING:
    from .app import Crib

# Cosine at or above which two items of one kind are reported as near-duplicates.
# Measured on the live corpus (1,141 items, 2026-11-13): an item's nearest
# same-kind neighbour sits at 0.54 (median) to 0.84 (p99); nothing fell between
# 0.88 and 0.93, and every pair from 0.94 up was a real duplicate (leftover
# test plans, a decision stored twice, near-identical notes).
NEAR_DUPLICATE = 0.90

# An item whose status plays one of these roles is history: what it depends on
# no longer needs keeping current (a decision superseded, a plan item declined
# or finished, or any facet's equivalent).
_SETTLED_ROLES = ("retired", "dropped", "done")


def _role_of(kind: str, status: str | None) -> str | None:
    """The role `status` plays in facet `kind` (None: not a facet's, or none)."""
    spec = facets.get(kind)
    return spec.role(status) if spec is not None else None


def diagnose(crib: Crib, proj: str) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    from .designs import Graph
    from .notestore import PendingMove

    for store in crib.pillars.values():
        if (why := store.pending_move(proj)) is not None:
            findings.append(
                {
                    "check": "waiting to be moved",
                    "kind": store.spec.facet or "note",
                    "detail": why,
                    "fix": "crib project adopt (in the repo that maps it)",
                }
            )
    try:
        graph = crib.designs._load_graph(proj)
    except PendingMove:  # reported above; the graph checks wait for the move
        graph = Graph(proj=proj)

    from . import verbs

    for node in graph.nodes.values():
        spec = facets.by_noun(node.kind)
        tool = verbs.view(spec).tool  # the item's own facet's verbs
        if spec.role(node.status) in _SETTLED_ROLES:
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
                        "fix": f"{tool('dep_remove')} {node.relpath} {dep} (or restore the target)",
                    }
                )
            elif (t_role := _role_of(target.kind, target.status)) in ("retired", "dropped"):
                by = graph.nodes.get(target.superseded_by)
                to = by.relpath if by is not None else "<the replacement>"
                fix = (
                    f"re-point it: {tool('dep_add')} {node.relpath} {to}, then "
                    f"{tool('dep_remove')} {node.relpath} {target.relpath}"
                    if t_role == "retired"
                    else f"{tool('dep_remove')} {node.relpath} {target.relpath} "
                    f"(a {target.status} {target.kind} will not happen)"
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
                        f"read {cite}, then {tool('reaffirm')} {node.relpath}"
                        if not gone
                        else f"{tool('read')} {node.relpath} and re-cite or drop the source"
                    ),
                }
            )

    for row in crib.learnings.report(proj):
        if row["status"] == "ok":
            continue
        best = (row.get("candidates") or [None])[0]
        fix = row.get("next") or (
            best["next"] + (f" (best of {len(row['candidates'])} candidates)"
                            if len(row["candidates"]) > 1 else "")
            if best else f"no candidate symbol: learning_forget {row['symbol']} if it is gone")
        findings.append(
            {
                "check": f"learning {row['status']}",
                "item": row["relpath"],
                "kind": "learning",
                "symbol": row["symbol"],
                **({"now_in": row["new_file"]} if row.get("new_file") else {}),
                "fix": fix,
            }
        )

    findings += _duplicate_ids(crib, proj)
    findings += _near_duplicates(crib, proj)
    findings += _unstamped(crib, proj)
    findings += _mistyped_fields(crib, proj)
    findings += _untitled(crib, proj)

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


def _mistyped_fields(crib: Crib, proj: str) -> list[dict[str, Any]]:
    """A declared facet's field whose value in a file doesn't fit its type
    (`timeout: 30s` on an int field): the index keeps it as empty, so the item
    quietly drops out of every filter on that field. Read from the files (the
    truth), mapped or not; hand-edited ones are where this happens."""
    from . import notes
    from .errors import CribUserError
    from .filesync import scan

    out = []
    for spec in facets.FACETS:
        if spec.builtin or not spec.params or spec.noun in crib.pending_facets:
            continue
        store = crib.pillars.get(spec.store)
        if store is None:
            continue
        loc = store.location(proj)
        try:
            root = loc.root if loc is not None else store.root(proj)
        except CribUserError:  # pending a move, or not on this machine
            continue
        if not root.is_dir():
            continue
        for rel in sorted(scan(root, ".md", recursive=True)):
            if loc is not None and not loc.owns(rel):
                continue
            try:
                fm, _body = notes.parse((root / rel).read_text())
            except (OSError, UnicodeDecodeError, notes.NoteParseError):
                continue
            out += [{"item": rel, **f} for f in _misfits(spec, fm)]
    return out


def _untitled(crib: Crib, proj: str) -> list[dict[str, Any]]:
    """A declared facet's items that came out with no title: none of its
    `title_from` sources (default: the frontmatter `title`) yielded one. The
    facet insists on titles; name each, and the two ways out."""
    out = []
    for spec in facets.FACETS:
        if spec.builtin or spec.noun in crib.pending_facets:
            continue
        for it in crib.store.items({"project": proj, "kind": spec.noun}, vectors=False).values():
            if not (it.title or "").strip():
                out.append({
                    "check": "no title",
                    "item": it.relpath,
                    "kind": spec.noun,
                    "fix": f"give it a `title:` (or a `# heading`), or let the facet "
                    f"name its items another way: `title_from = [\"title\", "
                    f"\"heading\", \"filename\"]` in its [{spec.noun}] spec",
                })
    return sorted(out, key=lambda f: f["item"])


def _misfits(spec: Any, fm: dict[str, Any]) -> list[dict[str, Any]]:
    """The findings for one file's frontmatter: each field value that doesn't
    fit its declared type (fields.coerce refuses it)."""
    from . import fields as _fields
    from .errors import CribUserError

    out = []
    for name, kind in spec.params.items():
        value = fm.get(name)
        if value is None:
            if name in spec.required:
                out.append({
                    "check": "required field missing",
                    "kind": spec.noun,
                    "field": name,
                    "fix": f"give it `{name}:`, or mark the field optional in the "
                    f"[{spec.noun}] spec (`optional = [\"{name}\"]`)",
                })
            continue
        try:
            _fields.coerce(spec, name, value)
        except CribUserError:
            takes = "an ISO date (YYYY-MM-DD)" if kind == "date" else f"an {kind}"
            out.append({
                "check": "field value doesn't fit its type",
                "kind": spec.noun,
                "field": name,
                "value": str(value),
                "fix": f"`{name}` takes {takes}: correct it in the file (until then "
                f"the item has no value for it, so filters on `{name}` miss it)",
            })
    return out


def _unstamped(crib: Crib, proj: str) -> list[dict[str, Any]]:
    """A read-only, id-stamped mapped location's files with no id yet: they are
    not indexed (a temporary id would flip under their links), so say so, and
    what makes them appear."""
    from . import notes
    from .filesync import scan

    out = []
    for store in crib.pillars.values():
        loc = store.location(proj)
        if loc is None or not loc.read_only or loc.ids != "stamp" or not loc.available:
            continue
        for rel in sorted(scan(loc.root, ".md", recursive=True)):
            if not loc.owns(rel):
                continue
            try:
                has_id = bool(notes.scan_id((loc.root / rel).read_text()))
            except (OSError, UnicodeDecodeError):
                continue
            if not has_id:
                out.append(
                    {
                        "check": "not yet stamped",
                        "item": rel,
                        "kind": store.spec.facet or "note",
                        "location": loc.token,
                        "fix": "index it from a writable checkout of that repo (it "
                        "stamps an id) and commit; this read-only one then picks it up",
                    }
                )
    return out


def _duplicate_ids(crib: Crib, proj: str) -> list[dict[str, Any]]:
    """One id in several files (an interrupted move): from the chunk metadata."""
    out = []
    for kind in facets.nouns():
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
    from . import verbs

    out = []
    for kind in facets.item_kinds():
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
            spec = facets.by_noun(kind)
            view = verbs.view(spec)
            tool = view.tool
            change = " / ".join(tool(v) for v in ("append", "edit") if view.verb(v))
            out.append(
                {
                    "check": "near-duplicate",
                    "kind": kind,
                    "items": [a.relpath, b.relpath],
                    "titles": [a.title, b.title],
                    "similarity": round(float(S[i, j]), 3),
                    "fix": (
                        f"{tool('supersede')} {b.relpath} {a.relpath} if one replaces the other"
                        if spec.has("supersede") and view.verb("supersede")
                        else f"merge into one ({change}), then delete the other"
                    ),
                }
            )
    return out
