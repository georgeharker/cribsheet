#!/usr/bin/env python3
"""Build a LARGE notes-retrieval gold set from the enrichment indexes as they exist.

Each SectionIndex entry (keyword_index/<label>/, summary_index/<label>/) records its
section's relpath + heading — so its terms are READY-MADE queries with known targets.
This freezes them into an `eval_retrieval.py --cases`-compatible needs file, giving
the notes eval the resolution the 31-phrasing hand set lacks (differences under
±0.03 MRR are noise there).

HOLD-OUT RULE (circularity guard): a label used as a QUERY SOURCE here must never be
used by the retrieval config under assessment — a query drawn from the very vector /
BM25 field being matched is a guaranteed hit, not a measurement. Defaults:
  queries   ← summary_index/asks (user-phrased questions) + keyword_index/kw-tight
              (terse distinctive phrases)
  retrieval → may use keyword_index/keywords and summary_index/summary ONLY.

Ambiguous phrases (same query text emitted for >1 section) are dropped — they have
no single right answer. (Stale entries are the prune GC's problem, not this script's:
a project-wide elaborate/summarize pass keeps the stores live-only.)

    python scripts/gen_notes_gold.py                       # cribsheet, default labels
    python scripts/gen_notes_gold.py -p PROJ -o out.json \
        --queries summary_index/asks keyword_index/kw-tight
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vendor" / "llmkit" / "src"))

MIN_QUERY_CHARS = 8          # drop fragments too short to be a plausible query


def load_entries(project_dir: Path, source: str) -> list[dict]:
    """All TOML entries under <project_dir>/<root_name>/<label>/ for a
    'root_name/label' source spec."""
    root_name, _, label = source.partition("/")
    d = project_dir / root_name / label
    out = []
    for p in sorted(d.glob("*.toml")) if d.is_dir() else []:
        try:
            out.append(tomllib.loads(p.read_text()))
        except (OSError, tomllib.TOMLDecodeError):
            continue
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--project", default="cribsheet")
    ap.add_argument("-o", "--out",
                    default=str(ROOT / "scripts" / "eval_data" / "notes_gold_large.json"))
    ap.add_argument("--queries", nargs="+",
                    default=["summary_index/asks", "keyword_index/kw-tight"],
                    help="root_name/label sources to harvest queries from "
                         "(HOLD these labels out of any assessed retrieval config)")
    args = ap.parse_args(argv)

    from crib.paths import Paths
    pdir = Paths.resolve().project_dir(args.project)

    # query text -> {(relpath, heading)}: build once across ALL sources so a phrase
    # that names two sections is dropped everywhere (no single right answer)
    owners: dict[str, set[tuple[str, str]]] = defaultdict(set)
    per_source: dict[str, list[tuple[str, str, str]]] = {}   # source -> [(q, rel, head)]
    for source in args.queries:
        rows = []
        for e in load_entries(pdir, source):
            rel, head = e.get("relpath", ""), e.get("heading", "")
            if not rel:
                continue    # liveness is the prune GC's job — entries here are current
            for t in e.get("terms", []):
                t = " ".join(str(t).split())
                if len(t) >= MIN_QUERY_CHARS:
                    owners[t.lower()].add((rel, head))
                    rows.append((t, rel, head))
        per_source[source] = rows

    needs: dict[tuple[str, str], dict] = {}
    kept = dropped = 0
    for source, rows in per_source.items():
        seg = source.rsplit("/", 1)[-1]
        for q, rel, head in rows:
            if len(owners[q.lower()]) != 1:
                dropped += 1                   # ambiguous across sections
                continue
            kept += 1
            need = needs.setdefault((rel, head), {
                "id": f"{rel}#{head}" if head else rel,
                "expect": rel,
                **({"expect_heading": head} if head else {}),
                "queries": [], "segments": []})
            need["queries"].append(q)
            need["segments"].append(seg)

    spec = {"_doc": (f"LARGE notes gold set for {args.project}, harvested from "
                     f"{args.queries} (see scripts/gen_notes_gold.py — hold these "
                     "labels OUT of assessed retrieval configs). Frozen; regenerate "
                     "deliberately, not per-run."),
            "project": args.project,
            "query_sources": args.queries,
            "needs": sorted(needs.values(), key=lambda n: n["id"])}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, indent=1))
    n_q = sum(len(n["queries"]) for n in needs.values())
    print(f"wrote {out}: {len(needs)} needs, {n_q} queries "
          f"(kept {kept}, dropped {dropped} ambiguous/short)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
