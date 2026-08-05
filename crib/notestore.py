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
from .notes import Note, NoteParseError
from .paths import check_relpath, confine
from .sources import SRC_PREFIX, SourceRoots
from .util import derived_ulid

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

    def notes_root(self, project: str) -> Path:
        """The project's notes dir as a PATH — resolved, never created.

        The read side of the split: a lookup naming a project that doesn't exist
        (a typo, a stale session pointer) must not CREATE it. `dir()` below is the
        write side. Before the split every `abspath()` — including `read`,
        `locate`, `version_content` and the learnings audit — mkdir'd on the way
        through, so one mistyped `project=` planted a permanent phantom namespace
        in `project_list`."""
        return self.paths.notes_dir(project)

    def dir(self, project: str) -> Path:
        """The project's notes dir, CREATED — the WRITE-side resolver (see
        `notes_root`). Call this only where a file is about to be written."""
        d = self.notes_root(project)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def source_roots(self, project: str) -> SourceRoots:
        """Per-project registry of docs indexed in-situ (prefix -> repo root)."""
        return SourceRoots(self.paths.project_dir(project) / "doc-sources.json")

    def abspath(self, project: str, relpath: str) -> Path:
        """On-disk file for a note. Source-anchored docs (`sources/<repo>/…`) resolve
        to the repo file via the registry; everything else lives under the notes tree.

        Every relpath here is a tool argument, so it is screened before either join:
        an absolute one would replace the base outright, `..` would walk out of the
        tree (or out of the source repo).

        Pure resolution — it does NOT create the notes dir (see `notes_root`);
        writers get the directory from `save_atomic`/`dir()`."""
        check_relpath(relpath, self.notes_root(project))
        if relpath.startswith(SRC_PREFIX):
            src = self.source_roots(project).resolve(relpath)
            if src is not None:
                return src
        return confine(self.notes_root(project), relpath)

    def _refuse_source(self, relpath: str, verb: str) -> None:
        """Source-anchored docs are indexed IN PLACE — their bytes belong to the
        repo, not to crib. Without this the note write verbs would stamp crib
        frontmatter into someone's README (`edit`) or delete it from their
        checkout (`forget`)."""
        if relpath.startswith(SRC_PREFIX):
            raise ValueError(
                f"cannot {verb} {relpath}: source files are indexed in place and "
                "owned by their repo — edit them in the checkout (`note_locate` "
                "gives the path); the watcher reindexes on save")

    def _stash_existing(self, project: str, relpath: str, path: Path,
                        fallback_id: str | None = None) -> str:
        """Stash a note's current bytes to the version ring before it is overwritten
        or unlinked; returns the id they landed under.

        EVERY note stashes — an id-less one included. Its bytes are keyed by the
        id the incoming write is about to stamp (`fallback_id`), else by a
        DERIVED id over (project, relpath): deterministic, so successive
        overwrites of the same path accumulate in one ring dir instead of
        scattering, and `delete`'s `recoverable_id` names a directory that really
        holds the content. Before this, a note with no `id:` in its frontmatter
        silently skipped the ring while `forget` still advertised the delete as
        recoverable — the one case where the promise was a lie.

        A note whose frontmatter no longer parses is stashed RAW — keyed by the
        `id:` still legible in the broken header, else the same fallbacks — rather
        than refused: otherwise a corrupt note could never be repaired by a write,
        and `forget` would drop its only copy."""
        raw = path.read_text()
        fallback = fallback_id or derived_ulid(project, relpath)
        try:
            fm, body = notes.parse(raw, path)
        except NoteParseError:
            note_id = notes.scan_id(raw) or fallback
            self.versions.stash(note_id, raw)
            return note_id
        note_id = fm.get("id") or fallback
        self.versions.stash(note_id, notes.serialize(fm, body))
        return note_id

    async def write(self, project: str, relpath: str, note: Note) -> IndexResult:
        """Stash prior content (ring), write atomically, then index."""
        self._refuse_source(relpath, "write")
        path = self.abspath(project, relpath)
        # id FIRST, then stash: the incoming note's id is what the prior bytes should
        # be filed under, so overwriting a note that had no `id:` still leaves its
        # history where `note_versions`/`note_restore` will look for it. (ensure_id
        # only touches the in-memory frontmatter; `save_atomic` below persists it.)
        notes.ensure_id(note)
        if path.exists():
            self._stash_existing(project, relpath, path, note.id)
        note.path = path
        notes.save_atomic(note)
        return await self.index.index_file(project, self.dir(project), relpath)

    def read(self, project: str, relpath: str) -> str:
        return self.abspath(project, relpath).read_text()

    async def delete(self, project: str, relpath: str) -> dict[str, Any]:
        """Delete a note: stash current content to the version ring (keyed by id, so
        it's recoverable), unlink, and drop its chunks via index_file's missing-path
        path."""
        self._refuse_source(relpath, "delete")
        path = self.abspath(project, relpath)
        note_id: str | None = None
        if path.exists():
            note_id = self._stash_existing(project, relpath, path)
            path.unlink()
        res = await self.index.index_file(project, self.dir(project), relpath)
        return {"project": project, "relpath": relpath, "removed": res.deleted,
                "recoverable_id": note_id}

    async def move(self, project: str, relpath: str, dst_proj: str,
                   dst_relpath: str) -> dict[str, Any]:
        """Relocate a note across projects and/or rename it, preserving its `id` (and
        thus version-ring history). One-way: write destination, drop source.

        Crash-window note: destination is written BEFORE the source is unlinked, so
        an interrupted move leaves TWO notes carrying one id (the alternative —
        unlink first — loses the note outright, which is worse). `reindex`'s
        full-project sweep reports any such `duplicate_ids` so the survivor is
        visible rather than silent."""
        self._refuse_source(relpath, "move")            # would unlink the repo's file
        self._refuse_source(dst_relpath, "move into")   # would write into the repo
        src = self.abspath(project, relpath)
        if not src.exists():
            raise ValueError(f"no such note: {relpath} in project {project!r}")
        # capture BEFORE the write — save_atomic mkdirs the destination notes dir
        created = not self.notes_root(dst_proj).exists()
        dst_path = self.abspath(dst_proj, dst_relpath)
        # Compare RESOLVED paths, not the (project, relpath) strings: `a/../b.md`,
        # a differently-spelled prefix, or a symlinked project dir all name the same
        # file, and a "move" onto itself would write the destination and then unlink
        # it — deleting the note it was asked to preserve.
        if src.resolve() == dst_path.resolve():
            raise ValueError("source and destination are the same")
        if dst_path.exists():
            raise ValueError(f"destination exists: {dst_relpath} in {dst_proj!r}")
        note = notes.load(src)              # carries the id in frontmatter
        dst = Note(path=dst_path, frontmatter=note.frontmatter, body=note.body)
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
        recreated = False
        if relpath:
            targets = [relpath]
        else:
            # Full reindex is the one safe place to switch embedder: if the stored
            # vectors' dim differs from the current embedder (a profile flip to a
            # bigger model), recreate the collection so all chunks re-embed at the new
            # dim. Chroma is shared across projects, so this wipes EVERY project's
            # chunks (and this project's in-situ `sources/…` docs, which the sweep
            # below doesn't walk) — reported as `recreated` so the caller can drive
            # the full recovery re-embed (Crib.reindex → reconcile_in_background).
            cur = self.store.current_dim()
            if cur is not None and cur != self.index.embedder.dim:
                print(f"crib: embedder dim {cur}→{self.index.embedder.dim}; recreating "
                      f"the vector collection (full re-embed)", file=sys.stderr)
                self.store.recreate()
                # The derived retrieval caches (BM25 corpus + summary alias
                # vectors) are built from the store and keyed per project — a wipe
                # invalidates ALL of them at once, not just this project's.
                # Without this a warm daemon kept serving BM25 hits for chunks
                # that no longer exist (and whose ids the dense side can't score).
                self.index.invalidate_all_caches()
                recreated = True
            disk = {str(p.relative_to(nd)) for p in nd.rglob("*.md")}
            # Source-anchored docs (`sources/<repo>/…`) live in the REPO, not the notes
            # tree — the on-disk sweep must NOT treat them as deleted (owned by
            # index_docs_insitu + the code watcher).
            indexed = {m.get("relpath")
                       for m in self.store.get_meta({"project": project}).values()
                       if not (m.get("relpath") or "").startswith(SRC_PREFIX)}
            targets = sorted(disk | {r for r in indexed if r})
        changed = removed = 0
        skipped: list[dict[str, str]] = []
        by_id: dict[str, list[str]] = {}
        for rp in targets:
            # One unreadable note (bad frontmatter from a hand edit or conflict
            # markers, bad encoding, a permission problem) must never abort the
            # sweep — the startup reconcile covers every note in the project.
            # Skip it, name it in the result, keep going.
            try:
                res = await self.index.index_file(project, nd, rp)
            except (NoteParseError, UnicodeDecodeError, OSError) as e:
                skipped.append({"relpath": rp, "error": str(e)})
                continue
            if res.note_id:
                by_id.setdefault(res.note_id, []).append(rp)
            changed += int(res.changed)
            removed += res.deleted
        out: dict[str, Any] = {"project": project, "files": len(targets),
                               "changed": changed, "removed": removed,
                               "skipped": skipped}
        # A `move` interrupted between writing the destination and unlinking the
        # source leaves two notes sharing one id — and a shared id means a shared
        # version ring, so the two histories interleave silently. The sweep already
        # knows every note's id (index_file returns it), so detecting it is free:
        # REPORT it and let a human pick which copy survives (crib must never
        # delete a note on a heuristic).
        dupes = [{"id": nid, "relpaths": sorted(rps)}
                 for nid, rps in sorted(by_id.items()) if len(rps) > 1]
        if dupes:
            out["duplicate_ids"] = dupes
            print(f"[crib] {project}: {len(dupes)} duplicate note id(s) — an "
                  f"interrupted move? " + "; ".join(
                      f"{d['id']}: {', '.join(d['relpaths'])}" for d in dupes),
                  file=sys.stderr)
        if recreated:
            out["recreated"] = True     # the whole store was wiped — see above
        return out

    def _ring_id(self, project: str, relpath: str) -> str | None:
        """The version-ring key for a note on disk.

        Deliberately does NOT go through `notes.load`: a note whose frontmatter
        stopped parsing is EXACTLY the one whose history you need, and loading it
        would raise `NoteParseError` before you could list — let alone restore —
        the good bytes sitting in the ring. So parse if we can, and otherwise scan
        the still-legible `id:` out of the broken header (the same salvage
        `_stash_existing` uses on the way in), falling back to the derived id an
        id-less note's stashes are keyed by."""
        raw = self.abspath(project, relpath).read_text()
        try:
            fm, _body = notes.parse(raw, relpath)
        except NoteParseError:
            return notes.scan_id(raw) or derived_ulid(project, relpath)
        return fm.get("id") or derived_ulid(project, relpath)

    def list_versions(self, project: str, relpath: str) -> list[dict[str, Any]]:
        note_id = self._ring_id(project, relpath)
        if not note_id:
            return []
        return [{"version": e.name, "seq": e.seq, "mtime": e.mtime}
                for e in self.versions.list(note_id)]

    def version_content(self, project: str, relpath: str, version: str) -> str:
        note_id = self._ring_id(project, relpath)
        if not note_id:
            raise ValueError("note has no id; nothing to restore")
        try:
            return self.versions.read(note_id, version)
        except OSError as e:
            # A name that isn't in the ring is a CALLER error, not an I/O surprise:
            # answer in the same currency as every other bad argument (ValueError,
            # naming what to do), so a wrong `version=` doesn't surface as a raw
            # FileNotFoundError from deep inside the ring.
            raise ValueError(
                f"no such version {version!r} for {relpath} in {project!r} — "
                f"`note_versions` lists what is recoverable") from e
