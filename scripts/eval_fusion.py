#!/usr/bin/env python3
"""Fusion test bed for code_lookup ranking — prototype the standard kw+dense techniques
from the IR literature on the 30 concept queries from the mcp-companion session (+ terse
and name segments), scoring P@1 / R@3-per-segment / MRR, WITHOUT touching crib source.

Techniques (all computed from the resident index; see crib note "code_lookup ranking:
fusion is the lever"):
  current        the shipped code_lookup path (coverage-gated blend + rerank; baseline)
  convex·α       normalized linear fusion  α·mm(dense) + (1-α)·mm(bm_name)   [Bruch et al.]
  covpc·a        per-candidate coverage reweight of the sparse term          [~ColBERT-lite]
  bm25f          field-weighted lexical: name⊕description⊕signature BM25      [BM25F]
  graph·g        + query-independent authority prior from call-graph in-degree
  colbert        soft MaxSim late interaction over token embeddings          [ColBERT]
  ltr            logistic learning-to-rank over all features, leave-one-query-out
  *+rerank       cross-encoder precision stage stacked on a base

    python scripts/eval_fusion.py                 # fusion family + graph + rerank
    python scripts/eval_fusion.py --heavy         # + colbert-lite + ltr (slower)
    python scripts/eval_fusion.py --per-query covpc·a2

Scoping rule: BM25/coverage stats are computed over BM25's OWN candidate set, never the
whole corpus. Dense is full-support.
"""
from __future__ import annotations
import argparse
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vendor" / "llmkit" / "src"))

MC, CS = "mcp-companion", "cribsheet"

GOLD: list[tuple[str, str, list[str], str]] = [
 ("concept", MC, ["ConnectionManager"], "persistent manager that keeps upstream MCP client sessions alive across tool calls"),
 ("concept", MC, ["_monitor"], "background health check that pings upstreams and reconnects with exponential backoff"),
 ("concept", MC, ["_reconnect"], "after repeated failed reconnects, hard restart the backing process"),
 ("concept", MC, ["_fetch_or_join"], "single-flight coalescing so concurrent tools/list cache misses share one upstream fetch"),
 ("concept", MC, ["_merge_stale_server_tools"], "re-inject last known good tools for a server that dropped out mid-reconnect (stale hysteresis)"),
 ("concept", MC, ["invalidate_tool_cache"], "invalidate tool cache and broadcast tools/list_changed to all sessions"),
 ("concept", MC, ["prime_server_tools"], "list a freshly mounted server's tools then mark it ready — the started to ready transition"),
 ("concept", MC, ["_proactive_refresh"], "proactively refresh the OAuth access token using the refresh token before it's found stale"),
 ("concept", MC, ["_apply_network_grace_window"], "grace window preserving an expired token when the network is unreachable to avoid full re-auth"),
 ("concept", MC, ["call_nvim_tool"], "route a neovim_ virtual tool call back to the editor instance over the back channel"),
 ("concept", MC, ["_instance_for_session"], "find which neovim editor instance owns a given chat session token"),
 ("concept", MC, ["permissions.enforce", ".enforce"], "enforce allow/deny/elicit permission policy before running a tool call"),
 ("concept", MC, ["_apply_session_filter", "SessionRegistry"], "disable a server for a single chat session only (per-session blocklist)"),
 ("concept", MC, ["_rewrite_app_meta"], "rewrite MCP-Apps UI resourceUri pointers inside tool _meta to match namespaced resources"),
 ("concept", MC, ["_sanitize_tools"], "rebuild tools to strip circular $ref schemas that crash pydantic model_dump"),
 ("concept", MC, ["_is_auth_error"], "decide whether an exception is an OAuth or authentication failure (401 403)"),
 ("concept", MC, ["_is_transport_dead"], "tell whether the upstream transport or subprocess is dead versus an ordinary tool error"),
 ("concept", MC, ["SharedServerManager"], "manage shared backing server processes: start, stop, restart the external process"),
 ("concept", MC, ["restart_server"], "restart a single mounted upstream server: unmount, remount, re-prime its tools"),
 ("concept", MC, ["_interpolate"], "interpolate ${ENV_VAR} environment variable references inside config string values"),
 ("concept", MC, ["effective_policy"], "compute the effective permission policy by merging global and per-server policies"),
 ("concept", MC, ["MockOAuthProvider"], "fake OAuth authorization server for tests that issues and refreshes tokens with PKCE"),
 ("concept", MC, ["TokenRewriteMiddleware"], "ASGI middleware that moves the session token from the URL path into a request header"),
 ("concept", MC, ["_http_request", ".Client"], "Lua MCP client that opens a fresh TCP connection per request and parses the chunked SSE HTTP response"),
 ("concept", MC, ["_start_sse"], "open the long-lived SSE stream that delivers server notifications to keep capabilities live"),
 ("concept", MC, [".dispatch"], "the single dispatch entry point that runs an in-process native neovim tool handler"),
 ("concept", MC, ["channel.bind", ".bind"], "register this neovim editor instance with the combiner and bind a chat token to it"),
 ("concept", MC, ["find_root"], "walk up parent directories to locate the .mcp-companion.json project config file"),
 ("concept", MC, ["resolve_allowed"], "resolve which MCP servers are allowed or disabled for a project and adapter"),
 ("concept", MC, ["_generate_token"], "generate a random per-chat session token for the combiner filter"),
 ("terse", MC, ["_merge_stale_server_tools"], "stale tool hysteresis"),
 ("terse", MC, ["_reconnect"], "backing process restart"),
 ("terse", MC, ["_apply_network_grace_window"], "offline grace window"),
 ("name", CS, ["BM25"], "BM25"),
 ("name", CS, ["reciprocal_rank_fusion"], "reciprocal rank fusion"),
 ("name", CS, ["LexicalCache"], "lexical cache"),
 ("name", CS, ["SymbolIndex"], "symbol index"),
 ("name", MC, ["SharedServerManager"], "SharedServerManager"),
 ("name", MC, ["NvimChannel"], "nvim channel"),
]
SEGMENTS = ["concept", "terse", "name"]
_STOP = {"the", "a", "an", "that", "to", "of", "for", "and", "or", "in", "on", "by", "its",
         "it", "is", "then", "so", "with", "into", "which", "given", "this", "them", "only",
         "before", "after", "up", "re", "back", "single", "one"}


