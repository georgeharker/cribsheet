"""Opt-in corpus golden gate.

Re-indexes the committed, URL-pinned real-project goldens (tests/goldens/) from their
source repos at the frozen SHA and confirms STRUCTURAL identity — the heavy, real-world
regression net for the code-index pipeline + store + query.

It CLONES the source repos from GitHub at the pinned SHA and re-indexes them (needs
network + an installed LSP + a couple minutes), so it is skipped by default. Run it
explicitly:

    CRIB_CORPUS_GOLDENS=1 pytest tests/test_corpus_goldens.py

or drive the harness directly for one project:

    python scripts/snapshot_harness.py compare tests/goldens/mcp-companion

The fast, always-on structural gates are the unit suite + tests/test_notestore_snapshot
(notes) + tests/test_codeindex (extraction) — this is the deliberate deep check.
`compare` exits 0 on STRUCTURAL identity (LSP cross-file edge wobble is tolerated as
noise) and 1 on real structural drift.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_GOLDENS = _ROOT / "tests" / "goldens"

pytestmark = pytest.mark.skipif(
    not os.environ.get("CRIB_CORPUS_GOLDENS"),
    reason="opt-in: clones source repos from GitHub + reindexes; set CRIB_CORPUS_GOLDENS=1")


@pytest.mark.parametrize("project", ["cribsheet", "mcp-companion"])
def test_corpus_golden_structurally_identical(project):
    golden = _GOLDENS / project
    assert (golden / "meta").exists(), f"no committed golden for {project}"
    r = subprocess.run(
        ["python", str(_ROOT / "scripts" / "snapshot_harness.py"), "compare", str(golden)],
        capture_output=True, text=True, cwd=str(_ROOT))
    assert r.returncode == 0, (
        f"{project} STRUCTURALLY drifted from its golden:\n{r.stdout[-2000:]}")
