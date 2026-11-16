"""`.github/scripts/wait-for-release-tests.sh`: the OpenCode and Pi releases
publish only on release.yml's verdict for the same tag. Exercised against a fake
`gh` that serves canned API answers (and applies `--jq` with the real jq)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / ".github"
    / "scripts"
    / "wait-for-release-tests.sh"
)

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")

FAKE_GH = r"""#!/usr/bin/env bash
# fake `gh api URL --jq EXPR`: the fixture for URL, filtered by jq -r
url="$2"; expr="${4:-.}"
echo "$url" >> "$STUB/calls"
case "$url" in
  */jobs*) f=jobs.json ;;
  *workflows/release.yml/runs*) f=runs.json ;;
  *) f=run.json ;;
esac
jq -r "$expr" "$STUB/$f"
"""


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(tmp_path: Path, runs: list[dict], jobs: list[dict], minutes: str = "1"):
    now = datetime.now(UTC)
    (tmp_path / "bin").mkdir()
    gh = tmp_path / "bin" / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    (tmp_path / "run.json").write_text(json.dumps({"created_at": _iso(now)}))
    (tmp_path / "runs.json").write_text(json.dumps({"workflow_runs": runs}))
    (tmp_path / "jobs.json").write_text(json.dumps({"jobs": jobs}))
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "STUB": str(tmp_path),
        "GH_TOKEN": "x",
        "REPO": "o/r",
        "SHA": "abc",
        "TAG": "v1.2.3",
        "RUN_ID": "99",
        "WAIT_MINUTES": minutes,
        "WAIT_SLEEP": "0",
    }
    r = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=120
    )
    calls = (tmp_path / "calls").read_text() if (tmp_path / "calls").exists() else ""
    return r.returncode, r.stdout, calls


def _now(**kw) -> str:
    return _iso(datetime.now(UTC) + timedelta(**kw))


def test_tests_passed_once_build_started(tmp_path):
    code, out, calls = _run(
        tmp_path,
        [{"id": 1, "status": "in_progress", "created_at": _now()}],
        [
            {"name": "tests / tests", "conclusion": "success", "status": "completed"},
            {"name": "build", "conclusion": None, "status": "in_progress"},
        ],
    )
    assert code == 0 and "passed" in out
    assert "branch=v1.2.3" in calls  # only this tag's runs


def test_a_failed_test_job_stops_the_publish(tmp_path):
    code, out, _ = _run(
        tmp_path,
        [{"id": 1, "status": "in_progress", "created_at": _now()}],
        [{"name": "tests / tests", "conclusion": "failure", "status": "completed"}],
    )
    assert code == 1 and "did not pass" in out


def test_a_run_that_ended_without_its_build_stops_at_once(tmp_path):
    code, out, _ = _run(
        tmp_path,
        [{"id": 1, "status": "completed", "created_at": _now()}],
        [],
        minutes="60",
    )
    assert code == 1 and "ended without" in out


def test_a_skipped_build_stops_the_publish(tmp_path):
    code, _, _ = _run(
        tmp_path,
        [{"id": 1, "status": "completed", "created_at": _now()}],
        [
            {"name": "tests / tests", "conclusion": "success", "status": "completed"},
            {"name": "build", "conclusion": "skipped", "status": "completed"},
        ],
    )
    assert code == 1


def test_an_old_run_for_a_re_pushed_tag_is_never_the_verdict(tmp_path):
    """Only an earlier run exists (the tag was deleted and pushed again): the
    script waits for this push's run, and gives up rather than pass."""
    code, out, _ = _run(
        tmp_path,
        [{"id": 1, "status": "completed", "created_at": _now(days=-2)}],
        [
            {"name": "tests / tests", "conclusion": "success", "status": "completed"},
            {"name": "build", "conclusion": "success", "status": "completed"},
        ],
        minutes="0",
    )
    assert code == 1 and "waiting for release.yml to start" in out
