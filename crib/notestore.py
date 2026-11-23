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

from .access import WriteAccess
from . import facets, notes
from .chunk import kind_of
from .facets import FacetSpec
from .errors import CribUserError

from .notes import Note, NoteParseError
from .paths import check_relpath, confine
from .sources import SRC_PREFIX, SourceRoots
from .util import derived_ulid

if TYPE_CHECKING:
    from .indexer import IndexEngine, IndexResult
    from .paths import FacetLocation, Paths, ProjectPathResolver, ProjectPaths
    from .store import Store
    from .versions import VersionRing


class PendingMove(CribUserError):
    """A mapped facet whose items still wait to be moved into the mapping."""


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


def store_spec(facet: FacetSpec) -> StoreSpec:
    """The note store for a facet. The home facet (notes) refuses every other
    facet's store directory as a relpath prefix, including legacy spellings
    (`code-learnings/`, whose files the migration moves to `learnings/`)."""
    if facet.noun != facets.HOME:
        return StoreSpec(facet.store, facet.store, facet=facet.noun)
    others = [s for s in facets.FACETS if s.noun != facet.noun]
    return StoreSpec(facet.store, facet.store, reserved=tuple(
        f"{name}/" for s in others for name in (s.store, *s.legacy_stores)))


# One per facet in the registry, by noun.
STORE_SPECS = {spec.noun: store_spec(spec) for spec in facets.FACETS}
NOTES_SPEC = STORE_SPECS["note"]
DESIGN_SPEC = STORE_SPECS["design"]
PLANS_SPEC = STORE_SPECS["plan"]
LEARNINGS_SPEC = STORE_SPECS["learning"]