def mm(xs: list[float]) -> list[float]:
    lo, hi = min(xs), max(xs)
    r = (hi - lo) or 1.0
    return [(x - lo) / r for x in xs]


def _istest(f: str) -> bool:
    return "/test" in f or f.rsplit("/", 1)[-1].startswith("test_")


class Feat:
    """Per-project feature substrate: dense embeddings, per-field BM25 (name/desc/sig),
    call-graph authority, is_test — all built once and reused across combiners."""

    def __init__(self, crib, proj: str):
        from crib.retrieve import BM25, _subtokens, tokenize
        self.tok = lambda s: tokenize(s or "") + _subtokens(s or "")
        rc = crib.query._resident(proj)
        self.ids, self.by, self.rc = rc.lk_ids, rc.by_fq, rc
        self.n = len(self.ids)
        E = [self.by[i] for i in self.ids]
        self.name_bm = rc.bm25                                   # existing: over name_terms
        self.desc_bm = BM25([self.tok(e.get("description", "")) for e in E])
        self.sig_bm = BM25([self.tok(e.get("signature", "")) for e in E])
        self.authority = [math.log1p(len(e.get("called_by") or [])) for e in E]
        self.is_test = [_istest(e.get("file", "")) for e in E]
        self.name_tokens = [set(self.tok(e.get("name", ""))) for e in E]
        self._dense = rc.dense(crib.embedder)
        self._crib = crib
        self.tokemb: dict[str, list[float]] = {}

    def build_token_emb(self):
        """Embed the vocabulary of name subtokens once (for ColBERT-lite MaxSim)."""
        vocab = sorted({t for ts in self.name_tokens for t in ts if len(t) > 1})
        vecs = self._crib.embedder.embed(vocab)
        self.tokemb = dict(zip(vocab, vecs))

    def q(self, query: str) -> dict:
        qv = self._crib.embedder.embed_query([query])[0]
        cos = [sum(a * b for a, b in zip(qv, v)) if v else -1.0 for v in self._dense]
        qt = self.tok(query)
        qinfo = {t for t in qt if t not in _STOP and len(t) > 1}
        bn, bd, bs = self.name_bm.scores(qt), self.desc_bm.scores(qt), self.sig_bm.scores(qt)
        cov = [(len(qinfo & self.name_tokens[i]) / max(len(qinfo), 1)) for i in range(self.n)]
        return {"cos": cos, "bn": bn, "bd": bd, "bs": bs, "cov": cov, "qinfo": qinfo}


