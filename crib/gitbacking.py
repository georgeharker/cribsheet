"""Git backing for the data dir (DESIGN §8 Layer 2, §14 sync).

Two roles over the same repo:
  - `snapshot`/`history`: local checkpoints of the markdown source of truth.
  - `sync`/`pull`/`push`: share notes across machines via a git remote.

Auto-detected for snapshot/history (active only when the data dir is a repo);
`init` bootstraps the repo + remote + `.gitignore` for sharing. Commits never
happen on write — only via these verbs.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
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

# Per-project code-index local state: the source repo's absolute path on THIS
# machine (regenerated on index). The symbol tomls themselves ARE shared.
.source_root
"""

# Route notes AND symbol_index records through the crib merge driver (DESIGN §14):
# a note's header merges deterministically (provenance never conflicts) while real
# body conflicts still surface; a symbol_index `.toml` (flat record) merges fully
# clean (description/edge/mtime divergence auto-resolves). Committed so it travels.
_GITATTRIBUTES = "*.md merge=cribnote\n**/symbol_index/*.toml merge=cribnote\n"

# The driver itself is registered in each machine's *local* `.git/config` (git
# config doesn't sync), so it must be (re)ensured on every machine — see
# `_ensure_merge_driver`.
_MERGE_DRIVER_NAME = "crib frontmatter-aware note merge"


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

    def _pending_join(self) -> bool:
        """True when `origin/<branch>` has been fetched but isn't part of local
        history yet — a join (first merge) is imminent. Cheap and local: reads
        only refs already fetched, never the network."""
        branch = self._branch()
        if self._run("rev-parse", "--verify", "-q", f"origin/{branch}").returncode != 0:
            return False
        if self._run("rev-parse", "--verify", "-q", "HEAD").returncode != 0:
            return True                  # unborn local branch → nothing merged yet
        return self._run("merge-base", "HEAD", f"origin/{branch}").returncode != 0

    def _remote_file(self, name: str) -> str | None:
        """When a join is pending and the fetched branch carries `name`, its
        content — the joining machine seeds the REMOTE's copy of a shared file
        rather than its own default: seeding ours would both-added-conflict with
        the join merge whenever the contents diverge, yet the file must exist
        before that merge (`.gitattributes` routes the merge itself)."""
        if not self._pending_join():
            return None
        r = self._run("show", f"origin/{self._branch()}:{name}")
        return r.stdout if r.returncode == 0 else None

    def _ensure_gitignore(self) -> None:
        """Guarantee the data repo excludes machine-specific/transient files,
        even when the user created the repo by hand (no `init`). A pending join
        seeds the remote branch's copy, never our default over theirs."""
        gi = self.data_dir / ".gitignore"
        if not gi.exists():
            gi.write_text(self._remote_file(".gitignore") or _GITIGNORE)

    def _ensure_gitattributes(self) -> None:
        """Ensure `*.md` routes through the cribnote merge driver (committed).
        A pending join seeds the remote branch's copy, never our default over
        theirs — the attributes must exist BEFORE the join merge (they route
        it), so deferring isn't an option."""
        ga = self.data_dir / ".gitattributes"
        if not ga.exists():
            ga.write_text(self._remote_file(".gitattributes") or _GITATTRIBUTES)

    def _ensure_merge_driver(self) -> None:
        """Register the cribnote merge driver in this machine's local git config.
        Git config isn't synced, so every machine must do this — invoking the CLI
        back through `python -m crib` keeps it PATH-independent."""
        cmd = (f"{shlex.quote(sys.executable)} -m crib merge-driver "
               "%O %A %B %P")
        self._run("config", "merge.cribnote.name", _MERGE_DRIVER_NAME)
        self._run("config", "merge.cribnote.driver", cmd)

    def _ensure_repo_config(self) -> None:
        """Idempotently ensure the shareable-repo scaffolding: ignore rules,
        merge attributes (committed), and the local merge-driver registration."""
        self._ensure_gitignore()
        self._ensure_gitattributes()
        self._ensure_merge_driver()

    def current_remote(self) -> str | None:
        """The configured `origin` URL, or None."""
        r = self._run("remote", "get-url", "origin")
        return r.stdout.strip() if r.returncode == 0 else None

    def state(self) -> dict:
        """Local-only sync health for `status`: remote, uncommitted count, last
        commit, and ahead/behind vs the last-FETCHED origin ref (no network —
        the numbers are as fresh as the last fetch/pull)."""
        if not self.enabled:
            return {"enabled": False}
        branch = self._branch()
        dirty = len([ln for ln in self._run("status", "--porcelain").stdout.splitlines()
                     if ln.strip()])
        last = self._run("log", "-1", "--pretty=%h %ad %s",
                         "--date=short").stdout.strip() or None
        out: dict = {"enabled": True, "remote": self.current_remote(),
                     "branch": branch, "dirty": dirty, "last_commit": last}
        if self._run("rev-parse", "--verify", "-q",
                     f"origin/{branch}").returncode == 0:
            out["ahead"] = int(self._run(
                "rev-list", "--count", f"origin/{branch}..HEAD").stdout.strip() or 0)
            out["behind"] = int(self._run(
                "rev-list", "--count", f"HEAD..origin/{branch}").stdout.strip() or 0)
        return out

    # --- local checkpoints -------------------------------------------------
    def snapshot(self, message: str | None = None) -> str:
        if not self.enabled:
            return "git not enabled (data dir is not a repo; run `crib setup --remote <url>`)"
        self._ensure_repo_config()
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
        `.gitignore` + `.gitattributes`, register the merge driver, add the
        remote. Idempotent. When the remote already has our branch (joining, not
        bootstrapping), the shared files are seeded from the REMOTE's copies so
        the join merge can't both-added-conflict on them — fetch first so
        `_pending_join`/`_remote_file` can see the branch."""
        out = []
        if not self.enabled:
            self._run("init")
            out.append("initialized repo")
        self._ensure_merge_driver()
        if remote:
            if self._run("remote", "get-url", "origin").returncode == 0:
                self._run("remote", "set-url", "origin", remote)
                out.append("updated remote origin")
            else:
                self._run("remote", "add", "origin", remote)
                out.append("added remote origin")
            self._run("fetch", "origin")     # best-effort; visibility for the join guard
        for f, ensure in ((".gitignore", self._ensure_gitignore),
                          (".gitattributes", self._ensure_gitattributes)):
            if not (self.data_dir / f).exists():
                ensure()
                out.append(f"wrote {f}")
        return "; ".join(out) or "already initialized"

    def setup(self, remote: str) -> SyncResult:
        """New-machine onboarding: join an existing shared-notes remote.

        The remote already carries `.gitignore`/`.gitattributes`, so we must NOT
        write local copies before pulling — untracked files collide with the
        merge and abort it. Order: init the repo + remote, register the merge
        driver (a `.git/config`-only change), fetch, and if the remote branch
        exists, hard-adopt its tree (overwriting any stray untracked files);
        only then ensure the shared files (now no-ops the remote provided).
        Falls back to the plain init+pull path for a brand-new/empty remote."""
        if not self.enabled:
            self._run("init")
        if self._run("remote", "get-url", "origin").returncode == 0:
            self._run("remote", "set-url", "origin", remote)
        else:
            self._run("remote", "add", "origin", remote)
        self._ensure_merge_driver()          # config only — safe before checkout

        branch = self._branch()
        fetched = self._run("fetch", "origin")
        remote_has_branch = self._run(
            "rev-parse", "--verify", f"origin/{branch}").returncode == 0
        if fetched.returncode == 0 and remote_has_branch:
            # Adopt the remote tree. Drop the two shared files first if they're
            # present-but-untracked (e.g. from a prior failed setup) so checkout
            # can't collide — the remote's tracked versions replace them.
            for f in (".gitignore", ".gitattributes"):
                (self.data_dir / f).unlink(missing_ok=True)
            self._run("checkout", "-B", branch, f"origin/{branch}")
            self._run("branch", f"--set-upstream-to=origin/{branch}", branch)
            self._ensure_gitignore()         # no-ops: remote provided them
            self._ensure_gitattributes()
            return SyncResult(True, False, True, False, [],
                              f"joined {remote} at origin/{branch}", changed=True)
        # Brand-new / empty remote: fall back to bootstrap + (no-op) pull.
        self.init(remote)
        return self.pull()

    def _conflicts(self) -> list[str]:
        r = self._run("diff", "--name-only", "--diff-filter=U")
        return [ln for ln in r.stdout.splitlines() if ln.strip()]

    def pull(self) -> SyncResult:
        """Fetch + merge origin. On conflict, stop with the conflicted files —
        the caller tells the user to resolve them manually, then re-run."""
        if not self.enabled:
            return SyncResult(False, False, False, False, [], "git not enabled")
        self._run("fetch", "origin")     # best-effort, pull refetches; makes a joinable
        self._ensure_repo_config()       # branch visible so the shared files defer to it
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
        # Post-merge: if the join deferred the shared files and the remote turned
        # out not to carry them (older remote), write them now (no-op otherwise).
        self._ensure_gitignore()
        self._ensure_gitattributes()
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
