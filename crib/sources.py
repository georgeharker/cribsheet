"""Source-anchored doc registry — the map that lets crib index a repo's docs
*in-situ* (never copied) yet still resolve them from anywhere.

A source-anchored note's bytes live in the source repo, not under the crib notes
tree. Its identity is a relpath under a reserved prefix — `sources/<repo>/<rel>` —
and this registry records `prefix -> absolute repo root` so `abspath`, reconcile
and the file watcher can turn that relpath back into the on-disk source file
without needing the repo's `.crib` to be reachable from the current cwd.

The registry is machine-local (like the Chroma index it feeds): the markdown is
git-synced, but the vectors — and these abs roots — are rebuilt per machine.
"""
from __future__ import annotations

import json
from pathlib import Path

# Reserved relpath prefix for source-anchored docs. Crib-owned notes are either
# top-level `<slug>.md` or under `imported/` / `claude-memory/` / `code-learnings/`,
# so a `sources/` subtree never collides with them.
SRC_PREFIX = "sources/"


def src_relpath(repo: str, rel_to_repo: str) -> str:
    """Identity relpath for a doc indexed in-situ from `repo` at `rel_to_repo`."""
    return f"{SRC_PREFIX}{repo}/{Path(rel_to_repo).as_posix()}"


class SourceRoots:
    """Per-project `prefix -> abs repo root` registry, persisted as JSON."""

    def __init__(self, path: Path):
        self.path = path                       # <project_dir>/doc-sources.json
        self._map: dict[str, str] = {}
        if path.exists():
            try:
                self._map = json.loads(path.read_text()) or {}
            except (json.JSONDecodeError, OSError):
                self._map = {}

    def upsert(self, prefix: str, root: str | Path) -> None:
        self._map[prefix] = str(Path(root))
        self._save()

    def remove(self, prefix: str) -> None:
        if self._map.pop(prefix, None) is not None:
            self._save()

    def all(self) -> dict[str, str]:
        return dict(self._map)

    def resolve(self, relpath: str) -> Path | None:
        """relpath (`sources/<repo>/<rel>`) -> on-disk source file, or None if the
        relpath isn't source-anchored. Longest prefix wins (nested roots)."""
        for prefix in sorted(self._map, key=len, reverse=True):
            if relpath.startswith(prefix):
                return Path(self._map[prefix]) / relpath[len(prefix):]
        return None

    def prefix_for(self, root: str | Path) -> str | None:
        """The registered prefix whose root is `root`, if any (reverse lookup)."""
        target = str(Path(root))
        for prefix, r in self._map.items():
            if r == target:
                return prefix
        return None

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._map, indent=2, sort_keys=True))
