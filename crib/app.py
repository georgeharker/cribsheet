"""Crib — the core service. Implements the tool verbs (DESIGN §5).

Both the MCP server and the CLI call into this; tests exercise it directly. All
writes go through `_write_note` so every mutation stashes a version and funnels
through the one hash-gated `index_file`.
"""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import notes
from .chunk import section_line_map
from .config import Config, CribLink, ProjectConfig, resolve_project
from .embed import build_embedder
from .gitbacking import GitBacking
from .indexer import IndexEngine, IndexResult
from .notes import Note
from .paths import Paths
from .store import InMemoryStore, Store
from .util import new_ulid
from .versions import VersionRing
from .watch import Watcher


def _slug(title: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_" else "" for c in title)
    return "-".join(keep.lower().split()) or "note"


@dataclass
class LookupHit:
    project: str
    relpath: str
    heading: str
    title: str
    snippet: str
    score: float
    line_start: int | None = None   # 1-based span of the section in the file,
    line_end: int | None = None     # resolved against current disk (None if gone)


class Crib:
    def __init__(self, paths: Paths, config: Config, store: Store) -> None:
        self.paths = paths
        self.config = config
        self.store = store
        self.embedder = build_embedder(config.embed)
        self.index = IndexEngine(store, self.embedder)
        self.git = GitBacking(paths.data_dir)
        self.versions = VersionRing(paths.versions_dir, config.versions_keep)
        self._watcher: Watcher | None = None
        self._on_close: Callable[[], None] | None = None

    # --- construction ------------------------------------------------------
    @classmethod
    def open(cls, store: Store | None = None) -> "Crib":
        paths = Paths.resolve().ensure()
        config = Config.load(paths.config_file)
        on_close: Callable[[], None] | None = None
        if store is None:
            store, on_close = _build_store(paths, config)
        crib = cls(paths, config, store)
        crib._on_close = on_close
        return crib

    # --- lifecycle ---------------------------------------------------------
    def start_watchers(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the file watcher on `loop` — the SAME loop the tools run on, so
        the per-path index locks coordinate writers and watcher (DESIGN §4)."""
        if self._watcher is not None:
            return
        self._watcher = Watcher(self.paths.projects_dir, self._on_fs_change, loop)
        self._watcher.start()

    async def _on_fs_change(self, project: str, relpath: str) -> None:
        await self.index.index_file(project, self.notes_dir(project), relpath)

    def stop_watchers(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def close(self) -> None:
        """Stop watchers and release the shared Chroma refcount, if any."""
        self.stop_watchers()
        if self._on_close is not None:
            self._on_close()
            self._on_close = None

    # --- helpers -----------------------------------------------------------
    def resolve_project(self, project: str | None, cwd: Path | None = None) -> str:
        return resolve_project(self.config, project, cwd)

    def notes_dir(self, project: str) -> Path:
        d = self.paths.notes_dir(project)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def abspath(self, project: str, relpath: str) -> Path:
        return self.notes_dir(project) / relpath

    async def _write_note(self, project: str, relpath: str, note: Note) -> IndexResult:
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
        return await self.index.index_file(project, self.notes_dir(project), relpath)

    # --- tool verbs --------------------------------------------------------
    async def store_note(self, content: str, title: str | None = None,
                         project: str | None = None, tags: list[str] | None = None,
                         cwd: Path | None = None) -> dict[str, Any]:
        proj = self.resolve_project(project, cwd)
        title = title or content.strip().splitlines()[0][:60] if content.strip() else "note"
        relpath = f"{_slug(title)}-{new_ulid()[-6:].lower()}.md"
        fm: dict[str, Any] = {"title": title, "source": "manual"}
        if tags:
            fm["tags"] = tags
        note = Note(path=self.abspath(proj, relpath), frontmatter=fm, body=content)
        res = await self._write_note(proj, relpath, note)
        return {"project": proj, "relpath": relpath, "indexed": res.upserted}

    async def append_note(self, relpath: str, content: str,
                          heading: str | None = None, project: str | None = None,
                          cwd: Path | None = None) -> dict[str, Any]:
        proj = self.resolve_project(project, cwd)
        path = self.abspath(proj, relpath)
        note = notes.load(path) if path.exists() else Note(
            path=path, frontmatter={"source": "appended"}, body="")
        block = f"\n\n## {heading}\n{content}" if heading else f"\n\n{content}"
        note.body = note.body.rstrip() + block
        res = await self._write_note(proj, relpath, note)
        return {"project": proj, "relpath": relpath, "indexed": res.upserted}

    async def edit_note(self, relpath: str, new_content: str,
                        project: str | None = None, cwd: Path | None = None) -> dict[str, Any]:
        """Replace raw file content (frontmatter preserved if present in input)."""
        proj = self.resolve_project(project, cwd)
        fm, body = notes.parse(new_content)
        path = self.abspath(proj, relpath)
        if not fm and path.exists():
            fm = notes.load(path).frontmatter   # keep existing frontmatter
        note = Note(path=path, frontmatter=fm, body=body)
        res = await self._write_note(proj, relpath, note)
        return {"project": proj, "relpath": relpath, "indexed": res.upserted}

    async def forget(self, relpath: str, project: str | None = None,
                     cwd: Path | None = None) -> dict[str, Any]:
        """Delete a note: remove it from disk and drop its chunks from the index.

        The current content is stashed to the version ring first (keyed by note
        id), so a forgotten note's bytes survive and can be recovered. Deletion
        of the index entry happens through the same `index_file` — once the file
        is gone, it sees a missing path and drops all chunks for that relpath."""
        proj = self.resolve_project(project, cwd)
        path = self.abspath(proj, relpath)
        note_id = None
        if path.exists():
            note = notes.load(path)
            note_id = note.id
            if note_id:
                self.versions.stash(
                    note_id, notes.serialize(note.frontmatter, note.body))
            path.unlink()
        res = await self.index.index_file(proj, self.notes_dir(proj), relpath)
        return {"project": proj, "relpath": relpath, "removed": res.deleted,
                "recoverable_id": note_id}

    def read_note(self, relpath: str, project: str | None = None,
                  cwd: Path | None = None) -> str:
        proj = self.resolve_project(project, cwd)
        return self.abspath(proj, relpath).read_text()

    def locate(self, relpath: str, project: str | None = None,
               cwd: Path | None = None) -> str:
        proj = self.resolve_project(project, cwd)
        return str(self.abspath(proj, relpath))

    async def reindex(self, relpath: str | None = None, project: str | None = None,
                      cwd: Path | None = None) -> dict[str, Any]:
        """Reindex a note, or fully reconcile a project when relpath is None.

        Full reconcile walks the UNION of on-disk notes and indexed paths, so it
        catches files added/edited while crib was down AND drops orphaned chunks
        for notes deleted off disk. All idempotent via the hash gate (§4)."""
        proj = self.resolve_project(project, cwd)
        nd = self.notes_dir(proj)
        if relpath:
            targets = [relpath]
        else:
            disk = {str(p.relative_to(nd)) for p in nd.rglob("*.md")}
            indexed = {m.get("relpath")
                       for m in self.store.get_meta({"project": proj}).values()}
            targets = sorted(disk | {r for r in indexed if r})
        changed = removed = 0
        for rp in targets:
            res = await self.index.index_file(proj, nd, rp)
            changed += int(res.changed)
            removed += res.deleted
        return {"project": proj, "files": len(targets),
                "changed": changed, "removed": removed}

    async def reconcile_all(self) -> dict[str, Any]:
        """Startup sweep across every project — catch up on offline changes."""
        projects = sorted(set(self.projects()) | self._indexed_projects())
        total = {"projects": len(projects), "changed": 0, "removed": 0}
        for proj in projects:
            r = await self.reindex(project=proj)
            total["changed"] += r["changed"]
            total["removed"] += r["removed"]
        return total

    def _indexed_projects(self) -> set[str]:
        out: set[str] = set()
        for m in self.store.get_meta({}).values():
            if p := m.get("project"):
                out.add(p)
        return out

    def lookup(self, query: str, project: str | None = None, k: int = 8,
               tags: list[str] | None = None, dedupe_by_file: bool = True,
               min_score: float = 0.0, cwd: Path | None = None) -> list[LookupHit]:
        proj = self.resolve_project(project, cwd)
        vec = self.embedder.embed([query])[0]
        where: dict[str, Any] = {"project": proj}
        raw = self.store.query(vec, k=k * 3 if dedupe_by_file else k, where=where)
        hits, seen = [], set()
        line_maps: dict[str, dict[str, tuple[int, int]]] = {}
        for h in raw:
            if h.score <= min_score:        # drop orthogonal / irrelevant matches
                continue
            rp = h.metadata.get("relpath", "")
            if dedupe_by_file and rp in seen:
                continue
            seen.add(rp)
            if tags and not (set(tags) & set(
                    filter(None, (h.metadata.get("tags") or "").split(",")))):
                continue
            heading = h.metadata.get("heading_path", "")
            if rp not in line_maps:         # read each file once, current on disk
                try:
                    line_maps[rp] = section_line_map(self.abspath(proj, rp).read_text())
                except OSError:
                    line_maps[rp] = {}
            span = line_maps[rp].get(heading)
            hits.append(LookupHit(
                project=proj, relpath=rp,
                heading=heading,
                title=h.metadata.get("title", ""),
                snippet=h.document[:280],
                score=round(h.score, 4),
                line_start=span[0] if span else None,
                line_end=span[1] if span else None,
            ))
            if len(hits) >= k:
                break
        return hits

    # --- import ------------------------------------------------------------
    async def import_docs(self, project: str | None = None,
                          cwd: Path | None = None) -> dict[str, Any]:
        """Ingest local docs declared in a code repo's `.crib` (DESIGN §6).

        One-way pull: copy matched files into the project under `import_into`,
        stamp provenance frontmatter, index. Source wins on re-import; the target
        note id (and thus version-ring history) is preserved across re-pulls.
        """
        link = CribLink.find(cwd or Path.cwd())
        if link is None or link.root is None:
            raise ValueError("no .crib found from cwd upward")
        proj = project or link.project
        into = link.import_into or f"imported/{link.root.name}/"
        if not into.endswith("/"):
            into += "/"
        today = datetime.date.today().isoformat()

        imported: list[str] = []
        for pattern in link.imports:
            for src in sorted(link.root.glob(pattern)):
                if not src.is_file():
                    continue
                rel_to_repo = src.relative_to(link.root)
                relpath = f"{into}{rel_to_repo.as_posix()}"
                sfm, sbody = notes.parse(src.read_text())
                fm = dict(sfm)
                fm.update({
                    "source": "imported",
                    "source_repo": str(link.root),
                    "source_path": rel_to_repo.as_posix(),
                    "imported": today,
                })
                tgt = self.abspath(proj, relpath)
                if tgt.exists() and (ex := notes.load(tgt)).id:
                    fm = {"id": ex.id, **fm}   # keep identity across re-imports
                note = Note(path=tgt, frontmatter=fm, body=sbody)
                await self._write_note(proj, relpath, note)
                imported.append(relpath)
        return {"project": proj, "imported": len(imported), "files": imported}

    # --- versioning / git --------------------------------------------------
    def list_versions(self, relpath: str, project: str | None = None,
                      cwd: Path | None = None) -> list[dict[str, Any]]:
        proj = self.resolve_project(project, cwd)
        note = notes.load(self.abspath(proj, relpath))
        if not note.id:
            return []
        return [{"version": e.name, "seq": e.seq, "mtime": e.mtime}
                for e in self.versions.list(note.id)]

    async def restore(self, relpath: str, version: str, project: str | None = None,
                      cwd: Path | None = None) -> dict[str, Any]:
        proj = self.resolve_project(project, cwd)
        note = notes.load(self.abspath(proj, relpath))
        if not note.id:
            raise ValueError("note has no id; nothing to restore")
        content = self.versions.read(note.id, version)
        return await self.edit_note(relpath, content, project=proj)

    def snapshot(self, message: str | None = None) -> str:
        return self.git.snapshot(message)

    def history(self, relpath: str | None = None) -> list[str]:
        return self.git.history(relpath)

    def projects(self) -> list[str]:
        pd = self.paths.projects_dir
        if not pd.is_dir():
            return []
        return sorted(p.name for p in pd.iterdir() if p.is_dir())

    def project_config(self, project: str) -> ProjectConfig:
        return ProjectConfig.load(
            self.paths.project_dir(project) / ".cribproject", project)


def _build_store(paths: Paths, config: Config) -> tuple[Store, Callable[[], None] | None]:
    """Return (store, on_close). on_close releases the shared Chroma refcount."""
    from .store import JsonStore

    mode = config.chroma.mode
    if mode == "shared":
        return _build_shared_chroma(paths, config)
    if mode == "embedded":
        try:
            from .store import ChromaStore
            return ChromaStore.embedded(str(paths.chroma_dir)), None
        except ImportError:
            pass  # fall through to the dependency-free persistent store
    # mode == "json", or embedded requested but chromadb unavailable
    return JsonStore(paths.index_dir / "store.json"), None


def _build_shared_chroma(paths: Paths, config: Config) -> tuple[Store, Callable[[], None]]:
    """Refcount a `chroma run` via sharedserver, wait for it, then connect.

    `sharedserver use` is reuse-or-start by default: if `<server_name>` is already
    running it just attaches and increments the refcount; it only launches a new
    `chroma run` when none exists. crib releases its refcount on close."""
    from . import sharedserver
    from .store import ChromaStore

    c = config.chroma
    paths.chroma_dir.mkdir(parents=True, exist_ok=True)
    command = ["chroma", "run", "--path", str(paths.chroma_dir),
               "--host", c.host, "--port", str(c.port)]
    sharedserver.use(c.server_name, command, c.grace_period)

    def probe() -> None:
        import chromadb  # lazy
        chromadb.HttpClient(host=c.host, port=c.port).heartbeat()

    try:
        sharedserver.wait_ready(probe)
        store = ChromaStore.shared(c.host, c.port)
    except Exception:
        sharedserver.unuse(c.server_name)   # don't leak the refcount on failure
        raise
    return store, (lambda: sharedserver.unuse(c.server_name))
