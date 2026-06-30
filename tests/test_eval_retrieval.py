"""Retrieval-quality regression gate — runs scripts/eval_retrieval.py.

Integration-flavoured: it needs the `crib` CLI on PATH and the `cribsheet` project
seeded (`crib import` via the repo's .crib). When that environment isn't present the
harness exits 2 and this test skips rather than failing — so a plain unit-test run on
a machine without a warm daemon stays green. When the store *is* seeded, an unmet
quality bar (exit 1) fails here, turning the §5.1 proof into a standing check.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "eval_retrieval.py"


def test_retrieval_quality_bars():
    # By default the gate hits the warm daemon (sub-second/query — important on a
    # Pi, where a cold model load per call makes --no-daemon take minutes). The
    # discipline: restart the daemon after changing retrieval code so it serves the
    # current code. For a restart-independent, repo-code run set CRIB_EVAL_NO_DAEMON=1
    # (slow: cold embedder load per query).
    cmd = [sys.executable, str(SCRIPT)]
    if os.environ.get("CRIB_EVAL_NO_DAEMON"):
        cmd.append("--no-daemon")
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    out = proc.stdout + proc.stderr
    if proc.returncode == 2:
        import pytest

        pytest.skip(f"eval environment not ready (crib/seeded project absent):\n{out}")
    assert proc.returncode == 0, f"retrieval quality bars unmet:\n{out}"