def _cands(bm: list[float], k: int = 30) -> list[int]:
    nz = [i for i in range(len(bm)) if bm[i] > 0]
    nz.sort(key=lambda i: bm[i], reverse=True)
    return nz[:k]


def _norm_over(vals: list[float], cand: list[int]) -> dict[int, float]:
    """Min-max a signal over its candidate set (non-candidates -> 0)."""
    if not cand:
        return {}
    sub = [vals[i] for i in cand]
    lo, hi = min(sub), max(sub)
    r = (hi - lo) or 1.0
    return {i: (vals[i] - lo) / r for i in cand}


def order_from(scores: dict[int, float], k: int) -> list[int]:
    return sorted(scores, key=lambda i: scores[i], reverse=True)[:k]


# ---- combiners: (feat, qf, k) -> list[fqname] --------------------------------------

def c_current(crib, proj, q, feat, qf, k):
    """The SHIPPED code_lookup path end-to-end (coverage-gated blend, plus the
    range-matched rerank when `[retrieve].rerank` is on) — the production baseline
    the prototype combiners are compared against."""
    hits = crib.code_lookup(q, project=proj, k=k)
    return [h["fqname"] for h in hits[:k]]


def c_convex(alpha):
    def fn(crib, proj, q, feat, qf, k):
        cand = _cands(qf["bn"])
        d = mm(qf["cos"])
        s = _norm_over(qf["bn"], cand)
        sc = {i: alpha * d[i] + (1 - alpha) * s.get(i, 0.0) for i in range(feat.n)}
        return [feat.by[feat.ids[i]]["fqname"] for i in order_from(sc, k)]
    return fn


def c_covpc(a):
    """Per-candidate coverage: reweight each BM25 candidate's sparse signal by how much
    of the query ITS OWN name covers."""
    def fn(crib, proj, q, feat, qf, k):
        cand = _cands(qf["bn"])
        d = mm(qf["cos"])
        s = _norm_over(qf["bn"], cand)
        sc = {i: d[i] + a * qf["cov"][i] * s.get(i, 0.0) for i in range(feat.n)}
        return [feat.by[feat.ids[i]]["fqname"] for i in order_from(sc, k)]
    return fn


def c_bm25f(wn, wd, ws, wdense):
    """Field-weighted lexical (BM25F-style): fuse name/desc/sig BM25, then blend w/ dense."""
    def fn(crib, proj, q, feat, qf, k):
        f = [wn * qf["bn"][i] + wd * qf["bd"][i] + ws * qf["bs"][i] for i in range(feat.n)]
        cand = _cands(f)
        d = mm(qf["cos"])
        s = _norm_over(f, cand)
        sc = {i: d[i] + wdense * s.get(i, 0.0) for i in range(feat.n)}
        return [feat.by[feat.ids[i]]["fqname"] for i in order_from(sc, k)]
    return fn


