"""The pillar note-file store — one implementation, one instance per pillar.

NoteStore owns note-file orchestration — path resolution (including, for the
notes pillar, source-anchored in-situ docs), and the write path (stash the prior
content to the version ring → atomic save → reindex). A `StoreSpec` is all that
distinguishes the pillars (notes / design / plans / learnings): the sibling dir
under the project's data root, the `store` tag every chunk carries, and which
relpath prefixes the notes instance refuses because a facet owns that content.
It *references* the backends it drives (the vector store, the IndexEngine, the
VersionRing) rather than owning them — all four instances share the same
objects. Crib keeps thin delegators for the notes pillar so its many note
callers are unchanged; the facet layers hold their own instances.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import notes
from .errors import CribUserError
from .notes import Note, NoteParseError
from .paths import check_relpath, confine
from .sources import SRC_PREFIX, SourceRoots
from .util import derived_ulid

if TYPE_CHECKING:
    from .indexer import IndexEngine, IndexResult
    from .paths import Paths, ProjectPathResolver, ProjectPaths
    from .store import Store
    from .versions import VersionRing


@dataclass(frozen=True)
class StoreSpec:
    """What distinguishes one pillar store from another — everything else is the
    shared `NoteStore` implementation. `name` is the index scope tag every chunk
    carries; `segment` the sibling dir under the project's data root; `facet`
    names the verbs a refusal error should point at; `reserved` the relpath
    prefixes this store refuses because another pillar owns that content."""
    name: str
    segment: str
    facet: str | None = None
    reserved: tuple[str, ...] = ()


# Every facet pillar's content is refused on the note verbs — including the
# legacy `code-learnings/` spelling, whose files the migration moves to the
# `learnings/` sibling.
NOTES_SPEC = StoreSpec("notes", "notes",
                       reserved=("design/", "plans/",
                                 "learnings/", "code-learnings/"))
DESIGN_SPEC = StoreSpec("design", "design", facet="design")
PLANS_SPEC = StoreSpec("plans", "plans", facet="plan")
LEARNINGS_SPEC = StoreSpec("learnings", "learnings", facet="learning")


class NoteStore:
    def __init__(self, paths: Paths, store: Store, index: IndexEngine,
                 versions: VersionRing,
                 project_paths: ProjectPathResolver,
                 spec: StoreSpec = NOTES_SPEC) -> None:
        self.paths = paths
        self.store = store
        self.index = index
        self.versions = versions
        self.spec = spec
        # Where THIS project's data tier lives — global, or an adopted in-repo
        # store (docs/plans/repo-local-storage). Every note path and every ring
        # lookup below goes through it; nothing here derives a path from `paths`
        # directly any more.
        self.project_paths = project_paths
        self._rings: dict[Path, VersionRing] = {}

    def resolved(self, project: str) -> ProjectPaths:
        """This project's storage locations, REQUIRING them to be reachable — an
        adopted store whose repo isn't on this machine errors here (naming the
        token and the fix) instead of half-working."""
        return self.project_paths(project).require()

    def root(self, project: str) -> Path:
        """The project's dir for THIS pillar as a PATH — resolved, never created.

        The read side of the split: a lookup naming a project that doesn't exist
        (a typo, a stale session pointer) must not CREATE it. `dir()` below is the
        write side. Before the split every `abspath()` — including `read`,
        `locate`, `version_content` and the learnings audit — mkdir'd on the way
        through, so one mistyped `project=` planted a permanent phantom namespace
        in `project_list`."""
        return self.resolved(project).pillar_dir(self.spec.segment)

    def notes_root(self, project: str) -> Path:
        """Alias for `root` — the pre-split name, kept for the many callers that
        grew up when notes was the only pillar."""
        return self.root(project)

    def dir(self, project: str) -> Path:
        """The project's pillar dir, CREATED — the WRITE-side resolver (see
        `root`). Call this only where a file is about to be written."""
        d = self.root(project)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def versions_for(self, project: str) -> VersionRing:
        """The version ring this project's notes stash into.

        Global projects share one ring keyed by note id (`data_dir/.versions`,
        DESIGN §8). A project adopted into a repo gets its own ring INSIDE the
        store (`<store>/.versions`) so history travels with the notes — and is
        gitignored there, since the repo owns the notes but not the ring."""
        vd = self.project_paths(project).versions_dir
        if vd == self.versions.dir:
            return self.versions
        ring = self._rings.get(vd)
        if ring is None:
            from .versions import VersionRing as _Ring
            ring = self._rings[vd] = _Ring(vd, self.versions.keep)
        return ring

    def source_roots(self, project: str) -> SourceRoots:
        """Per-project registry of docs indexed in-situ (prefix -> repo root).
        Index tier — stays in the GLOBAL project dir even for an adopted project
        (its entries are absolute local repo paths, machine-specific by nature)."""
        return SourceRoots(self.project_paths(project).project_dir
                           / "doc-sources.json")

    def abspath(self, project: str, relpath: str) -> Path:
        """On-disk file for a note. In the notes pillar, source-anchored docs
        (`sources/<repo>/…`) resolve to the repo file via the registry; everything
        else lives under this pillar's tree.

        Every relpath here is a tool argument, so it is screened before either join:
        an absolute one would replace the base outright, `..` would walk out of the
        tree (or out of the source repo).

        Pure resolution — it does NOT create the pillar dir (see `root`);
        writers get the directory from `save_atomic`/`dir()`."""
        check_relpath(relpath, self.root(project))
        self._refuse_reserved(relpath)
        if self.spec.name == "notes" and relpath.startswith(SRC_PREFIX):
            src = self.source_roots(project).resolve(relpath)
            if src is not None:
                return src
        return confine(self.root(project), relpath)

    def _refuse_reserved(self, relpath: str) -> None:
        """A relpath another pillar owns is refused on READS as well as writes —
        after the split those files simply aren't in this tree, so resolving the
        path would at best 404 and at worst recreate a legacy subtree. The error
        names the facet verbs that are the way in, and the fact that direct file
        edits still work (the watcher reindexes them)."""
        hit = next((p for p in self.spec.reserved if relpath.startswith(p)), None)
        if hit is not None:
            facet = {"design/": "design", "plans/": "plan",
                     "code-learnings/": "learning", "learnings/": "learning"} \
                .get(hit, hit.rstrip("/"))
            raise CribUserError(
                f"{relpath}: `{hit}` content lives in its own store, not under "
                f"notes — use the {facet}_* verbs (e.g. {facet}_read "
                f"{relpath[len(hit):]}), or edit the file in the `{hit.rstrip('/')}`"
                f" sibling dir directly; the watcher reindexes on save")

    def _refuse_source(self, relpath: str, verb: str) -> None:
        """Source-anchored docs are indexed IN PLACE — their bytes belong to the
        repo, not to crib. Without this the note write verbs would stamp crib
        frontmatter into someone's README (`edit`) or delete it from their
        checkout (`forget`)."""
        if self.spec.name == "notes" and relpath.startswith(SRC_PREFIX):
            raise CribUserError(
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
        ring = self.versions_for(project)
        try:
            fm, body = notes.parse(raw, path)
        except NoteParseError:
            note_id = notes.scan_id(raw) or fallback
            ring.stash(note_id, raw)
            return note_id
        note_id = fm.get("id") or fallback
        ring.stash(note_id, notes.serialize(fm, body))
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
        return await self.index.index_file(project, self.dir(project), relpath,
                                           store=self.spec.name)

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
        res = await self.index.index_file(project, self.dir(project), relpath,
                                          store=self.spec.name)
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
            raise CribUserError(f"no such note: {relpath} in project {project!r}")
        # capture BEFORE the write — save_atomic mkdirs the destination notes dir
        created = not self.notes_root(dst_proj).exists()
        dst_path = self.abspath(dst_proj, dst_relpath)
        # Compare RESOLVED paths, not the (project, relpath) strings: `a/../b.md`,
        # a differently-spelled prefix, or a symlinked project dir all name the same
        # file, and a "move" onto itself would write the destination and then unlink
        # it — deleting the note it was asked to preserve.
        if src.resolve() == dst_path.resolve():
            raise CribUserError("source and destination are the same")
        if dst_path.exists():
            raise CribUserError(f"destination exists: {dst_relpath} in {dst_proj!r}")
        note = notes.load(src)              # carries the id in frontmatter
        dst = Note(path=dst_path, frontmatter=note.frontmatter, body=note.body)
        notes.save_atomic(dst)
        await self.index.index_file(dst_proj, self.dir(dst_proj), dst_relpath,
                                    store=self.spec.name)
        src.unlink()                        # drop source + its chunks
        await self.index.index_file(project, self.dir(project), relpath,
                                    store=self.spec.name)
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
            disk = {str(p.relative_to(nd)) for p in nd.rglob("*.md")}
            # The indexed side is scoped to THIS pillar (absence rule: a chunk
            # indexed before the split has no `store` key and counts as notes) —
            # without the scope, the notes sweep would see every facet chunk as
            # "indexed but gone from disk" and drop it. Source-anchored docs
            # (`sources/<repo>/…`) live in the REPO, not the notes tree — the
            # sweep must NOT treat them as deleted (owned by index_docs_insitu +
            # the code watcher).
            indexed = {m.get("relpath")
                       for m in self.store.get_meta({"project": project}).values()
                       if ((m.get("store") or "notes") == self.spec.name
                           and not (m.get("relpath") or "").startswith(SRC_PREFIX))}
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
                res = await self.index.index_file(project, nd, rp,
                                                  store=self.spec.name)
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
                for e in self.versions_for(project).list(note_id)]

    def version_content(self, project: str, relpath: str, version: str) -> str:
        note_id = self._ring_id(project, relpath)
        if not note_id:
            raise CribUserError("note has no id; nothing to restore")
        try:
            return self.versions_for(project).read(note_id, version)
        except OSError as e:
            # A name that isn't in the ring is a CALLER error, not an I/O surprise:
            # answer in the same currency as every other bad argument (ValueError,
            # naming what to do), so a wrong `version=` doesn't surface as a raw
            # FileNotFoundError from deep inside the ring.
            raise CribUserError(
                f"no such version {version!r} for {relpath} in {project!r} — "
                f"`note_versions` lists what is recoverable") from e
