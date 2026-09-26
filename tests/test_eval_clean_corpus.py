"""The CLEAN public-seeded eval corpus, as a committed fixture (plan item:
build-the-clean-public-eval-corpus-as-a-committed-fixture).

The claim this test pins: the clean corpus (scripts/eval_data/notes_gold_clean.json
— public-repo targets only, ask-contaminated phrasings dropped) can be SEEDED
FROM THE FIXTURES ALONE — checkout the public repo, `project_setup` to import its
docs in-situ, inject the frozen enrichment TOMLs — and the eval then runs with
ZERO LLM calls and zero private-store content, meeting its measured floors.

The floors come from the first seeded run (recorded below, per the standing
re-tune rule: re-measure + record, never silently). The enrichment TOMLs are
frozen at the working tree they were exported from: if the indexed docs change,
section hashes drift and this test fails loudly — regenerate the fixtures with
`scripts/derive_clean_corpus.py --export-enrichments`, deliberately."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "scripts" / "eval_data" / "notes_gold_clean.json"
ENRICH = ROOT / "scripts" / "eval_data" / "enrichments"

# Floors: ~2 SE under the SEEDED BASELINE, measured 2026-09-25:
#     MRR 0.8347   recall@3 0.9091   (n=638, hash-embedder fixture)
# SE ≈ 0.015 (MRR) / 0.011 (recall); floors sit ~2 SE below — far enough not to
# trip on sampling, close enough to catch a real fixture/retrieval regression.
# Fixture-relative numbers (the hash embedder is self-consistent across runs;
# NOT comparable to the daemon's bge numbers). Re-measure + record, never
# silently re-tune.
FLOOR_MRR = 0.80
FLOOR_RECALL = 0.88


@pytest.fixture()
def seeded_crib(tmp_path, monkeypatch):
    """A crib whose 'cribsheet' project is seeded from THIS repo checkout (the
    public seed) plus the frozen enrichment TOMLs — no LLM anywhere."""
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())

    import asyncio

    run = asyncio.run
    # DOCS-ONLY seeding, deliberately: project_setup would set want_code=True,
    # and the code pipeline is extract → describe → PERSIST — describe hands
    # every symbol to the LLM bridge. The eval needs the repo's DOCS in-situ
    # and nothing else; embedding is local (hash), so this is LLM-free.
    link, _ = crib._ensure_crib(cwd=ROOT, project=None, want_code=False, want_docs=True)
    run(crib.index_docs_insitu(link.project, link.root))

    # Inject the frozen enrichments: the TOMLs are content-addressed by section
    # hash, and the import above chunked the SAME working tree the export was
    # frozen against — the hashes line up, or the eval below says so loudly.
    proj_dir = Paths.resolve().project_dir("cribsheet")
    for label in ("keywords", "summary"):
        src = ENRICH / label
        if src.is_dir():
            dest = (
                proj_dir
                / ("keyword_index" if label == "keywords" else "summary_index")
                / label
            )
            dest.mkdir(parents=True, exist_ok=True)
            for f in src.glob("*.toml"):
                shutil.copy(f, dest / f.name)
    return crib


def test_clean_corpus_seeds_from_public_fixtures_and_meets_floors(seeded_crib):
    import json

    spec = json.loads(CASES.read_text())
    # crib.lookup is SYNC — plain calls; only the fixture's doc indexing is async.
    mrr_num = rec_num = n = 0
    misses = []
    for need in spec["needs"]:
        for q in need["queries"]:
            hits = seeded_crib.lookup(q, project="cribsheet", k=8, store="notes")
            eh = (need.get("expect_heading") or "").lower()
            rank = next(
                (
                    i
                    for i, h in enumerate(hits, 1)
                    if h.relpath == need["expect"]
                    and (not eh or eh in h.heading.lower())
                ),
                None,
            )
            mrr_num += (1.0 / rank) if rank else 0.0
            rec_num += bool(rank and rank <= 3)
            n += 1
            if not rank:
                misses.append((need["id"][:40], q[:40]))
    mrr, recall = mrr_num / n, rec_num / n

    # Floors: measured on the seeded fixture (2026-09-25). A drop below means
    # the fixture seeding or retrieval regressed — diagnose before re-tuning.
    assert mrr >= FLOOR_MRR, (mrr, misses[:5])
    assert recall >= FLOOR_RECALL, (recall, misses[:5])
