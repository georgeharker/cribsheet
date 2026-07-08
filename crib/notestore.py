"""The note-file store, extracted from Crib.

NoteStore owns note-file orchestration — path resolution (including source-anchored
in-situ docs), and the write path (stash the prior content to the version ring →
atomic save → reindex). It *references* the backends it drives (the vector store, the
IndexEngine, the VersionRing) rather than owning them, since retrieval, in-situ docs,
import, and generation share the same objects. Crib keeps thin delegators so its many
note callers are unchanged. Read/delete/move/versions migrate here in later steps.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import notes
from .notes import Note
from .sources import SRC_PREFIX, SourceRoots

if TYPE_CHECKING:
    from .indexer import IndexEngine, IndexResult
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

    def read(self, project: str, relpath: str) -> str:
        return self.abspath(project, relpath).read_text()

    async def delete(self, project: str, relpath: str) -> dict[str, Any]:
        """Delete a note: stash current content to the version ring (keyed by id, so
        it's recoverable), unlink, and drop its chunks via index_file's missing-path
        path."""
        path = self.abspath(project, relpath)
        note_id = None
        if path.exists():
            note = notes.load(path)
            note_id = note.id
            if note_id:
                self.versions.stash(
                    note_id, notes.serialize(note.frontmatter, note.body))
            path.unlink()
        res = await self.index.index_file(project, self.dir(project), relpath)
        return {"project": project, "relpath": relpath, "removed": res.deleted,
                "recoverable_id": note_id}

    async def move(self, project: str, relpath: str, dst_proj: str,
                   dst_relpath: str) -> dict[str, Any]:
        """Relocate a note across projects and/or rename it, preserving its `id` (and
        thus version-ring history). One-way: write destination, drop source."""
        src = self.abspath(project, relpath)
        if not src.exists():
            raise ValueError(f"no such note: {relpath} in project {project!r}")
        if project == dst_proj and dst_relpath == relpath:
            raise ValueError("source and destination are the same")
        # capture BEFORE any abspath(dst_proj) call — dir()/abspath mkdir the notes dir
        created = not self.paths.notes_dir(dst_proj).exists()
        if self.abspath(dst_proj, dst_relpath).exists():
            raise ValueError(f"destination exists: {dst_relpath} in {dst_proj!r}")
        note = notes.load(src)              # carries the id in frontmatter
        dst = Note(path=self.abspath(dst_proj, dst_relpath),
                   frontmatter=note.frontmatter, body=note.body)
        notes.save_atomic(dst)
        await self.index.index_file(dst_proj, self.dir(dst_proj), dst_relpath)
        src.unlink()                        # drop source + its chunks
        await self.index.index_file(project, self.dir(project), relpath)
        return {"from": {"project": project, "relpath": relpath},
                "to": {"project": dst_proj, "relpath": dst_relpath},
                "id": note.id, "created": created}

    async def reindex(self, project: str, relpath: str | None = None) -> dict[str, Any]:
        """Reindex a note, or fully reconcile a project when relpath is None (walks
        the UNION of on-disk notes and indexed paths — catches offline edits AND drops
        orphaned chunks). All idempotent via the hash gate."""
        nd = self.dir(project)
        if relpath:
            targets = [relpath]
        else:
            # Full reindex is the one safe place to switch embedder: if the stored
            # vectors' dim differs from the current embedder (a profile flip to a
            # bigger model), recreate the collection so all chunks re-embed at the new
            # dim. Chroma is shared across projects, so this wipes them all — hence
            # full-reindex-only; a --all sweep re-embeds the rest.
            cur = self.store.current_dim()
            if cur is not None and cur != self.index.embedder.dim:
                print(f"crib: embedder dim {cur}→{self.index.embedder.dim}; recreating "
                      f"the vector collection (full re-embed)", file=sys.stderr)
                self.store.recreate()
            disk = {str(p.relative_to(nd)) for p in nd.rglob("*.md")}
            # Source-anchored docs (`sources/<repo>/…`) live in the REPO, not the notes
            # tree — the on-disk sweep must NOT treat them as deleted (owned by
            # index_docs_insitu + the code watcher).
            indexed = {m.get("relpath")
                       for m in self.store.get_meta({"project": project}).values()
                       if not (m.get("relpath") or "").startswith(SRC_PREFIX)}
            targets = sorted(disk | {r for r in indexed if r})
        changed = removed = 0
        for rp in targets:
            res = await self.index.index_file(project, nd, rp)
            changed += int(res.changed)
            removed += res.deleted
        return {"project": project, "files": len(targets),
                "changed": changed, "removed": removed}

    def list_versions(self, project: str, relpath: str) -> list[dict[str, Any]]:
        note = notes.load(self.abspath(project, relpath))
        if not note.id:
            return []
        return [{"version": e.name, "seq": e.seq, "mtime": e.mtime}
                for e in self.versions.list(note.id)]

    def version_content(self, project: str, relpath: str, version: str) -> str:
        note = notes.load(self.abspath(project, relpath))
        if not note.id:
            raise ValueError("note has no id; nothing to restore")
        return self.versions.read(note.id, version)
