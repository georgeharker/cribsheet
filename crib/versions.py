"""Per-write version ring (DESIGN §8 Layer 1).

Before any write overwrites a note, the prior content is stashed here, keyed by
note id so it survives renames. Kept outside `notes/`, git-ignored, never
indexed. Recovery only via tools.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .paths import confine

# Ring entries are named "<seq>-<shorthash>.md". Anything else in the directory
# (an editor backup, a merge leftover, something a user dropped there) is not
# ours to interpret — parsing it for a sequence number used to raise ValueError
# and take down EVERY write for that note, since `stash` needs `_next_seq`.
_ENTRY_RE = re.compile(r"^(\d+)-[^/]*\.md$")
_warned_dirs: set[Path] = set()


@dataclass
class VersionEntry:
    seq: int
    name: str       # "<seq>-<shorthash>.md"
    path: Path
    mtime: float


def _ring_files(d: Path) -> list[tuple[int, Path]]:
    """`(seq, path)` for the well-formed entries in a ring dir, newest last.
    Strays are skipped, with one warning per directory per process."""
    out: list[tuple[int, Path]] = []
    strays: list[str] = []
    for p in d.glob("*.md"):
        if m := _ENTRY_RE.match(p.name):
            out.append((int(m.group(1)), p))
        else:
            strays.append(p.name)
    if strays and d not in _warned_dirs:
        _warned_dirs.add(d)
        print(f"[crib] version ring {d}: ignoring {len(strays)} file(s) not named "
              f"<seq>-<hash>.md (first: {sorted(strays)[0]})", file=sys.stderr)
    return sorted(out)


class VersionRing:
    def __init__(self, versions_dir: Path, keep: int = 20) -> None:
        self._dir = versions_dir
        self._keep = keep

    @property
    def dir(self) -> Path:
        """The ring's root — `data_dir/.versions` for a global project, or
        `<store>/.versions` for one whose notes live in a repo."""
        return self._dir

    @property
    def keep(self) -> int:
        return self._keep

    def _note_dir(self, note_id: str) -> Path:
        # id and version name are both caller-supplied (`note_restore(version=…)`)
        # — confined so a ring lookup can only ever read inside the ring.
        return confine(self._dir, note_id)

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
        entries = [VersionEntry(seq, p.name, p, p.stat().st_mtime)
                   for seq, p in _ring_files(d)]
        return sorted(entries, key=lambda e: e.seq, reverse=True)

    def read(self, note_id: str, name: str) -> str:
        return confine(self._note_dir(note_id), name).read_text()

    def _next_seq(self, d: Path) -> int:
        seqs = [seq for seq, _ in _ring_files(d)]
        return (max(seqs) + 1) if seqs else 1

    def _prune(self, d: Path) -> None:
        entries = [p for _, p in _ring_files(d)]
        excess = len(entries) - self._keep
        for p in entries[:max(0, excess)]:
            p.unlink(missing_ok=True)
