#!/usr/bin/env python3
"""Derive the CLEAN, public-seeded eval corpus from notes_gold_large.json.

Two filters, both mechanical, both recorded per phrasing as provenance:

1. PUBLIC SOURCE. The clean corpus seeds from PUBLIC repos only: a need is
   included when its `expect` resolves through `.crib`'s `doc-sources.json` to a
   source-anchored doc in a public repo checkout (today: `sources/cribsheet/`
   → georgeharker/cribsheet). Authored notes live in the private `.crib` store;
   their text must not ride in a committed fixture.

2. HOLD-OUT (the contamination cut). Every gold phrasing is an enrichment TERM
   harvested from a label — `summary_index/asks` or `keyword_index/kw-tight`
   (recorded per query in `segments`). A phrasing whose source label is IN the
   retrieval config is a query drawn from the field being matched — a
   guaranteed hit, not a measurement. Today `asks` IS in `summary_labels`
   (enabled 2026-08-06), so all `asks`-segment phrasings are contaminated and
   dropped; `kw-tight` is held out → clean. If the config ever changes, this
   filter is re-derivable by re-running with a different drop-set — the
   provenance field records the source label per phrasing, so the cut is a
   parameter, not a rewrite.

    python scripts/derive_clean_corpus.py          # derive + write + report
    python scripts/derive_clean_corpus.py --dry-run

The enrichment TOMLs are exported separately (see --export-enrichments) so a
CI seed is: import the public repo → inject frozen TOMLs → eval, zero LLM.
"""

from __future__ import annotations

import argparse
import json
import os
import tomllib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LARGE = ROOT / "scripts" / "eval_data" / "notes_gold_large.json"
OUT = ROOT / "scripts" / "eval_data" / "notes_gold_clean.json"
ENRICH_DIR = ROOT / "scripts" / "eval_data" / "enrichments"

# PUBLIC SOURCES: prefix → repo. The clean corpus seeds from these ONLY.
# georgeharker/{cribsheet} today; extend the map as more public repos are
# imported into the store (the volume-corpus five: dotfiler, mcp-companion,
# svg-mcp, zdot — add their prefixes here when their needs join the gold set).
PUBLIC_SOURCES = {"sources/cribsheet/": "georgeharker/cribsheet"}

QUERY_LABEL = "asks"  # the segment whose phrasings are contaminated by config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--export-enrichments",
        action="store_true",
        help="also copy the keyword_index/keywords + summary_index/"
        "summary TOMLs for the target sections into "
        "scripts/eval_data/enrichments/ (frozen fixtures)",
    )
    args = ap.parse_args(argv)

    spec = json.loads(LARGE.read_text())
    home = os.path.expanduser("~")
    pdir = Path(home) / ".local" / "share" / "crib" / "projects" / "cribsheet"
    asks_dir = pdir / "summary_index" / "asks"

    # Contamination PROVENANCE: the exact asks alias text a phrasing was
    # harvested from, keyed (relpath, heading, normalized query) so the record
    # is checkable, not asserted.
    alias_text: dict[tuple[str, str, str], str] = {}
    for p in sorted(asks_dir.glob("*.toml")) if asks_dir.is_dir() else []:
        try:
            e = tomllib.loads(p.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        rel, head = e.get("relpath", ""), e.get("heading", "")
        for t in e.get("terms", []):
            t = " ".join(str(t).split()).lower()
            if t:
                alias_text[(rel.lower(), head.lower(), t)] = p.name

    clean_needs, dropped_q, kept_q = [], Counter(), Counter()
    for n in spec["needs"]:
        public = n["expect"].startswith(tuple(PUBLIC_SOURCES))
        queries, segments, prov = [], [], []
        for q, seg in zip(n["queries"], n["segments"]):
            if seg != QUERY_LABEL and public:
                queries.append(q)
                segments.append(seg)
                prov.append(
                    {
                        "query": q,
                        "segment": seg,
                        "contaminated": False,
                        "note": "query source (kw-tight) is held out of the "
                        "assessed retrieval config",
                    }
                )
            else:
                dropped_q[
                    ("public-asks" if public else "asks")
                    if seg == QUERY_LABEL
                    else "other"
                ] += 1
        if public and queries:
            clean_needs.append(
                {
                    "id": n["id"],
                    "expect": n["expect"],
                    **(
                        {"expect_heading": n["expect_heading"]}
                        if n.get("expect_heading")
                        else {}
                    ),
                    "queries": queries,
                    "segments": segments,
                    "provenance": prov,
                    "public_source": PUBLIC_SOURCES[
                        next(p for p in PUBLIC_SOURCES if n["expect"].startswith(p))
                    ],
                }
            )
            kept_q["clean"] += len(queries)
        elif not public:
            dropped_q["private-target"] += 1

    spec_out = {
        "_doc": (
            "CLEAN public-seeded notes gold subset, derived from "
            "notes_gold_large.json by scripts/derive_clean_corpus.py. Two cuts, "
            "recorded per phrasing: (1) PUBLIC SOURCE only — the expect target "
            "is source-anchored to a public repo checkout (seedable in CI from "
            "the pinned repo, zero private-store content); (2) HOLD-OUT — "
            "phrasings harvested from summary_index/asks are dropped because "
            "asks is IN the assessed retrieval config (summary_labels includes "
            "it since 2026-08-06): a query drawn from the very vector field "
            "being matched is a guaranteed hit, not a measurement. kw-tight is "
            "held out → clean. Regenerate deliberately, not per-run."
        ),
        "project": spec["project"],
        "query_sources": spec["query_sources"],
        "public_sources": PUBLIC_SOURCES,
        "needs": sorted(clean_needs, key=lambda n: n["id"]),
    }
    n_q = sum(len(n["queries"]) for n in clean_needs)
    print(
        f"clean corpus: {len(clean_needs)} needs / {n_q} phrasings "
        f"(kept from {len(spec['needs'])} needs; dropped {dict(dropped_q)})"
    )

    if args.dry_run:
        return 0
    OUT.write_text(json.dumps(spec_out, indent=1))
    print(f"wrote {OUT}")

    if args.export_enrichments:
        # The TOMLs the eval's retrieval config consumes for the target
        # sections: keyword_index/keywords (BM25) + summary_index/summary
        # (dense aliases). kw-tight/asks are HELD OUT — never exported as
        # retrieval fixtures (they are the query sources).
        exported = 0
        wanted_hashes: set[str] = set()
        for p in sorted((pdir / "keyword_index" / "keywords").glob("*.toml")):
            try:
                e = tomllib.loads(p.read_text())
            except (OSError, tomllib.TOMLDecodeError):
                continue
            if e.get("relpath", "").startswith(tuple(PUBLIC_SOURCES)):
                wanted_hashes.add(e.get("section_hash") or p.stem)
        for label_root, label in (
            (pdir / "keyword_index" / "keywords", "keywords"),
            (pdir / "summary_index" / "summary", "summary"),
        ):
            dest = ENRICH_DIR / label
            dest.mkdir(parents=True, exist_ok=True)
            for p in sorted(label_root.glob("*.toml")):
                if p.stem in wanted_hashes:
                    (dest / p.name).write_text(p.read_text())
                    exported += 1
        print(f"exported {exported} enrichment TOMLs → {ENRICH_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