class NoteStore:
    def __init__(self, paths: Paths, store: Store, index: IndexEngine,
                 versions: VersionRing,
                 project_paths: ProjectPathResolver,
                 spec: StoreSpec = NOTES_SPEC,
                 access: WriteAccess | None = None) -> None:
        self.paths = paths
        # which projects this instance may write (crib/access.py); None = all
        self.access = access or WriteAccess()
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

    def location(self, project: str) -> FacetLocation | None:
        """This facet's mapped location in `project` (`.crib` `facets:`, recorded
        in the stub), or None when its items live under the data root."""
        return self.project_paths(project).mappings.get(self.spec.segment)

    def prefixed(self, project: str) -> list[FacetLocation]:
        """This facet's ADDITIVE mapped locations in `project`, longest prefix
        first: the notes facet's docs (`sources/<repo>/`, read-only, ids derived
        from the relpath), from the sources registry the doc sweep keeps."""
        from .paths import FacetLocation

        if self.spec.name != "notes":
            return []
        roots = self.source_roots(project).all()
        return [FacetLocation(root=Path(roots[p]), token=roots[p], read_only=True,
                              ids="derived", prefix=p)
                for p in sorted(roots, key=len, reverse=True)]

    def pending_move(self, project: str) -> str | None:
        """Why this facet can't be served in `project` yet, or None: it is mapped
        (an exclusive location) but its items are still under the data root,
        waiting for `crib project adopt` to move them there."""
        loc = self.location(project)
        if loc is None or loc.prefix is not None:
            return None
        home = self.project_paths(project).home_dir(self.spec.segment)
        # asked on every read of the facet: the usual answer (nothing waiting:
        # no home dir, or an empty one) must stay cheap, so stop at the first
        if not home.is_dir() or next(home.rglob("*.md"), None) is None:
            return None
        n = sum(1 for _ in home.rglob("*.md"))
        noun = self.spec.facet or "note"
        return (
            f"project {project!r} maps its {noun} items to {loc.token}, but {n} are "
            f"still under crib's data dir ({home}): run `crib project adopt` in that "
            "repo to move them there (ids and history kept)"
        )

    def _prefixed_at(self, project: str, relpath: str) -> FacetLocation | None:
        return next((loc for loc in self.prefixed(project)
                     if loc.prefix and relpath.startswith(loc.prefix)), None)

    def _policy(self, project: str) -> dict[str, Any]:
        """The index policy of this facet's location in `project` (see
        IndexEngine.index_file): a mapping's ids and read-only flag."""
        loc = self.location(project)
        return {} if loc is None else {"ids": loc.ids, "read_only": loc.read_only}

    def _refuse_read_only(self, project: str, verb: str) -> None:
        """A project this instance holds read-only (crib/access.py), or a
        read-only mapped location, is indexed and searched, never written."""
        self.access.require(project, verb)
        loc = self.location(project)
        if loc is not None and loc.read_only:
            noun = self.spec.facet or "note"
            raise CribUserError(
                f"{verb} refused: project {project!r} maps its {noun} items from "
                f"a read-only location ({loc.token}); change them in that repo "
                "(a writable checkout), and crib picks the edit up"
            )

    def suffix(self, project: str) -> str:
        """What a new item's filename ends with here: its mapping's glob suffix
        (FacetLocation.suffix), else `.md`."""
        loc = self.location(project)
        return loc.suffix if loc is not None and loc.prefix is None else ".md"

    def _refuse_unowned(self, project: str, relpath: str, verb: str) -> None:
        """A mapped location's items are the files its glob picks: a write to
        any other relpath there would make a file the facet never indexes or
        watches (and every reconcile re-reads)."""
        loc = self.location(project)
        if loc is not None and loc.prefix is None and not loc.owns(relpath):
            noun = self.spec.facet or "note"
            raise CribUserError(
                f"{verb} refused: {relpath!r} isn't one of the {noun} files of "
                f"project {project!r} (its mapping {loc.token} takes `{loc.glob}`); "
                "pick a relpath that glob matches")

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
        if (why := self.pending_move(project)) is not None:
            raise PendingMove(why)
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
        if (loc := self.location(project)) is not None:
            # a mapped facet's history lives beside its files, ignored by that
            # repo's git: the repo owns the files, crib owns their history
            vd = loc.versions_dir
            if not (ignore := vd / ".gitignore").exists() and loc.available:
                vd.mkdir(parents=True, exist_ok=True)
                ignore.write_text("*\n")
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
        if (loc := self._prefixed_at(project, relpath)) is not None:
            assert loc.prefix is not None
            return loc.root / relpath[len(loc.prefix):]
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
        # the whole `sources/` namespace, registered or not: no relpath under it
        # is ever crib's to write (a prefixed location is read-only by nature)
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
        if self._policy(project).get("ids") == "derived":
            ring.stash(fallback, raw)  # the id is the path's (a file's own `id:` isn't crib's)
            return fallback
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
        self._refuse_read_only(project, "write")
        self._refuse_unowned(project, relpath, "write")
        path = self.abspath(project, relpath)
        policy = self._policy(project)
        # id FIRST, then stash: the incoming note's id is what the prior bytes should
        # be filed under, so overwriting a note that had no `id:` still leaves its
        # history where `note_versions`/`note_restore` will look for it. (ensure_id
        # only touches the in-memory frontmatter; `save_atomic` below persists it.)
        # A `derived`-id location never has an id written: its id is its relpath's.
        if policy.get("ids") != "derived":
            notes.ensure_id(note)
        if path.exists():
            self._stash_existing(project, relpath, path, note.id)
        note.path = path
        notes.save_atomic(note)
        return await self.index.index_file(project, self.dir(project), relpath,
                                           store=self.spec.name, **policy)

    def read(self, project: str, relpath: str) -> str:
        path = self.abspath(project, self.resolve(project, relpath))
        if not path.is_file():
            from .errors import NoSuchItem

            raise NoSuchItem(self._missing(project, relpath))
        return path.read_text()

    def resolve(self, project: str, relpath: str) -> str:
        """`relpath`, or, when no such file exists but one does with this
        location's suffix (`morning-briefing` -> `morning-briefing.md`), that
        one: a ref named the way its tool names it still reads."""
        if self.abspath(project, relpath).exists():
            return relpath
        for ext in dict.fromkeys((self.suffix(project), ".md")):
            if not relpath.endswith(ext) and self.abspath(project, relpath + ext).is_file():
                return relpath + ext
        return relpath

    def _missing(self, project: str, relpath: str) -> str:
        """No such item: say so, with the nearest names this facet has."""
        import difflib

        from .filesync import scan

        noun = self.spec.facet or "note"
        loc = self.location(project)
        rels: list[str] = []
        try:
            root = loc.root if loc is not None and loc.prefix is None else self.root(project)
            if root.is_dir():
                rels = [r for r in scan(root, ".md", recursive=True)
                        if loc is None or loc.prefix is not None or loc.owns(r)]
        except CribUserError:
            pass
        stem = relpath.rsplit("/", 1)[-1].split(".", 1)[0]
        near = [r for r in rels if r.rsplit("/", 1)[-1].split(".", 1)[0] == stem]
        near += [r for r in difflib.get_close_matches(relpath, rels, n=3, cutoff=0.6)
                 if r not in near]
        hint = f" (did you mean {', '.join(near[:3])}?)" if near else ""
        return f"no {noun} {relpath!r} in project {project!r}{hint}"

    async def delete(self, project: str, relpath: str) -> dict[str, Any]:
        """Delete a note: stash current content to the version ring (keyed by id, so
        it's recoverable), unlink, and drop its chunks via index_file's missing-path
        path."""
        self._refuse_source(relpath, "delete")
        self._refuse_read_only(project, "delete")
        path = self.abspath(project, relpath)
        note_id: str | None = None
        if path.exists():
            note_id = self._stash_existing(project, relpath, path)
            path.unlink()
        res = await self.index.index_file(project, self.dir(project), relpath,
                                          store=self.spec.name, **self._policy(project))
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
        self._refuse_read_only(project, "move")
        self._refuse_read_only(dst_proj, "move into")
        self._refuse_unowned(dst_proj, dst_relpath, "move into")
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
                "id": note.id, **({"project_created": True} if created else {})}

    async def reindex(self, project: str, relpath: str | None = None,
                      full: bool = True) -> dict[str, Any]:
        """Reindex a note, or fully reconcile a project when relpath is None (walks
        the UNION of on-disk notes and indexed paths — catches offline edits AND drops
        orphaned chunks). All idempotent via the hash gate.

        `full=False` (the startup/post-pull reconcile) trusts the per-file stamps
        `index_file` records (crib.filesync): a note whose stamp hasn't moved is
        not re-read. Indexed paths gone from disk are always swept."""
        from .filesync import scan
        from .indexer import index_facet

        if (why := self.pending_move(project)) is not None:
            # not served until adopt moves its items: nothing to reconcile yet
            return {"project": project, "files": 0, "changed": 0, "removed": 0,
                    "skipped": [], "pending_move": why}
        nd = self.dir(project)
        loc = self.location(project)
        policy = self._policy(project)
        if relpath:
            targets = [relpath]
        else:
            now = scan(nd, ".md", recursive=True)
            if loc is not None:  # a mapping: only the files its glob picks
                now = {r: st for r, st in now.items() if loc.owns(r)}
            disk = set(now)
            # The indexed side is scoped to THIS pillar (absence rule: a chunk
            # indexed before the split has no `store` key and counts as notes) —
            # without the scope, the notes sweep would see every facet chunk as
            # "indexed but gone from disk" and drop it. Source-anchored docs
            # (`sources/<repo>/…`) live in the REPO, not the notes tree — the
            # sweep must NOT treat them as deleted (owned by index_docs_insitu +
            # the code watcher).
            indexed = {m.get("relpath")
                       for m in self.store.get_meta(
                           {"project": project, "kind": kind_of(self.spec.name)}).values()}
            targets = sorted(disk | {r for r in indexed if r})
            if not full:
                known = {f: (m, z) for f, (m, z, _i) in self.store.file_stamps(
                    index_facet(self.spec.name), project).items()}
                targets = [r for r in targets
                           if r not in disk or known.get(r) != now[r]]
        changed = removed = 0
        skipped: list[dict[str, str]] = []
        unstamped: list[str] = []
        by_id: dict[str, list[str]] = {}
        for rp in targets:
            # One unreadable note (bad frontmatter from a hand edit or conflict
            # markers, bad encoding, a permission problem) must never abort the
            # sweep — the startup reconcile covers every note in the project.
            # Skip it, name it in the result, keep going.
            try:
                res = await self.index.index_file(project, nd, rp,
                                                  store=self.spec.name, **policy)
            except (NoteParseError, UnicodeDecodeError, OSError) as e:
                skipped.append({"relpath": rp, "error": str(e)})
                continue
            if res.unstamped:
                unstamped.append(rp)
                continue
            if res.note_id:
                by_id.setdefault(res.note_id, []).append(rp)
            changed += int(res.changed)
            removed += res.deleted
        out: dict[str, Any] = {"project": project, "files": len(targets),
                               "changed": changed, "removed": removed,
                               "skipped": skipped}
        if unstamped:
            # read-only and id-stamped: these wait for a writable checkout to
            # stamp them (and commit), so their identity never flips
            out["unstamped"] = unstamped
        # A `move` interrupted between writing the destination and unlinking the
        # source leaves two notes sharing one id — and a shared id means a shared
        # version ring, so the two histories interleave silently. The sweep already
        # knows every note's id (index_file returns it), so detecting it is free:
        # REPORT it and let a human pick which copy survives (crib must never
        # delete a note on a heuristic).
        if not relpath and not full:
            # the sweep read only what changed: every note's id is in its chunks
            by_id = {}
            for m in self.store.get_meta(
                    {"project": project, "kind": kind_of(self.spec.name)}).values():
                if m.get("item_id") and m.get("relpath"):
                    by_id.setdefault(str(m["item_id"]), [])
                    if m["relpath"] not in by_id[str(m["item_id"])]:
                        by_id[str(m["item_id"])].append(str(m["relpath"]))
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
        if self._policy(project).get("ids") == "derived":
            return derived_ulid(project, relpath)  # the path's, never the file's
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
