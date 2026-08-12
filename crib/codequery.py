"""Code-index queries (lookup / xref / dossier / graph), extracted from Crib.

CodeQuery answers questions against the PERSISTED symbol_index via the resident cache
— no live LSP. It depends on `refs` (cross-project fan-out + symbol resolution),
`learnings` (annotate hits with pinned notes), the query `embedder`, and two injected
Crib callables it can't own yet: `resident` (a project's resident cache, which carries
the pipeline revalidate hook) and `require_index` (the self-diagnosing "is this project
indexed" guard). Cores take an explicit resolved `project`; Crib keeps resolve + delegate.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any, Callable

from .errors import CribUserError

if TYPE_CHECKING:
    from .codestore import _ResidentCode
    from .embed import Embedder
    from .learnings import Learnings
    from .refs import Refs

_POOL_K = 50        # top-K per source for the union candidate pool (min-max support)
_RERANK_N = 20      # candidates carried for the cross-encoder rerank stage

# Arguments that reach here come straight off an agent's tool call, so the bounds
# are stated once and enforced — an out-of-range or misspelled value gets an error
# that says what IS accepted, instead of a silent default (a typo'd
# `direction="calls"` used to mean `callees`, so the graph answered a question
# nobody asked) or a walk deep enough to be an outage.
GRAPH_DIRECTIONS = {"callees": "calls", "callers": "called_by",
                    "references": "references"}
# `tree` is the pstree view for a human reading one chain; `edges` is the
# SUBGRAPH — the shape you need the moment convergence is the point (several
# paths landing on one symbol), because a tree can only show a shared node by
# duplicating it or truncating it, and both hide the fact.
GRAPH_SHAPES = ("tree", "edges")
GRAPH_GROUPINGS = ("module",)
MAX_K = 200
MAX_DEPTH = 25
# Whole-project symbol-level export backstop. A rolled-up (`group_by="module"`)
# export is small whatever the repo size, so the cap applies only to the raw
# symbol graph, and it errors rather than truncating — a graph silently missing
# nodes is worse than no graph.
MAX_GRAPH_NODES = 5000


def _edge_target(ref: str, p: str) -> tuple[str, str, str, str]:
    """Split an index edge ref — `name`, `name [rel]` or `name [proj:rel]` — into
    (project, name, file-rel, raw-ref); a QUALIFIED ref hops into that project."""
    name, _, rest = ref.partition(" [")
    fref = rest.rstrip("]")
    if ":" in fref:
        tp, _, trel = fref.partition(":")
        return tp, name.strip(), trel, fref
    return p, name.strip(), fref, fref


def _rollup_modules(sub: dict[str, Any], proj: str) -> dict[str, Any]:
    """Collapse a symbol subgraph into MODULE-to-MODULE edges carrying the number of
    distinct symbol edges they stand for — the architecture view, where what matters
    is which files depend on which and how heavily. Self-edges (a file calling
    itself) are kept and left for the consumer to filter: they are real, and
    dropping them here would misreport a module's internal cohesion as zero."""
    member: dict[str, str] = {}
    mods: dict[str, dict[str, Any]] = {}
    for n in sub["nodes"]:
        p = str(n.get("project") or proj)
        f = str(n.get("file") or "")
        mid = (f or "(unresolved)") if (n.get("external") or p == proj) else f"{p}:{f}"
        member[str(n["id"])] = mid
        m = mods.get(mid)
        if m is None:
            m = {"id": mid, "file": f, "symbols": 0, "external": True}
            if p != proj and not n.get("external"):
                m["project"] = p
            if "depth" in n:
                m["depth"] = n["depth"]
            mods[mid] = m
        m["symbols"] += 1
        if not n.get("external"):
            m["external"] = False
        if "depth" in n and "depth" in m:
            m["depth"] = min(m["depth"], n["depth"])
    for m in mods.values():
        if not m["external"]:
            del m["external"]
    medges: dict[tuple[str, str], dict[str, Any]] = {}
    for e in sub["edges"]:
        a, b = member[e["from"]], member[e["to"]]
        key = (a, b)
        cur = medges.get(key)
        if cur is None:
            medges[key] = {"from": a, "to": b, "kind": e["kind"], "weight": 1}
        else:
            cur["weight"] += 1
    out = {k: v for k, v in sub.items() if k not in ("nodes", "edges")}

    def order(m: dict[str, Any]) -> tuple[Any, str]:
        return (m.get("depth", 0), str(m["id"]))

    return {**out, "group_by": "module",
            "nodes": sorted(mods.values(), key=order),
            "edges": sorted(medges.values(), key=lambda x: (x["from"], x["to"]))}


