"""Git backing for the data dir (DESIGN §8 Layer 2, §14 sync).

Two roles over the same repo:
  - `snapshot`/`history`: local checkpoints of the markdown source of truth.
  - `sync`/`pull`/`push`: share notes across machines via a git remote.

Auto-detected for snapshot/history (active only when the data dir is a repo);
`init` bootstraps the repo + remote + `.gitignore` for sharing. Commits never
happen on write — only via these verbs.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

# Synced: notes, the version ring (conflict-free, keeps `restore` cross-machine),
# and host-namespaced claude-memory. Local-only: machine-specific binding paths
# and temp files.
_GITIGNORE = """\
# crib data repo — machine-specific / transient, never shared
memory-bindings.json
*.tmp
.tmp
"""


@dataclass
class SyncResult:
    ok: bool
    committed: bool
    pulled: bool
    pushed: bool
    conflicts: list[str]
    message: str
    changed: bool = False   # did a pull change files (→ caller should reconcile)


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

    def _ensure_gitignore(self) -> None:
        """Guarantee the data repo excludes machine-specific/transient files,
        even when the user created the repo by hand (no `init`)."""
        gi = self.data_dir / ".gitignore"
        if not gi.exists():
            gi.write_text(_GITIGNORE)

    # --- local checkpoints -------------------------------------------------
    def snapshot(self, message: str | None = None) -> str:
        if not self.enabled:
            return "git not enabled (data dir is not a repo; run `crib sync --remote <url>`)"
        self._ensure_gitignore()
        self._run("add", "-A")
        if not self._run("status", "--porcelain").stdout.strip():
            return "nothing to snapshot"
        r = self._run("commit", "-m", message or "crib snapshot")
        return r.stdout.strip() or r.stderr.strip()

    def history(self, relpath: str | None = None, limit: int = 20) -> list[str]:
        if not self.enabled:
            return []
        args = ["log", f"-{limit}", "--pretty=%h %ad %s", "--date=short"]
        if relpath:
            args += ["--", relpath]
        r = self._run(*args)
        return [ln for ln in r.stdout.splitlines() if ln.strip()]

    # --- sharing across machines (DESIGN §14) ------------------------------
    def init(self, remote: str | None = None) -> str:
        """Bootstrap the data dir as a shareable repo: `git init`, write the
        `.gitignore`, add the remote. Idempotent."""
        out = []
        if not self.enabled:
            self._run("init")
            out.append("initialized repo")
        gi = self.data_dir / ".gitignore"
        if not gi.exists():
            gi.write_text(_GITIGNORE)
            out.append("wrote .gitignore")
        if remote:
            if self._run("remote", "get-url", "origin").returncode == 0:
                self._run("remote", "set-url", "origin", remote)
                out.append("updated remote origin")
            else:
                self._run("remote", "add", "origin", remote)
                out.append("added remote origin")
        return "; ".join(out) or "already initialized"

    def _conflicts(self) -> list[str]:
        r = self._run("diff", "--name-only", "--diff-filter=U")
        return [ln for ln in r.stdout.splitlines() if ln.strip()]

    def pull(self) -> SyncResult:
        """Fetch + merge origin. On conflict, stop with the conflicted files —
        the caller tells the user to resolve them manually, then re-run."""
        if not self.enabled:
            return SyncResult(False, False, False, False, [], "git not enabled")
        before = self._run("rev-parse", "HEAD").stdout.strip()
        # --allow-unrelated-histories lets a machine join an existing remote: the
        # two independently-init'd trees merge as a union (conflicts only on
        # genuinely divergent same-path files). Harmless once histories are linked.
        r = self._run("pull", "--no-rebase", "--allow-unrelated-histories",
                      "origin", self._branch())
        conflicts = self._conflicts()
        if conflicts:
            return SyncResult(
                False, False, False, False, conflicts,
                "merge conflict — resolve the files below in "
                f"{self.data_dir}, commit, then re-run:\n  " + "\n  ".join(conflicts))
        if r.returncode != 0:
            # First sync against an empty remote: nothing to merge yet, not an error.
            if any(s in r.stderr.lower() for s in
                   ("couldn't find remote ref", "no such ref", "not our ref")):
                return SyncResult(True, False, True, False, [], "remote empty (first sync)")
            return SyncResult(False, False, False, False, [],
                              f"pull failed: {r.stderr.strip()}")
        after = self._run("rev-parse", "HEAD").stdout.strip()
        return SyncResult(True, False, True, False, [],
                          r.stdout.strip() or "up to date", changed=(before != after))

    def push(self) -> SyncResult:
        if not self.enabled:
            return SyncResult(False, False, False, False, [], "git not enabled")
        r = self._run("push", "-u", "origin", self._branch())
        if r.returncode != 0:
            return SyncResult(False, False, False, False, [],
                              f"push failed: {r.stderr.strip()}")
        return SyncResult(True, False, False, True, [],
                          r.stderr.strip() or r.stdout.strip() or "pushed")

    def sync(self, message: str | None = None) -> SyncResult:
        """commit → pull → push. Stops (without pushing) if the pull conflicts."""
        if not self.enabled:
            return SyncResult(False, False, False, False, [],
                              "git not enabled; run `crib sync --remote <url>` first")
        committed = "nothing to" not in self.snapshot(message)
        pulled = self.pull()
        if not pulled.ok:
            pulled.committed = committed
            return pulled
        pushed = self.push()
        return SyncResult(
            pushed.ok, committed, True, pushed.pushed, [],
            "; ".join(filter(None, [
                "committed" if committed else "",
                pulled.message if pulled.changed else "up to date",
                pushed.message])),
            changed=pulled.changed)

    def _branch(self) -> str:
        r = self._run("rev-parse", "--abbrev-ref", "HEAD")
        return r.stdout.strip() or "main"