def c_graph(base, g):
    """Stack a query-independent authority prior on a base fusion score."""
    def fn(crib, proj, q, feat, qf, k):
        cand = _cands(qf["bn"])
        d = mm(qf["cos"])
        s = _norm_over(qf["bn"], cand)
        auth = mm(feat.authority)
        sc = {i: d[i] + base * qf["cov"][i] * s.get(i, 0.0) + g * auth[i] for i in range(feat.n)}
        return [feat.by[feat.ids[i]]["fqname"] for i in order_from(sc, k)]
    return fn


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def c_colbert(a):
    """ColBERT-lite late interaction: soft MaxSim = mean over query tokens of the best
    cosine to any of the candidate's name tokens (so 'cache'≈'caching', unlike exact
    coverage). Scored over the dense∪BM25 candidate pool; blended with dense."""
    def fn(crib, proj, q, feat, qf, k):
        pool = set(order_from({i: qf["cos"][i] for i in range(feat.n)}, 20)) | set(_cands(qf["bn"], 20))
        qemb = crib.embedder.embed(list(qf["qinfo"])) if qf["qinfo"] else []
        d = mm(qf["cos"])
        sc = {i: d[i] for i in range(feat.n)}
        for i in pool:
            nemb = [feat.tokemb[t] for t in feat.name_tokens[i] if t in feat.tokemb]
            if nemb and qemb:
                ms = sum(max(_cos(qe, ne) for ne in nemb) for qe in qemb) / len(qemb)
                sc[i] = d[i] + a * ms
        return [feat.by[feat.ids[i]]["fqname"] for i in order_from(sc, k)]
    return fn


def with_rerank(base_fn, topn=20):
    """Cross-encoder precision stage: RRF-fuse the reranker's order over the base top-n."""
    def fn(crib, proj, q, feat, qf, k):
        from crib.retrieve import reciprocal_rank_fusion
        base = base_fn(crib, proj, q, feat, qf, max(k, topn))
        head = base[:topn]
        by = {feat.by[i]["fqname"]: feat.by[i] for i in feat.ids}
        docs = [f"{fq}: {by.get(fq, {}).get('description', '')}" for fq in head]
        scores = crib.reranker.scores(q, docs)
        ro = [head[j] for j in sorted(range(len(head)), key=lambda j: scores[j], reverse=True)]
        return reciprocal_rank_fusion([base, ro])[:k]
    return fn


def build(heavy: bool):
    combs = {
        "current(prod)": c_current,
        "convex·.7": c_convex(0.7),
        "covpc·a1": c_covpc(1.0),
        "covpc·a2": c_covpc(2.0),
        "bm25f n3d1s1": c_bm25f(3.0, 1.0, 1.0, 0.5),
        "bm25f n1d2s0": c_bm25f(1.0, 2.0, 0.0, 0.5),
        "graph covpc+.3": c_graph(2.0, 0.3),
        "covpc·a2+rerank": with_rerank(c_covpc(2.0)),
        "bm25f+rerank": with_rerank(c_bm25f(3.0, 1.0, 1.0, 0.5)),
    }
    if heavy:
        combs["colbert·a1"] = c_colbert(1.0)
        combs["colbert+rerank"] = with_rerank(c_colbert(1.0))
    return combs


# ---- Learning-to-rank: logistic over all features, leave-one-query-out ---------------

_LTR_FEATS = ("cos", "bn", "bd", "cov", "auth", "test")


def _ltr_rows(feat, qf, targets):
    """Feature rows for one query's candidate pool; label=1 iff candidate is the target."""
    pool = set(order_from({i: qf["cos"][i] for i in range(feat.n)}, 25)) | set(_cands(qf["bn"], 25))
    d, bn, bd = mm(qf["cos"]), mm(qf["bn"]), mm(qf["bd"])
    auth = mm(feat.authority)
    rows = []
    for i in pool:
        fq = feat.by[feat.ids[i]]["fqname"]
        x = [d[i], bn[i], bd[i], qf["cov"][i], auth[i], 1.0 if feat.is_test[i] else 0.0]
        rows.append((i, fq, x, 1 if any(t in fq for t in targets) else 0))
    return rows


def _logistic_fit(X, y, iters=300, lr=0.3, l2=1e-3):
    n, m = len(X), len(X[0])
    w = [0.0] * m
    b = 0.0
    for _ in range(iters):
        gw = [0.0] * m
        gb = 0.0
        for xi, yi in zip(X, y):
            z = b + sum(w[j] * xi[j] for j in range(m))
            p = 1.0 / (1.0 + math.exp(-max(-30, min(30, z))))
            e = p - yi
            for j in range(m):
                gw[j] += e * xi[j]
            gb += e
        for j in range(m):
            w[j] -= lr * (gw[j] / n + l2 * w[j])
        b -= lr * gb / n
    return w, b