def check_k(k: int, name: str = "k") -> int:
    if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= MAX_K:
        raise CribUserError(f"{name} must be an integer in 1..{MAX_K}, got {k!r}")
    return k


def check_query(query: str, name: str = "query") -> str:
    if not (query or "").strip():
        raise CribUserError(
            f"empty {name}: describe what you're looking for — a concept "
            '("where do we fuse ranked lists") or a symbol name')
    return query


class CodeQuery:
    def __init__(self, refs: Refs, learnings: Learnings, embedder: Embedder,
                 resident: Callable[[str], _ResidentCode],
                 require_index: Callable[[str], None]) -> None:
        self.refs = refs
        self.learnings = learnings
        self.embedder = embedder
        self._resident = resident
        self._require_index = require_index

    def xref(self, proj: str, symbol: str) -> list[dict[str, Any]]:
        """Callers/callees for a symbol from the persisted symbol_index — no live LSP.
        A local miss falls through to the project's `.crib` `refs:`; every entry carries
        `project`."""
        check_query(symbol, "symbol")
        self._require_index(proj)
        rc = self._resident(proj)
        matches = rc.by_fqname(symbol)
        owner = proj
        if not matches:
            for ref in self.refs.project_refs(proj):
                if not ref["indexed"]:
                    continue
                matches = self._resident(ref["project"]).by_fqname(symbol)
                if matches:
                    owner = ref["project"]
                    break
        for m in matches:
            m["project"] = owner
        return self.learnings.attach(owner, matches)

    def dossier(self, proj: str, symbol: str, edge_cap: int = 20) -> dict[str, Any]:
        """Everything about ONE symbol: signature + description, its callers/callees/
        references each annotated with the NEIGHBOUR'S description, and any learning."""
        check_query(symbol, "symbol")
        self._require_index(proj)
        rc = self._resident(proj)
        # local first, then the `.crib` refs — the neighbourhood (edges, learnings)
        # lives with the OWNING project, so everything below reads from there
        owner, entry = self.refs.resolve_symbol_or_ref(proj, symbol, rc)
        if owner != proj:
            rc = self._resident(owner)
        self.learnings.attach(owner, [entry])
        idx = rc.entries
        desc = {e["fqname"]: e.get("description", "") for e in idx}
        by_nf = {(e.get("name", ""), e.get("file", "")): e["fqname"] for e in idx}

        ref_maps: dict[str, tuple[dict, dict]] = {}   # ref proj → (desc, by_nf)

        def _maps(rp: str) -> tuple[dict, dict]:
            if rp not in ref_maps:
                try:
                    rrc = self._resident(rp)
                    ref_maps[rp] = (
                        {e["fqname"]: e.get("description", "") for e in rrc.entries},
                        {(e.get("name", ""), e.get("file", "")): e["fqname"]
                         for e in rrc.entries})
                except Exception:  # noqa: BLE001 — unindexed ref → unresolved edge
                    ref_maps[rp] = ({}, {})
            return ref_maps[rp]

        def neigh(edges: list[str] | None) -> list[dict[str, Any]]:
            out = []
            for ref in (edges or [])[:edge_cap]:
                name, _, rest = ref.partition(" [")
                nm, fref = name.strip(), rest.rstrip("]")
                if ":" in fref:            # QUALIFIED edge — lives in a ref'd project
                    rp, _, rrel = fref.partition(":")
                    rdesc, rnf = _maps(rp)
                    fq = rnf.get((nm, rrel))
                    out.append({"symbol": fq or nm, "file": rrel, "project": rp,
                                "description": rdesc.get(fq or "", "")})
                    continue
                fq = by_nf.get((nm, fref))
                out.append({"symbol": fq or nm, "file": fref,
                            "description": desc.get(fq or "", "")})
            extra = max(len(edges or []) - edge_cap, 0)
            if extra:
                out.append({"symbol": f"… +{extra} more", "file": "", "description": ""})
            return out

        return {
            "fqname": entry["fqname"], "kind": entry.get("kind"),
            "project": owner,
            "file": entry.get("file"), "line": entry.get("line"),
            "signature": entry.get("signature"), "description": entry.get("description"),
            "learning": entry.get("learning"),
            "calls": neigh(entry.get("calls")),
            "called_by": neigh(entry.get("called_by")),
            "references": neigh(entry.get("references")),
        }

    def lookup(self, proj: str, query: str, k: int = 8) -> list[dict[str, Any]]:
        """Find a symbol — HYBRID: raw-cosine dense ⊕ coverage-gated BM25 over the
        expanded field (name ⊕ synth keywords), range-matched blend (see `_lookup_one`).
        FANS OUT to the project's `.crib` `refs:`; the per-project rankings RRF-fuse
        (queried project weighted above its refs). Every hit carries `project`."""
        from .retrieve import reciprocal_rank_fusion
        check_query(query)
        check_k(k)
        self._require_index(proj)
        pools: dict[str, list[dict[str, Any]]] = {
            proj: self._lookup_one(proj, query, k)}
        for ref in self.refs.project_refs(proj):
            if not ref["indexed"] or ref["project"] in pools:
                continue
            try:
                pools[ref["project"]] = self._lookup_one(ref["project"], query, k)
            except Exception:  # noqa: BLE001 — a broken ref never fails the query
                continue
        if len(pools) == 1:
            hits = pools[proj][:k]
        else:
            # EQUAL weights: rank decides (a ref's best hit must be able to beat a
            # local mid-ranker — a damped weight buries refs below any full local
            # top-k, since RRF is rank- not score-based). The queried project is the
            # FIRST ranking, so exact rank ties break local-first.
            by_key = {f"{p}:{h['fqname']}": h for p, hs in pools.items() for h in hs}
            fused = reciprocal_rank_fusion(
                [[f"{p}:{h['fqname']}" for h in hs] for p, hs in pools.items()])
            hits = [by_key[key] for key in fused[:k]]
        for i, h in enumerate(hits):
            h["rank"] = i + 1
        return hits

    def _lookup_one(self, proj: str, query: str, k: int) -> list[dict[str, Any]]:
        """The single-project core of lookup. SPARSE = coverage-gated BM25 over the
        EXPANDED field (name ⊕ synth keywords) — the keywords let a behavioral query hit
        the sparse arm the terse name can't. DENSE = raw cosine over descriptions. Blended
        DENSE-DOMINANT: only the (uncalibrated) BM25 side is min-max'd, over the union
        candidate pool; raw cosine is already calibrated. `_score` carries the blend for
        the range-matched rerank stage; `rank` is per-pool, re-ranked after fusion."""
        from .retrieve import STOPWORDS, _subtokens, tokenize
        rc = self._resident(proj)                            # resident: no re-parse/re-embed
        if not rc.lk:
            return []
        ids = rc.lk_ids
        by_id = rc.by_fq
        n = len(ids)
        qt = tokenize(query) + _subtokens(query)
        # dense: raw cosine over the resident description embeddings (only the query embeds)
        dense_v = rc.dense(self.embedder)
        if any(v for v in dense_v):
            qv = self.embedder.embed_query([query])[0]
            dense = [sum(a * b for a, b in zip(qv, v)) if v else -1.0 for v in dense_v]
        else:
            dense = [0.0] * n
        # sparse: coverage-gated BM25 over the expanded field (name ⊕ keywords)
        Q = {t for t in set(qt) if len(t) > 1 and t not in STOPWORDS}
        bmsc = rc.bm25.scores(qt)
        cov = rc.coverage(Q)
        gated = {i: cov[i] * bmsc[i] for i in range(n) if cov[i] * bmsc[i] > 0}
        # union pool (top-K each) → min-max ONLY the gated side → dense-dominant blend
        dtop = sorted(range(n), key=lambda i: dense[i], reverse=True)[:_POOL_K]
        gtop = sorted(gated, key=lambda i: gated[i], reverse=True)[:_POOL_K]
        pool = list(dict.fromkeys(dtop + gtop))
        if gated:
            gv = [gated.get(i, 0.0) for i in pool]
            lo, hi = min(gv), max(gv)
            rng = (hi - lo) or 1.0
            gnorm = {i: (gated.get(i, 0.0) - lo) / rng for i in pool}
        else:
            gnorm = {i: 0.0 for i in pool}
        score = {i: dense[i] + gnorm[i] for i in pool}       # alpha = beta = 1
        order = sorted(pool, key=lambda i: score[i], reverse=True)[:max(k, _RERANK_N)]
        keys = ("fqname", "name", "kind", "file", "line", "signature", "description",
                "parent", "calls", "called_by", "references", "content_hash", "keywords")
        hits = [{**{key: by_id[ids[i]].get(key) for key in keys},
                 "project": proj, "rank": r + 1, "_score": score[i]}
                for r, i in enumerate(order)]
        return self.learnings.attach(proj, hits)

    def graph(self, proj: str, symbol: str | None = None,
              direction: str = "callees", depth: int = 6, shape: str | None = None,
              group_by: str | None = None) -> dict[str, Any]:
        """Call graph around `symbol` from the persisted symbol_index — `callees`
        follows `calls`, `callers` follows `called_by`, `references` the broader
        mention relation. Omit `symbol` for the WHOLE PROJECT: every indexed symbol
        and every edge between them, with no root and no depth bound (`shape="edges"`
        only — a tree has to start somewhere).

        `shape="tree"` (default) is the pstree view: nested {fqname, kind, file,
        line, children[]}, depth-first, DAG-repeats marked `repeat` and unresolved
        edges `external`.

        `shape="edges"` is the depth-bounded SUBGRAPH: {nodes[], edges[]}, walked
        breadth-first so every symbol appears ONCE at its shortest distance while
        EVERY edge is kept — including the ones back into already-visited nodes,
        which is exactly what the tree drops. Convergence (several paths ending at
        one symbol) is then a fact in the data, not something the reader has to
        reconstruct. Edges are deduplicated and oriented caller→callee regardless
        of walk direction, so the output drops straight into a layout tool.

        `group_by="module"` rolls those symbol edges up into weighted file-to-file
        edges — the architecture view — and implies `shape="edges"`.

        `symbol` may be a full qualified name, a trailing run of its segments, or a
        bare local name in any language's separator. It resolves through the shared
        resolver, so a bare name matching several symbols RAISES with the candidates
        (ranked by caller count) rather than picking one; every result carries
        `resolved` saying what the name became and which tier matched."""
        if direction not in GRAPH_DIRECTIONS:
            raise CribUserError(
                f"unknown direction {direction!r}: use one of "
                f"{', '.join(sorted(GRAPH_DIRECTIONS))}")
        if shape is not None and shape not in GRAPH_SHAPES:
            raise CribUserError(f"unknown shape {shape!r}: use one of "
                             f"{', '.join(GRAPH_SHAPES)}")
        if group_by is not None:
            if group_by not in GRAPH_GROUPINGS:
                raise CribUserError(f"unknown group_by {group_by!r}: use one of "
                                 f"{', '.join(GRAPH_GROUPINGS)}")
            if shape == "tree":                 # explicit and contradictory
                raise CribUserError(
                    "group_by rolls up an edge list, which a tree cannot carry: "
                    'drop shape="tree" (group_by implies shape="edges")')
            shape = "edges"                     # …otherwise group_by picks it
        if symbol is None:
            if shape == "tree":                 # explicit and impossible
                raise CribUserError(
                    "a whole-project graph has no root to hang a tree from: "
                    'drop shape="tree", or pass a symbol')
            shape = "edges"
        shape = shape or "tree"
        check_k(depth, "depth")
        if depth > MAX_DEPTH:
            raise CribUserError(f"depth must be 1..{MAX_DEPTH}, got {depth!r}")
        if symbol is not None:
            check_query(symbol, "symbol")
        self._require_index(proj)
        rc = self._resident(proj)
        entries = rc.entries
        # per-project (name, file) maps — the tree can cross into ref'd projects via
        # QUALIFIED edges ("name [proj:rel]") and keeps walking there
        nf_maps: dict[str, dict[tuple[str, str], dict]] = {}

        def _nf(p: str) -> dict[tuple[str, str], dict]:
            if p not in nf_maps:
                try:
                    m: dict[tuple[str, str], dict] = {}
                    for e in self._resident(p).entries:
                        m.setdefault((e.get("name", ""), e.get("file", "")), e)
                    nf_maps[p] = m
                except Exception:  # noqa: BLE001 — unindexed ref → external leaf
                    nf_maps[p] = {}
            return nf_maps[p]

        _nf(proj)
        edge = GRAPH_DIRECTIONS[direction]      # validated above
        if symbol is None:
            sub = self._project_graph(proj, entries, edge, direction, _nf,
                                      capped=group_by is None)
            return _rollup_modules(sub, proj) if group_by else sub
        # ONE resolver, the same one dossier and the learnings use. This used to be an
        # ad-hoc first-match scan with the resolver as a fallback for its MISSES, so
        # the resolver's unique-or-refuse rule was unreachable: a bare name matching
        # two symbols silently took whichever came first in (unsorted) store order.
        # `add_diagram_node` answered "0 callers" for the MCP wrapper while the op it
        # wraps had 92 — a confident, well-formed, empty answer to the question you
        # ask before deleting something. Catch the miss BY TYPE: an ambiguity is a
        # ValueError too, and swallowing it here would restore the silence in the
        # shape of "symbol not found".
        from .codeindex import fqname_match
        from .refs import UnknownSymbol
        try:
            root_proj, root = self.refs.resolve_symbol_or_ref(proj, symbol, rc)
        except UnknownSymbol:
            return {}
        resolved: dict[str, Any] = {
            "query": symbol, "fqname": root["fqname"],
            "via": fqname_match(root["fqname"], root.get("name", ""), symbol,
                                root.get("lang", ""))}
        if root_proj != proj:
            resolved["project"] = root_proj
        if shape == "edges":
            sub = self._subgraph(proj, root, root_proj, edge, direction, depth, _nf)
            sub["resolved"] = resolved        # set before the rollup, which carries it
            return _rollup_modules(sub, proj) if group_by else sub
        seen: set[str] = set()

        def build(e: dict, p: str, d: int) -> dict:
            node = {"fqname": e["fqname"], "kind": e.get("kind", ""),
                    "file": e.get("file", ""), "line": e.get("line"), "children": []}
            if p != proj:
                node["project"] = p
            key = f"{p}:{e['fqname']}"
            if key in seen:
                node["repeat"] = True
                return node
            seen.add(key)
            if d <= 0:
                return node
            for ref in e.get(edge) or []:
                tp, name, trel, fref = _edge_target(ref, p)
                child = _nf(tp).get((name, trel))
                if child:
                    node["children"].append(build(child, tp, d - 1))
                else:
                    node["children"].append({"fqname": name, "kind": "?",
                                             "file": fref, "external": True,
                                             "children": []})
            return node

        tree = build(root, root_proj, depth)
        # glyph carriers, per owning project (a cross-project node's learning lives with
        # ITS project, and same-named local fqns must not false-mark)
        marks: dict[str, set[str]] = {}
        stack = [tree]
        while stack:
            n = stack.pop()
            p = n.get("project") or proj
            if p not in marks:
                marks[p] = self.learnings.fqns(p)
            if n.get("fqname") in marks[p]:
                n["has_learning"] = True
            stack.extend(n.get("children") or [])
        tree["resolved"] = resolved       # additive on the root node; children unchanged
        return tree

    def _project_graph(self, proj: str, entries: list[dict], edge: str,
                       direction: str,
                       nf: Callable[[str], dict[tuple[str, str], dict]],
                       capped: bool = True) -> dict[str, Any]:
        """EVERY indexed symbol in the project and every edge between them — no root,
        no depth, nothing reachability can hide. The rooted walk answers "what does
        this touch"; this answers "what is the shape of the program", which is a
        different question and the one an architecture diagram asks. Symbols nothing
        calls (entry points, dead code, test helpers) are in here and cannot be in a
        rooted walk.

        Cross-project and unresolved edge TARGETS still appear as nodes, so the
        boundary of the project is visible; only local symbols are enumerated."""
        kind = "references" if direction == "references" else "calls"
        forward = direction == "callees"
        if capped and len(entries) > MAX_GRAPH_NODES:
            raise CribUserError(
                f"{len(entries)} symbols exceeds the {MAX_GRAPH_NODES}-node "
                'whole-project cap: pass group_by="module" for the rolled-up '
                "export (no cap), or give a symbol to walk from")
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[tuple[str, str], dict[str, Any]] = {}
        for e in entries:
            nodes[e["fqname"]] = {"id": e["fqname"], "fqname": e["fqname"],
                                  "kind": e.get("kind", ""), "file": e.get("file", ""),
                                  "line": e.get("line")}
        for e in entries:
            for ref in e.get(edge) or []:
                tp, name, trel, fref = _edge_target(ref, proj)
                target = nf(tp).get((name, trel))
                if target and tp == proj:
                    ci = target["fqname"]
                elif target:
                    ci = f"{tp}:{target['fqname']}"
                    nodes.setdefault(ci, {"id": ci, "fqname": target["fqname"],
                                          "kind": target.get("kind", ""),
                                          "file": target.get("file", ""),
                                          "line": target.get("line"), "project": tp})
                else:
                    ci = f"{name} [{fref}]" if fref else name
                    nodes.setdefault(ci, {"id": ci, "fqname": name, "kind": "?",
                                          "file": fref, "line": None,
                                          "external": True})
                a, b = (e["fqname"], ci) if forward else (ci, e["fqname"])
                edges.setdefault((a, b), {"from": a, "to": b, "kind": kind})
        pinned = self.learnings.fqns(proj)
        for n in nodes.values():
            if not n.get("external") and not n.get("project") \
                    and n["fqname"] in pinned:
                n["has_learning"] = True
        return {"project": proj, "scope": "project", "direction": direction,
                "shape": "edges",
                "nodes": sorted(nodes.values(), key=lambda n: str(n["id"])),
                "edges": sorted(edges.values(), key=lambda x: (x["from"], x["to"]))}

    def _subgraph(self, proj: str, root: dict, root_proj: str, edge: str,
                  direction: str, depth: int,
                  nf: Callable[[str], dict[tuple[str, str], dict]]) -> dict[str, Any]:
        """The depth-bounded SUBGRAPH: breadth-first, so each symbol is visited once
        at its SHORTEST distance from the root, and every edge out of an expanded
        node is recorded — including edges into nodes already visited. That last
        part is the whole difference from the tree, which drops them (`repeat`) and
        so cannot show that four paths converge on one symbol.

        Edges are deduplicated and oriented caller→callee whichever way we walked,
        because a graph consumer wants one consistent arrow, not one that flips with
        the query."""
        kind = "references" if direction == "references" else "calls"
        forward = direction == "callees"       # walking OUT of the caller
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[tuple[str, str], dict[str, Any]] = {}

        def nid(p: str, fq: str) -> str:
            return fq if p == proj else f"{p}:{fq}"

        def put(p: str, e: dict, d: int) -> str:
            i = nid(p, e["fqname"])
            n = nodes.get(i)
            if n is None:
                n = {"id": i, "fqname": e["fqname"], "kind": e.get("kind", ""),
                     "file": e.get("file", ""), "line": e.get("line"), "depth": d}
                if p != proj:
                    n["project"] = p
                nodes[i] = n
            elif d < n["depth"]:                # BFS makes this unreachable; cheap guard
                n["depth"] = d
            return i

        def put_external(name: str, fref: str, d: int) -> str:
            i = f"{name} [{fref}]" if fref else name    # unresolvable → its own raw ref
            nodes.setdefault(i, {"id": i, "fqname": name, "kind": "?", "file": fref,
                                 "line": None, "depth": d, "external": True})
            return i

        put(root_proj, root, 0)
        queue: deque[tuple[str, dict, int]] = deque([(root_proj, root, 0)])
        expanded: set[str] = set()
        while queue:
            p, e, d = queue.popleft()
            i = nid(p, e["fqname"])
            if i in expanded:
                continue
            if d >= depth:                      # frontier: reached, deliberately unwalked
                if e.get(edge):
                    nodes[i]["truncated"] = True
                continue
            expanded.add(i)
            for ref in e.get(edge) or []:
                tp, name, trel, fref = _edge_target(ref, p)
                child = nf(tp).get((name, trel))
                if child:
                    ci = put(tp, child, d + 1)
                    queue.append((tp, child, d + 1))
                else:
                    ci = put_external(name, fref, d + 1)
                a, b = (i, ci) if forward else (ci, i)
                edges.setdefault((a, b), {"from": a, "to": b, "kind": kind})
        marks: dict[str, set[str]] = {}
        for n in nodes.values():
            if n.get("external"):
                continue
            p = str(n.get("project") or proj)
            if p not in marks:
                marks[p] = self.learnings.fqns(p)
            if n["fqname"] in marks[p]:
                n["has_learning"] = True
        return {"root": nid(root_proj, root["fqname"]), "scope": "symbol",
                "direction": direction, "depth": depth, "shape": "edges",
                "nodes": sorted(nodes.values(), key=lambda n: (n["depth"], n["id"])),
                "edges": sorted(edges.values(), key=lambda x: (x["from"], x["to"]))}
