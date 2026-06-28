"""Per-write version ring (DESIGN §8 Layer 1).

Before any write overwrites a note, the prior content is stashed here, keyed by
note id so it survives renames. Kept outside `notes/`, git-ignored, never
indexed. Recovery only via tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class VersionEntry:
    seq: int
    name: str       # "<seq>-<shorthash>.md"
    path: Path
    mtime: float


class VersionRing:
    def __init__(self, versions_dir: Path, keep: int = 20) -> None:
        self._dir = versions_dir
        self._keep = keep

    def _note_dir(self, note_id: str) -> Path:
        return self._dir / note_id

    def stash(self, note_id: str, content: str) -> VersionEntry | None:
        """Save `content` as the newest version; prune to `keep`."""
        if self._keep <= 0 or not note_id:
            return None
        from .util import short_hash

        d = self._note_dir(note_id)
        d.mkdir(parents=True, exist_ok=True)
        seq = self._next_seq(d)
        name = f"{seq:06d}-{short_hash(content)}.md"
        path = d / name
        path.write_text(content)
        self._prune(d)
        return VersionEntry(seq, name, path, path.stat().st_mtime)

    def list(self, note_id: str) -> list[VersionEntry]:
        d = self._note_dir(note_id)
        if not d.is_dir():
            return []
        entries = []
        for p in d.glob("*.md"):
            seq = int(p.name.split("-", 1)[0])
            entries.append(VersionEntry(seq, p.name, p, p.stat().st_mtime))
        return sorted(entries, key=lambda e: e.seq, reverse=True)

    def read(self, note_id: str, name: str) -> str:
        return (self._note_dir(note_id) / name).read_text()

    def _next_seq(self, d: Path) -> int:
        seqs = [int(p.name.split("-", 1)[0]) for p in d.glob("*.md")]
        return (max(seqs) + 1) if seqs else 1

    def _prune(self, d: Path) -> None:
        entries = sorted(d.glob("*.md"), key=lambda p: int(p.name.split("-", 1)[0]))
        excess = len(entries) - self._keep
        for p in entries[:max(0, excess)]:
            p.unlink(missing_ok=True)
