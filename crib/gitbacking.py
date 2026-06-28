"""Git backing for the data dir (DESIGN §8 Layer 2).

Auto-detected: active only when the data root is a git repo. No commit-on-write;
commits happen only via `snapshot`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitBacking:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    @property
    def enabled(self) -> bool:
        return (self.data_dir / ".git").is_dir()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self.data_dir,
            capture_output=True, text=True,
        )

    def snapshot(self, message: str | None = None) -> str:
        if not self.enabled:
            return "git not enabled (data dir is not a repo)"
        self._run("add", "-A")
        status = self._run("status", "--porcelain")
        if not status.stdout.strip():
            return "nothing to snapshot"
        msg = message or "crib snapshot"
        r = self._run("commit", "-m", msg)
        return r.stdout.strip() or r.stderr.strip()

    def history(self, relpath: str | None = None, limit: int = 20) -> list[str]:
        if not self.enabled:
            return []
        args = ["log", f"-{limit}", "--pretty=%h %ad %s", "--date=short"]
        if relpath:
            args += ["--", relpath]
        r = self._run(*args)
        return [ln for ln in r.stdout.splitlines() if ln.strip()]
