#!/usr/bin/env python3
"""FROZEN multi-repo hybrid-retrieval harness — one code path, one explicit spec.

SIGNALS (per candidate symbol i, per query q):
  Q          = informative query tokens = {t in tokenize(q)+subtokens(q) : not stopword, len>1}
  Field(i)   = tokens of (symbol name + its synth keyword phrases)     # expanded lexical field
  cov(i)     = |Q ∩ set(Field(i))| / |Q|                               # ∈ [0,1]
  BM25(i)    = BM25(query tokens ; Field corpus)[i]                     # ∈ [0,~40], UNbounded
  gated(i)   = cov(i) · BM25(i)                                         # coverage-gated sparse
  dense(i)   = cos(q_emb, doc_emb[i])                                  # ∈ [0.6,0.93], RAW (calibrated)

NORMALIZATION: min-max the BM25/gated side ONLY, over the union pool P. Dense left RAW.
  P          = top-50 dense ∪ top-50 gated(>0)
  g~(i)      = minmax over P of gated
  S(i)       = alpha·dense(i) + beta·g~(i)

RERANK (range-matched, per user): cross-encoder over blend top-N, TWO ways:
  rrf   : RRF(blend order, rerank order)                              # crib's current style
  blend : S'(i) = minmax_N(S) + gamma·minmax_N(rerank_score)          # range-matched 3rd term

Test set = frozen union of vocab-shifted LLM queries (exact-fqname targets) across repos +
the original 30 concept queries. Reports per-repo and combined MRR/R@3/P@1.

    python scripts/eval_hybrid.py
    python scripts/eval_hybrid.py --rerank
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vendor" / "llmkit" / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
SC = ROOT / "scripts" / "eval_data"
K = 50
RERANK_N = 20


def _leaf(x):
    return x.replace(":", ".").split(".")[-1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alpha", type=float, default=1.0)
    ap.add_argument("--beta", type=float, default=1.0)
    ap.add_argument("--gamma", type=float, default=1.0)
    ap.add_argument("--rerank", action="store_true")
    args = ap.parse_args(argv)

    from crib.app import Crib
    from crib.retrieve import BM25, tokenize, _subtokens, reciprocal_rank_fusion
    from eval_fusion import GOLD, Feat, _STOP

    # merge all keyword files (keys are "proj::fqname")
    kws: dict[str, list] = {}
    for kf in [SC / "kws.json"] + sorted(SC.glob("kws_*.json")):
        if kf.exists():
            kws.update(json.loads(kf.read_text()))

    # build the frozen item list: (proj, match_fn, query, repo_label)
    items = []
    # vocab-shifted sets: gold_large (mcp-companion+cribsheet) + per-repo queries_*.json
    def add_exact(fq_to_qs, label):
        for fq, qs in fq_to_qs.items():
            p, _, rel = fq.partition("::")
            for q in qs:
                items.append((p, (lambda rel: (lambda h: h == rel))(rel), q, label))
    if (SC / "gold_large.json").exists():
        add_exact(json.loads((SC / "gold_large.json").read_text()), "crib-family")
    for qf in sorted(SC.glob("queries_*.json")):
        label = qf.stem.replace("queries_", "")
        add_exact(json.loads(qf.read_text()), label)
    for seg, p, t, q in GOLD:
        if seg == "concept":
            ts = t if isinstance(t, list) else [t]
            items.append((p, (lambda ts: (lambda h: any(_leaf(h) == _leaf(x) or h.endswith(x) for x in ts)))(ts), q, "orig-concept"))

    projects = sorted({p for p, _, _, _ in items})
    from crib.app import Crib as _C  # noqa
    crib = Crib.open()
    feats = {p: Feat(crib, p) for p in projects}
    idx = {}
    for p, f in feats.items():
        corpus, tsets = [], []
        for i in f.ids:
            txt = f.by[i].get("name", "") + " " + " ".join(kws.get(f"{p}::{f.by[i]['fqname']}", []))
            toks = tokenize(txt) + _subtokens(txt)
            corpus.append(toks)
            tsets.append(set(toks))
        idx[p] = (BM25(corpus), tsets)

    def signals(p, q):
        f = feats[p]
        qf = f.q(q)
        bm, tsets = idx[p]
        qt = tokenize(q) + _subtokens(q)
        Q = {t for t in set(qt) if t not in _STOP and len(t) > 1}
        n = max(len(Q), 1)
        bmsc = bm.scores(qt)
        dense = {i: qf["cos"][i] for i in range(f.n)}
        gated = {i: (len(Q & tsets[i]) / n) * bmsc[i] for i in range(f.n) if (len(Q & tsets[i]) / n) * bmsc[i] > 0}
        return f, dense, gated

    def minmax_dict(d, keys):
        vals = [d.get(i, 0.0) for i in keys]
        lo, hi = (min(vals), max(vals)) if vals else (0.0, 1.0)
        rng = (hi - lo) or 1.0
        return {i: (d.get(i, 0.0) - lo) / rng for i in keys}

    def rank_of(f, order, match):
        return next((r for r, i in enumerate(order, 1) if match(f.by[f.ids[i]]["fqname"])), None)

    # accumulate per (repo, strategy)
    per: dict[tuple, list] = {}  # (repo, strat) -> list of ranks

    def rec(repo, strat, r):
        per.setdefault((repo, strat), []).append(r)
        per.setdefault(("ALL", strat), []).append(r)

    for p, match, q, repo in items:
        f, dense, gated = signals(p, q)
        d_order = sorted(dense, key=lambda i: dense[i], reverse=True)[:60]
        g_order = sorted(gated, key=lambda i: gated[i], reverse=True)[:60]
        rec(repo, "dense", rank_of(f, d_order, match))
        rec(repo, "gatedBM", rank_of(f, g_order, match))
        pool = list(dict.fromkeys(d_order[:K] + g_order[:K]))
        gn = minmax_dict(gated, pool)
        S = {i: args.alpha * dense[i] + args.beta * gn[i] for i in pool}
        blend_order = sorted(pool, key=lambda i: S[i], reverse=True)
        rec(repo, "blend", rank_of(f, blend_order, match))
        if args.rerank:
            head = blend_order[:RERANK_N]
            docs = [f"{f.by[f.ids[i]]['fqname']}: {f.by[f.ids[i]].get('description','')}" for i in head]
            rrs = crib.reranker.scores(q, docs)
            rr_order = [head[j] for j in sorted(range(len(head)), key=lambda j: rrs[j], reverse=True)]
            # (a) RRF of orders
            fused = reciprocal_rank_fusion([[str(i) for i in blend_order], [str(i) for i in rr_order]])
            rec(repo, "blend+rr(rrf)", next((r for r, x in enumerate(fused, 1) if match(f.by[f.ids[int(x)]]["fqname"])), None))
            # (b) range-matched 3rd term over the head
            Sn = minmax_dict(S, head)
            rn = {head[j]: rrs[j] for j in range(len(head))}
            rnn = minmax_dict(rn, head)
            S2 = {i: Sn[i] + args.gamma * rnn[i] for i in head}
            rec(repo, "blend+rr(blend)", rank_of(f, sorted(head, key=lambda i: S2[i], reverse=True), match))

    def P1(rs): return sum(1 for r in rs if r == 1) / len(rs)
    def R3(rs): return sum(1 for r in rs if r and r <= 3) / len(rs)
    def MRR(rs): return sum((1 / r) for r in rs if r) / len(rs)

    strmap = ["dense", "gatedBM", "blend"] + (["blend+rr(rrf)", "blend+rr(blend)"] if args.rerank else [])
    repos = [r for r in sorted({rp for rp, _ in per}) if r != "ALL"] + ["ALL"]
    print(f"FROZEN multi-repo · pool K={K} · dense RAW, gated min-max over union · a={args.alpha} b={args.beta} g={args.gamma}")
    counts = {r: len(per.get((r, "dense"), [])) for r in repos}
    print("repos: " + ", ".join(f"{r}={counts[r]}" for r in repos))
    print(f"\n{'repo':<14}{'strategy':<16}{'P@1':>7}{'R@3':>7}{'MRR':>8}")
    print("-" * 52)
    for r in repos:
        for s in strmap:
            rs = per.get((r, s))
            if rs:
                print(f"{r:<14}{s:<16}{P1(rs):>7.2f}{R3(rs):>7.2f}{MRR(rs):>8.3f}")
        print()
    crib.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
