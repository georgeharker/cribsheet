"""Retrieval-quality gates — run scripts/eval_retrieval.py.

Integration-flavoured: they need the `crib` CLI on PATH and the `cribsheet` project
seeded (`crib import` via the repo's .crib). When that environment isn't present the
harness exits 2 and these skip rather than failing — so a plain unit-test run on a
machine without a warm daemon stays green.

TWO SETS, TWO JOBS. The always-on one runs the 31-phrasing hand set against SMOKE
floors: it catches an empty store or a misconfigured project, and nothing finer,
because one query crossing rank 3 is 3.2% of recall at n=31. The real quality gate is
the n=1876 gold set, which is opt-in (`CRIB_EVAL_LARGE=1`) because it takes ~13
minutes on a warm daemon. See the bar rationale in scripts/eval_retrieval.py.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "eval_retrieval.py"
LARGE_CASES = ROOT / "scripts" / "eval_data" / "notes_gold_large.json"

# Floors for the large set, ~2 SE below the 2026-08-15 baseline (MRR 0.711 /
# recall@3 0.779; SE ~0.010 at n=1876). Far enough below not to trip on sampling,
# close enough to catch a real loss — dropping `asks` costs -0.0795/-0.0826, about
# 8 SE, which cannot hide under these.
LARGE_BAR_MRR = "0.69"
LARGE_BAR_RECALL = "0.75"


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    return proc.returncode, proc.stdout + proc.stderr


def test_retrieval_smoke_bars():
    """Hand set, smoke floors — gross breakage only."""
    # By default the gate hits the warm daemon (fast — important on a Pi, where a cold
    # model load per call makes --no-daemon take minutes). The discipline: restart the
    # daemon after changing retrieval code so it serves the current code. For a
    # restart-independent, repo-code run set CRIB_EVAL_NO_DAEMON=1.
    cmd = [sys.executable, str(SCRIPT)]
    if os.environ.get("CRIB_EVAL_NO_DAEMON"):
        cmd.append("--no-daemon")
    rc, out = _run(cmd)
    if rc == 2:
        pytest.skip(f"eval environment not ready (crib/seeded project absent):\n{out}")
    assert rc == 0, f"retrieval smoke bars unmet:\n{out}"


@pytest.mark.skipif(
    not os.environ.get("CRIB_EVAL_LARGE"),
    reason="opt-in: n=1876 gold set, ~13 min on a warm daemon; set CRIB_EVAL_LARGE=1")
def test_retrieval_quality_bars_large():
    """The n=1876 gold set — the gate with the statistical power to mean something.

    This is the one to run before and after a retrieval change. The hand set cannot
    resolve the effects involved: `asks` is worth +0.0795 MRR here and +0.000 there.
    """
    assert LARGE_CASES.exists(), f"missing gold set: {LARGE_CASES}"
    cmd = [sys.executable, str(SCRIPT), "--cases", str(LARGE_CASES),
           "--bar-mrr", LARGE_BAR_MRR, "--bar-recall", LARGE_BAR_RECALL]
    if os.environ.get("CRIB_EVAL_NO_DAEMON"):
        cmd.append("--no-daemon")
    rc, out = _run(cmd)
    if rc == 2:
        pytest.skip(f"eval environment not ready (crib/seeded project absent):\n{out}")
    assert rc == 0, f"large-set quality bars unmet:\n{out}"
