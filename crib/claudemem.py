"""Locate (and mirror) Claude Code's harness memory (DESIGN §13).

Claude Code writes per-project memory as markdown under
`$CLAUDE_CONFIG_DIR/projects/<munged-path>/memory/*.md` (plus a `MEMORY.md`
index). cribsheet mirrors those into a crib project so they're searchable
alongside everything else.

The directory name is the project's **absolute launch path** with `/` and `.`
both replaced by `-` (e.g. `/home/u/.cache/x` -> `-home-u--cache-x`). The munge
is lossy (real names contain `-`), so it's forward-only: we munge a known root
to find its dir; we never reverse a dir name back to a path.
"""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path

_MUNGE = re.compile(r"[/.]")


def hostslug() -> str:
    """A filesystem-safe short host id, used to namespace mirrored memory per
    machine so two machines' harness memories merge instead of colliding when the
    crib data dir is git-synced."""
    name = socket.gethostname().split(".")[0]
    return re.sub(r"[^a-z0-9_-]", "-", name.lower()) or "host"


def resolve_path(path: Path) -> Path:
    """Canonicalize a path the way the harness names its dirs: expand `~` (to the
    OS-native home — `/Users/…` on macOS, `/home/…` on Linux), then realpath so
    symlinks are followed (matching the harness's `getcwd`; e.g. `/tmp`→`/private/tmp`).

    No macOS special-casing is needed: firmlinks are transparent under realpath —
    `/Users`, `/private`, `/Volumes` etc. all resolve to themselves, never to
    `/System/Volumes/Data/…`. (The lone exception is `/home`, an autofs mount, not a
    firmlink and not a macOS project root.) The single seam: `munge`, root discovery,
    and binding keys all go through here."""
    return Path(path).expanduser().resolve()


def munge(path: Path) -> str:
    """Encode a path the way Claude Code names its project dirs: canonicalize
    (`resolve_path`), then collapse every `/` and `.` to `-`."""
    return _MUNGE.sub("-", str(resolve_path(path)))


def claude_config_dir() -> Path:
    """`$CLAUDE_CONFIG_DIR`, else `~/.claude` (Claude Code's default)."""
    if v := os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(v).expanduser()
    return Path.home() / ".claude"


def harness_memory_dir(root: Path) -> Path:
    """The harness memory dir for a project rooted at `root`."""
    return claude_config_dir() / "projects" / munge(root) / "memory"


def find_harness_root(start: Path) -> Path | None:
    """Walk up from `start` to the nearest dir that has a harness memory dir —
    so `crib import-memory` works from a subdir, like `.crib` discovery does."""
    start = resolve_path(start)
    for d in (start, *start.parents):
        if harness_memory_dir(d).is_dir():
            return d
    return None


class MemoryBindings:
    """Persistent registry of (root -> crib project) mirror bindings.

    `crib import-memory` upserts a binding when it syncs a repo; the daemon's
    live mirror reads them to know which harness dirs to watch. Keyed by root
    (absolute), so re-importing a repo updates rather than duplicates.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def all(self) -> list[dict[str, str]]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text())
        except (OSError, ValueError):
            return []

    def upsert(self, root: Path, project: str) -> None:
        root_s = str(resolve_path(root))
        items = [b for b in self.all() if b.get("root") != root_s]
        items.append({"root": root_s, "project": project})
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2))
        tmp.replace(self._path)
