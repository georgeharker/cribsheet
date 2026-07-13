"""Code-index queries (lookup / xref / dossier / graph), extracted from Crib.

CodeQuery answers questions against the PERSISTED symbol_index via the resident cache
— no live LSP. It depends on `refs` (cross-project fan-out + symbol resolution),
`learnings` (annotate hits with pinned notes), the query `embedder`, and two injected
Crib callables it can't own yet: `resident` (a project's resident cache, which carries
the pipeline revalidate hook) and `require_index` (the self-diagnosing "is this project
indexed" guard). Cores take an explicit resolved `project`; Crib keeps resolve + delegate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .codestore import _ResidentCode
    from .embed import Embedder
    from .learnings import Learnings
    from .refs import Refs

_POOL_K = 50        # top-K per source for the union candidate pool (min-max support)
_RERANK_N = 20      # candidates carried for the cross-encoder rerank stage


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

    def graph(self, proj: str, symbol: str, direction: str = "callees",
              depth: int = 6) -> dict[str, Any]:
        """Call-graph TREE around `symbol` from the persisted symbol_index — `callees`
        follows `calls`, `callers` follows `called_by`. Nested {fqname, kind, file, line,
        children[]} with DAG-repeats marked `repeat` and unresolved edges `external`."""
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
        root = (rc.by_fq.get(symbol)
                or next((e for e in entries if e.get("name") == symbol
                         or e["fqname"].endswith("." + symbol)), None))
        root_proj = proj
        if not root:                          # local miss → the `.crib` refs
            try:
                root_proj, root = self.refs.resolve_symbol_or_ref(proj, symbol, rc)
            except ValueError:
                return {}
        edge = {"callees": "calls", "callers": "called_by",
                "references": "references"}.get(direction, "calls")
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
                name, _, rest = ref.partition(" [")
                fref = rest.rstrip("]")
                if ":" in fref:               # qualified → hop into the ref project
                    tp, _, trel = fref.partition(":")
                else:
                    tp, trel = p, fref
                child = _nf(tp).get((name.strip(), trel))
                if child:
                    node["children"].append(build(child, tp, d - 1))
                else:
                    node["children"].append({"fqname": name.strip(), "kind": "?",
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
        return tree