def ltr_eval(feats, per_query):
    """Leave-one-query-out logistic LTR over all features. Returns ranks per segment."""
    data = [(seg, _ltr_rows(feats[proj], feats[proj].q(q), targets))
            for seg, proj, targets, q in GOLD]
    ranks = {s: [] for s in SEGMENTS}
    for held in range(len(data)):
        X, y = [], []
        for j, (_, rows) in enumerate(data):
            if j == held:
                continue
            for _i, _fq, x, lab in rows:
                X.append(x)
                y.append(lab)
        w, b = _logistic_fit(X, y)
        seg, rows = data[held]
        scored = sorted(rows, key=lambda r: b + sum(w[j] * r[2][j] for j in range(len(w))), reverse=True)
        rank = next((k for k, r in enumerate(scored, 1) if r[3] == 1), None)
        ranks[seg].append(rank)
    return ranks


def _match(f: str, t: str) -> bool:
    """Exact-leaf target match — NOT substring. Substring wrongly counts sibling
    tests/fakes (`fake_reconnect`, `test_reconnects_*` all contain `_reconnect`)."""
    leaf = f.replace(":", ".").split(".")[-1]
    tl = t.split(".")[-1]
    if "." in t:                       # qualified target → require the suffix too
        return f.endswith(t) and leaf == tl
    return leaf == tl or leaf.endswith("__" + t)   # `combiner__restart_server` ~ restart_server


def target_rank(fqs, targets):
    return next((i for i, f in enumerate(fqs, 1) if any(_match(f, t) for t in targets)), None)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--heavy", action="store_true")
    ap.add_argument("--per-query", default=None)
    ap.add_argument("--k", type=int, default=20)
    args = ap.parse_args(argv)

    from crib.app import Crib
    crib = Crib.open()
    combs = build(args.heavy)
    feats: dict[str, Feat] = {}
    ranks: dict[str, dict[str, list[int | None]]] = {name: {s: [] for s in SEGMENTS} for name in combs}
    try:
        for seg, proj, targets, q in GOLD:
            if proj not in feats:
                feats[proj] = Feat(crib, proj)
                if args.heavy:
                    feats[proj].build_token_emb()
            feat = feats[proj]
            qf = feat.q(q)
            for name, fn in combs.items():
                fqs = fn(crib, proj, q, feat, qf, args.k)
                ranks[name][seg].append(target_rank(fqs, targets))

        if args.per_query:
            name = next(n for n in combs if args.per_query.lower() in n.lower())
            print(f"per-query ranks for {name!r}:")
            idxs = {s: 0 for s in SEGMENTS}
            for seg, proj, targets, q in GOLD:
                r = ranks[name][seg][idxs[seg]]
                idxs[seg] += 1
                print(f"  {'  ' if (r and r<=3) else '✗ '}r{str(r or '—'):<3} [{seg:<7}] {q[:48]:<48} → {targets[0]}")
            return 0

        def r3(rs): return sum(1 for r in rs if r and r <= 3) / len(rs)
        def p1(rs): return sum(1 for r in rs if r == 1) / len(rs)
        def mrr(rs): return sum((1 / r) for r in rs if r) / len(rs)
        hdr = f"{'combiner':<18}" + "".join(f"{s[:7]+' R3':>11}" for s in SEGMENTS) + f"{'P@1':>7}{'MRR':>8}"
        print(hdr)
        print("-" * len(hdr))
        for name in combs:
            allr = [r for s in SEGMENTS for r in ranks[name][s]]
            cells = "".join(f"{r3(ranks[name][s]):>11.2f}" for s in SEGMENTS)
            print(f"{name:<18}{cells}{p1(allr):>7.2f}{mrr(allr):>8.3f}")
        if args.heavy:
            lr = ltr_eval(feats, args.per_query)
            allr = [r for s in SEGMENTS for r in lr[s]]
            cells = "".join(f"{r3(lr[s]):>11.2f}" for s in SEGMENTS)
            print(f"{'ltr (LOO)':<18}{cells}{p1(allr):>7.2f}{mrr(allr):>8.3f}")
        print("\nn: " + " ".join(f"{s}={sum(1 for g in GOLD if g[0]==s)}" for s in SEGMENTS))
    finally:
        crib.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
