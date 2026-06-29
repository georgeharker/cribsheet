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
from pathlib import Path

_MUNGE = re.compile(r"[/.]")


def munge(path: Path) -> str:
    """Encode an absolute path the way Claude Code names its project dirs."""
    return _MUNGE.sub("-", str(path.resolve()))


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
    start = start.resolve()
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
        root_s = str(root.resolve())
        items = [b for b in self.all() if b.get("root") != root_s]
        items.append({"root": root_s, "project": project})
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2))
        tmp.replace(self._path)
