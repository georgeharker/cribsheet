"""`related <ref>`: one item and everything around it, across kinds (Stage E2).

For a note, decision, plan, learning or code symbol: its declared links both
ways (deps and their kinds, dependents, drawn_from, superseded_by, learns,
member_of, calls/references for a symbol), and its nearest items of every
kind by stored vector, each with its status. Read-only.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from .errors import CribUserError

if TYPE_CHECKING:
    from .app import Crib

KINDS = ("note", "design", "plan", "learning", "symbol")
_PILLAR_KINDS = ("note", "design", "plan", "learning")


def _brief(it: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": it.id,
        "kind": it.kind,
        "title": it.title,
        "relpath": it.relpath,
    }
    if it.facet.get("status"):
        out["status"] = it.facet["status"]
    return out


def _find(crib: Crib, proj: str, ref: str) -> Any:
    """The item `ref` names: an item id, a pillar file's relpath (or its stem),
    a decision/plan by any ref the graph resolves, or a code symbol."""
    store = crib.store
    hit = store.items({"id": ref})
    if hit:
        return next(iter(hit.values()))
    rel = ref if ref.endswith(".md") else f"{ref}.md"
    for kind in _PILLAR_KINDS:
        hit = store.items({"project": proj, "kind": kind, "relpath": rel})
        if hit:
            return next(iter(hit.values()))
    # a decision / plan by title, slug or id prefix; then a code symbol. Each
    # resolver raises when the ref isn't one of its kind: try the next.
    with contextlib.suppress(CribUserError, ValueError):
        node = crib.designs._resolve_ref(crib.designs._load_graph(proj), ref)
        hit = store.items({"id": node.id})
        if hit:
            return next(iter(hit.values()))
    with contextlib.suppress(Exception):
        from .items import symbol_item_id
        from .symbols import key as symbol_key

        entry = crib.refs.resolve_symbol(proj, ref)
        hit = store.items({"id": symbol_item_id(proj, symbol_key(entry))})
        if hit:
            return next(iter(hit.values()))
    raise CribUserError(
        f"no item {ref!r} in project {proj!r}: give an id, a relpath, a "
        f"decision/plan title, or a symbol"
    )


def related(crib: Crib, proj: str, ref: str, k: int = 3) -> dict[str, Any]:
    store = crib.store
    item = _find(crib, proj, ref)
    # through Store.edges, which resolves a bare `dep` to its kind, both ways
    out_edges = [(dst, kind, h) for _s, dst, kind, h in store.edges({"src": item.id})]
    in_edges = [(src, kind) for src, _dst, kind, _h in store.edges({"dst": item.id})]
    ids = {d for d, _k, _h in out_edges} | {s for s, _k in in_edges}
    known = store.items({"id": {"$in": sorted(ids)}}, vectors=False) if ids else {}

    def link(other: str, kind: str) -> dict[str, Any]:
        it = known.get(other)
        return {"link": kind, **(_brief(it) if it is not None else {"ref": other})}

    links_out = [link(d, k) for d, k, _h in out_edges]
    links_in = [link(s, k) for s, k in in_edges]
    similar: dict[str, list[dict[str, Any]]] = {}
    if item.body_vec and any(item.body_vec):
        for kind in KINDS:
            top = store.body_vector_top(
                {"project": proj, "kind": kind}, list(item.body_vec), k + 1
            )
            top = [(i, s) for i, s in top if i != item.id][:k]
            if not top:
                continue
            rows = store.items({"id": {"$in": [i for i, _s in top]}}, vectors=False)
            similar[kind] = [
                {**_brief(rows[i]), "similarity": round(s, 3)}
                for i, s in top
                if i in rows
            ]
    return {
        "project": proj,
        "item": _brief(item),
        "links_out": links_out,
        "links_in": links_in,
        "similar": similar,
    }
