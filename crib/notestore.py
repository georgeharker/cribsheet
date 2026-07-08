"""The note-file store, extracted from Crib.

NoteStore owns note-file orchestration — path resolution (including source-anchored
in-situ docs), and the write path (stash the prior content to the version ring →
atomic save → reindex). It *references* the backends it drives (the vector store, the
IndexEngine, the VersionRing) rather than owning them, since retrieval, in-situ docs,
import, and generation share the same objects. Crib keeps thin delegators so its many
note callers are unchanged. Read/delete/move/versions migrate here in later steps.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from . import notes
from .sources import SRC_PREFIX, SourceRoots

if TYPE_CHECKING:
    from .indexer import IndexEngine, IndexResult
    from .notes import Note
    from .paths import Paths
    from .store import Store
    from .versions import VersionRing


class NoteStore:
    def __init__(self, paths: Paths, store: Store, index: IndexEngine,
                 versions: VersionRing) -> None:
        self.paths = paths
        self.store = store
        self.index = index
        self.versions = versions

    def dir(self, project: str) -> Path:
        d = self.paths.notes_dir(project)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def source_roots(self, project: str) -> SourceRoots:
        """Per-project registry of docs indexed in-situ (prefix -> repo root)."""
        return SourceRoots(self.paths.project_dir(project) / "doc-sources.json")

    def abspath(self, project: str, relpath: str) -> Path:
        """On-disk file for a note. Source-anchored docs (`sources/<repo>/…`) resolve
        to the repo file via the registry; everything else lives under the notes tree."""
        if relpath.startswith(SRC_PREFIX):
            src = self.source_roots(project).resolve(relpath)
            if src is not None:
                return src
        return self.dir(project) / relpath

    async def write(self, project: str, relpath: str, note: Note) -> IndexResult:
        """Stash prior content (ring), write atomically, then index."""
        path = self.abspath(project, relpath)
        if path.exists():
            existing = notes.load(path)
            if existing.id:
                self.versions.stash(existing.id, notes.serialize(
                    existing.frontmatter, existing.body))
        notes.ensure_id(note)
        note.path = path
        notes.save_atomic(note)
        return await self.index.index_file(project, self.dir(project), relpath)
