#!/usr/bin/env python3
"""Concept→symbol eval for code_lookup — MRR / recall@k, sweeping the RRF sparse
weight, segmented by query kind (concept vs name).

In-process (one Crib, warm embedder). The projects must already be code-indexed.
The sweep shows whether a given `sparse_weight` trades concept accuracy (dense) for
name accuracy (sparse) — pick the knee, don't eyeball one query.

    python scripts/eval_code.py
    python scripts/eval_code.py --k 5 --weights 0,0.2,0.4,0.6,1.0
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vendor" / "llmkit" / "src"))
CASES = Path(__file__).resolve().parent / "eval_code.cases.json"


def _hit_rank(hits: list[dict], expect) -> int | None:
    exps = expect if isinstance(expect, list) else [expect]
    for i, h in enumerate(hits, 1):
        fq = h.get("fqname", "")
        if any(e in fq for e in exps):
            return i
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", type=Path, default=CASES)
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--recall-k", type=int, default=3, dest="recall_k")
    ap.add_argument("--weights", default="0,0.2,0.4,0.6,1.0",
                    help="comma-separated RRF sparse weights to sweep")
    args = ap.parse_args(argv)

    from crib.app import Crib
    cases = json.loads(args.cases.read_text())["cases"]
    weights = [float(w) for w in args.weights.split(",")]
    crib = Crib.open()
    try:
        # guard: are the referenced projects actually indexed?
        from crib.codeindex import SymbolIndex
        for proj in {c["project"] for c in cases}:
            if not SymbolIndex(crib.paths.project_dir(proj)).all():
                print(f"error: project {proj!r} has no symbol_index — run "
                      f"`crib code-index <file> -p {proj}` first", file=sys.stderr)
                return 2

        print(f"{'sparse_w':>9}{'MRR':>8}{'recall@'+str(args.recall_k):>11}"
              f"{'concept-r':>11}{'name-r':>9}   (n={len(cases)})")
        print("-" * 60)
        rows_by_w = {}
        for w in weights:
            rows = []
            for c in cases:
                hits = crib.code_lookup(c["q"], project=c["project"], k=args.k,
                                        sparse_weight=w)
                rank = _hit_rank(hits, c["expect"])
                rows.append({**c, "rank": rank,
                             "rr": (1.0 / rank) if rank else 0.0,
                             "hit": bool(rank and rank <= args.recall_k)})
            rows_by_w[w] = rows
            mrr = sum(r["rr"] for r in rows) / len(rows)
            rec = sum(r["hit"] for r in rows) / len(rows)
            con = [r for r in rows if r["kind"] == "concept"]
            nam = [r for r in rows if r["kind"] == "name"]
            cr = sum(r["hit"] for r in con) / max(len(con), 1)
            nr = sum(r["hit"] for r in nam) / max(len(nam), 1)
            print(f"{w:>9.2f}{mrr:>8.3f}{rec:>11.3f}{cr:>11.3f}{nr:>9.3f}")
        print("-" * 60)

        # per-case detail at the tuned default (0.2 if present, else the middle)
        dflt = 0.2 if 0.2 in rows_by_w else weights[len(weights) // 2]
        print(f"per-case @ sparse_w={dflt} (✗ = missed top-{args.recall_k}):")
        for r in rows_by_w[dflt]:
            mark = " " if r["hit"] else "✗"
            print(f"  {mark} r{str(r['rank'] or '—'):<3} [{r['kind']:<7}] "
                  f"{r['q'][:44]:<44} → {r['expect']}")
    finally:
        crib.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
