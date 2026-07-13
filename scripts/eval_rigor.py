#!/usr/bin/env python3
"""Rigorous eval of the settled design on a LARGER, vocabulary-shifted gold set.

Design under test: coverage (covpc) over the UNIQ'd combined keyword field (name-split +
synth kws) gates a BM25 front-end; blended dense-dominant with dense; rerank top-N.

Answers three questions:
  1. Is the blend justified? — gated-BM25 alone vs dense alone vs blend vs an oracle router
     (per-query pick the better source) vs a coverage router (cov high → bm, else dense).
  2. Does uniq'ing the field tokens matter? (--no-uniq to compare)
  3. Do the conclusions hold at larger n? (loads eval_data/gold_large.json + original GOLD)

    python scripts/eval_rigor.py
    python scripts/eval_rigor.py --no-uniq
    python scripts/eval_rigor.py --rerank      # add the (slow) rerank rows
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
KWS = SC / "kws.json"
GLARGE = SC / "gold_large.json"


def mm(d):
    if not d:
        return {}
    lo, hi = min(d.values()), max(d.values())
    r = (hi - lo) or 1.0
    return {i: (v - lo) / r for i, v in d.items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-uniq", action="store_true")
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--w", type=float, default=1.5)
    args = ap.parse_args(argv)
    uniq = not args.no_uniq

    from crib.app import Crib
    from crib.retrieve import BM25, tokenize, _subtokens, reciprocal_rank_fusion
    from eval_fusion import GOLD, Feat, _STOP, order_from

    kws = json.loads(KWS.read_text())
    crib = Crib.open()
    projset = {g[1] for g in GOLD}
    feats = {p: Feat(crib, p) for p in projset}

    # combined keyword field per symbol = name-split + synth kw phrases; optionally uniq'd
    idx = {}
    for p, f in feats.items():
        corpus, fsets = [], []
        for i in f.ids:
            fq = f"{p}::{f.by[i]['fqname']}"
            txt = f.by[i].get("name", "") + " " + " ".join(kws.get(fq, []))
            toks = tokenize(txt) + _subtokens(txt)
            if uniq:
                toks = list(dict.fromkeys(toks))
            corpus.append(toks)
            fsets.append(set(toks))
        idx[p] = (BM25(corpus), fsets)

    # gold: larger vocab-shifted set (exact fqname targets) + original concept queries
    gold = []
    if GLARGE.exists():
        for fq, qs in json.loads(GLARGE.read_text()).items():
            p, _, rel = fq.partition("::")
            if p in feats:
                for q in qs:
                    gold.append((p, rel, q))
    orig = [(p, None, q, t) for seg, p, t, q in GOLD if seg == "concept"]
    n_large = len(gold)

    def signals(p, q):
        f = feats[p]
        qf = f.q(q)
        bm, fsets = idx[p]
        qt = tokenize(q) + _subtokens(q)
        qi = {t for t in set(qt) if t not in _STOP and len(t) > 1}
        n = max(len(qi), 1)
        bmsc = bm.scores(qt)
        cov = [len(qi & fsets[i]) / n for i in range(f.n)]
        gated = {i: cov[i] * bmsc[i] for i in range(f.n) if cov[i] * bmsc[i] > 0}
        dense = {i: qf["cos"][i] for i in range(f.n)}
        return f, dense, gated

    def rank_exact(f, order, rel, tset):
        for r, i in enumerate(order, 1):
            fq = f.by[f.ids[i]]["fqname"]
            if (rel is not None and fq == rel) or (tset and any(_leaf(fq) == _leaf(t) or fq.endswith(t) for t in tset)):
                return r
        return None

    def _leaf(x):
        return x.replace(":", ".").split(".")[-1]

    # accumulate ranks per strategy
    strat: dict[str, list] = {k: [] for k in ["dense", "gatedBM", "blend", "oracle", "covRouter"]}
    rer_ranks = []
    items = [(p, rel, q, None) for (p, rel, q) in gold] + orig
    for p, rel, q, tset in items:
        f, dense, gated = signals(p, q)
        d_order = order_from(dense, 60)
        g_order = sorted(gated, key=gated.get, reverse=True)[:60]
        blend = {i: mm(dense).get(i, 0) + args.w * mm(gated).get(i, 0) for i in range(f.n)}
        b_order = order_from(blend, 60)
        rd = rank_exact(f, d_order, rel, tset)
        rg = rank_exact(f, g_order, rel, tset)
        rb = rank_exact(f, b_order, rel, tset)
        strat["dense"].append(rd)
        strat["gatedBM"].append(rg)
        strat["blend"].append(rb)
        strat["oracle"].append(min([r for r in (rd, rg) if r], default=None))
        # coverage router: if the target-region has strong coverage, trust BM else dense
        top_cov = max((gated.get(i, 0) for i in g_order[:1]), default=0)
        strat["covRouter"].append(rg if top_cov > 0 and rg and rg <= (rd or 999) else rd)
        if args.rerank:
            base = [f.by[f.ids[i]]["fqname"] for i in b_order[:20]]
            by = {f.by[i]["fqname"]: f.by[i] for i in f.ids}
            rs = crib.reranker.scores(q, [f"{x}: {by[x].get('description','')}" for x in base])
            ro = [base[j] for j in sorted(range(len(base)), key=lambda j: rs[j], reverse=True)]
            fused = reciprocal_rank_fusion([base, ro])
            rr = None
            for r, fq in enumerate(fused, 1):
                if (rel is not None and fq == rel) or (tset and any(_leaf(fq) == _leaf(t) for t in tset)):
                    rr = r
                    break
            rer_ranks.append(rr)

    def h3(rs): return sum(1 for r in rs if r and r <= 3) / len(rs)
    def p1(rs): return sum(1 for r in rs if r == 1) / len(rs)
    def mrr(rs): return sum((1 / r) for r in rs if r) / len(rs)

    print(f"n = {len(items)} queries ({n_large} vocab-shifted + {len(orig)} original concept), "
          f"uniq={'on' if uniq else 'OFF'}, blend w={args.w}")
    print(f"{'strategy':<12}{'P@1':>7}{'R@3':>7}{'MRR':>8}")
    print("-" * 34)
    for k in ["dense", "gatedBM", "covRouter", "oracle", "blend"]:
        rs = strat[k]
        print(f"{k:<12}{p1(rs):>7.2f}{h3(rs):>7.2f}{mrr(rs):>8.3f}")
    if args.rerank:
        print(f"{'blend+rr':<12}{p1(rer_ranks):>7.2f}{h3(rer_ranks):>7.2f}{mrr(rer_ranks):>8.3f}")

    # blend justification: how often does blend beat the better single source?
    better = sum(1 for a, b in zip(strat["blend"], strat["oracle"]) if a and (not b or a < b))
    equal = sum(1 for a, b in zip(strat["blend"], strat["oracle"]) if a == b)
    worse = sum(1 for a, b in zip(strat["blend"], strat["oracle"]) if a and b and a > b)
    print(f"\nblend vs oracle-router: better={better}  equal={equal}  worse={worse}  (of {len(items)})")
    crib.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
