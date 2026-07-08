"""Crib — the core service. Implements the tool verbs (DESIGN §5).

Both the MCP server and the CLI call into this; tests exercise it directly. All
writes go through `_write_note` so every mutation stashes a version and funnels
through the one hash-gated `index_file`.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import re
import sys
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .codeindex import RefProjects

from . import claudemem, notes
from .chunk import section_line_map
from .claudemem import MemoryBindings
from .codestore import CodeStore, _ResidentCode
from .config import (Config, CribLink, ProjectConfig, portable_path,
                     resolve_project)
from .embed import build_embedder
from .gitbacking import GitBacking
from .indexer import IndexEngine, IndexResult
from .notes import Note
from .paths import Paths
from .sources import SRC_PREFIX, SourceRoots, src_relpath
from .store import Hit, InMemoryStore, Store
from .util import derived_ulid
from .versions import VersionRing
from .watch import CodeWatcher, Watcher


def _slug(title: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_" else "" for c in title)
    return "-".join(keep.lower().split()) or "note"


# Built-in distill instruction (knowledge-capture §4); a project's `.cribproject`
# `distill_prompt` overrides it.
DEFAULT_DISTILL_PROMPT = (
    "Revise this note to be tighter and cleaner while preserving meaning. "
    "Compress verbose prose, merge duplicated points, normalize structure. "
    "KEEP every fact, decision, gotcha, and API detail; DROP deliberation, "
    "hedging, and restated context. Preserve code blocks and commands VERBATIM. "
    "Return ONLY the revised note body in markdown — no preamble, no fences."
)


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


def _resolve_embed_config(config: Config) -> Any:
    """Pick the embed model by *active profile*, reusing the `models.toml`
    profiles the generation layer already uses (`select(profile, "embed")`). The
    profile is chosen **externally** — `$CRIB_PROFILE` (a per-host picker, the
    Python analog of zsh-ai's per-host `zstyle ':zsh-ai:*' profile`), falling back
    to `[generate].profile`. A capable box picks a profile whose `embed` names a
    bigger model; a Pi sets nothing and keeps `[embed].model`. No model name lives
    outside the config — only the profile does. Falls back to `[embed]` on any
    miss (no profile, no `embed` key, unreadable config)."""
    profile = os.environ.get("CRIB_PROFILE") or config.generate.profile
    if profile and config.generate.config:
        try:
            from llmkit.bridge import load
            conf = load(str(Path(config.generate.config).expanduser()))
            spec = conf.select(profile, "embed")
            if spec:
                return replace(config.embed, model=spec)
        except Exception:  # noqa: BLE001 — profile embed is best-effort; fall back
            pass
    return config.embed


class Crib:
    def __init__(self, paths: Paths, config: Config, store: Store) -> None:
        self.paths = paths
        self.config = config
        self.store = store
        self.embedder = build_embedder(_resolve_embed_config(config))
        self.index = IndexEngine(store, self.embedder,
                                 config.chunk.window_words,
                                 config.chunk.overlap_words,
                                 keyword_terms=self._keyword_terms,
                                 summary_terms=self._summary_terms)
        self.git = GitBacking(paths.data_dir)
        self.versions = VersionRing(paths.versions_dir, config.versions_keep)
        self.memory_bindings = MemoryBindings(paths.data_dir / "memory-bindings.json")
        self._reranker: Any = None      # lazy cross-encoder, warm for the daemon
        self._watcher: Watcher | None = None
        self._code_watcher: CodeWatcher | None = None
        self._mirror: Any = None        # MemoryMirror, started by the daemon
        self._on_close: Callable[[], None] | None = None
        # The code subsystem's shared per-project state (resident cache, freshness
        # epoch, write locks, in-flight/sweep tracking) — owned by CodeStore; the
        # methods below reach through `self.code` (codestore.py).
        self.code = CodeStore(paths, config)

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
        # Code watcher — reindexes source on edit (notes-watcher reloads notes,
        # code-watcher reindexes code). Seed it with every code-indexed project's root.
        self._code_watcher = CodeWatcher(self._on_code_change, loop)
        from .codeindex import SymbolIndex
        for p in self.projects():
            name = p["project"] if isinstance(p, dict) else p
            root = SymbolIndex(self.paths.project_dir(name)).source_root()
            if root is not None:
                self._code_watcher.watch_root(name, root)
        self._code_watcher.start()

    async def _on_fs_change(self, project: str, relpath: str) -> None:
        await self.index.index_file(project, self.notes_dir(project), relpath)

    async def _on_code_change(
            self, project: str, changes: dict[str, tuple[str, bool]]) -> None:
        """Reindex (or drop) the source files the watcher coalesced for a project —
        eager counterpart to the lazy query-time revalidation. Off the loop;
        best-effort (a transient syntax error mid-edit just leaves the prior entry
        until the next save). A batch too large to reindex file-by-file (a branch
        switch) collapses to a single revalidation sweep."""
        from .codeindex import _POOL
        from .watch import CODE_BATCH_FALLBACK
        # Pump the same events into the warm LSP sessions (docs §3.2) BEFORE
        # reindexing, so a server that doesn't self-watch the fs invalidates its
        # workspace index — cross-file edges then resolve against current code.
        by_root: dict[str, list[tuple[str, int]]] = {}
        for relpath, (root, deleted) in changes.items():
            if not relpath.startswith("\x00doc\x00"):
                by_root.setdefault(root, []).append((relpath, 3 if deleted else 2))
        for root, events in by_root.items():
            _POOL.notify_changes(Path(root), events)
        if len(changes) > CODE_BATCH_FALLBACK:
            try:
                await asyncio.to_thread(self._revalidate, project)
            except Exception:  # noqa: BLE001 — never let a watcher event crash the loop
                pass
            return
        for relpath, (root, deleted) in changes.items():
            try:
                if relpath.startswith("\x00doc\x00"):     # in-situ doc (see CodeWatcher._decode)
                    # index_file is async (asyncio locks) — await on THIS loop, don't
                    # thread it (a per-thread asyncio.run would strand stale-loop locks).
                    rel_to_repo = relpath[len("\x00doc\x00"):]
                    src_rel = src_relpath(Path(root).name, rel_to_repo)
                    await self.index.index_file(
                        project, self.notes_dir(project), src_rel,
                        content_path=self.abspath(project, src_rel))
                elif deleted and not (Path(root) / relpath).exists():
                    await asyncio.to_thread(self._drop_file, project, relpath)
                else:
                    # NB: a `deleted` flag for a file that EXISTS is a spurious
                    # delete — macOS FSEvents coalesces a rename-style save into
                    # flag bundles that watchdog re-expands in arbitrary order,
                    # and the batch's last-event-wins can land on the delete.
                    # Trusting it evicted whole files' symbols; the file's actual
                    # state at dispatch time (post-debounce) is the truth.
                    await asyncio.to_thread(
                        self._index_code_file_tracked, Path(root), relpath, project, True)
            except Exception:  # noqa: BLE001 — one bad file never aborts the batch
                pass

    def _register_code_root(self, project: str, root: str | Path) -> None:
        """Watch a repo's source root as soon as it's indexed (so a mid-session
        onboard starts live-updating immediately)."""
        if self._code_watcher is not None:
            self._code_watcher.watch_root(project, root)

    async def start_memory_mirror(self, loop: asyncio.AbstractEventLoop) -> None:
        """Catch up + live-mirror bound Claude harness memory dirs (DESIGN §13).
        No-op without bindings (`crib import-memory` opts repos in)."""
        if self._mirror is not None or not self.config.memory.watch:
            return
        from .memmirror import MemoryMirror

        async def sync(root: Path, project: str) -> Any:
            return await self.import_claude_memory(project=project, root=root)

        self._mirror = MemoryMirror(self.memory_bindings, sync, loop)
        await self._mirror.catch_up()
        self._mirror.start()

    def stop_watchers(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        if self._code_watcher is not None:
            self._code_watcher.stop()
            self._code_watcher = None
        if self._mirror is not None:
            self._mirror.stop()
            self._mirror = None

    def close(self) -> None:
        """Stop watchers, shut warm LSP sessions down, and release the shared
        Chroma refcount, if any."""
        self.stop_watchers()
        from .codeindex import _POOL
        _POOL.close_all()
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

    def _keyword_terms(self, project: str, section_hash: str,
                       labels: tuple[str, ...]) -> list[str]:
        """Per-section keyword_index terms for the given labels — the BM25 feed
        (§3.1). Read from the section-addressed TOML store; empty when a section
        has no keyword set for a label yet (graceful: BM25 falls back to
        body+heading)."""
        from .section_index import SectionIndex
        return SectionIndex(self.paths.project_dir(project), "keyword_index") \
            .terms_for(section_hash, list(labels))

    def _summary_terms(self, project: str, section_hash: str,
                       labels: tuple[str, ...]) -> list[str]:
        """Per-section summary_index rephrasings for the given labels — the dense
        alias feed (§3). Read from the section-addressed TOML store; empty when a
        section has no summary for a label yet."""
        from .section_index import SectionIndex
        return SectionIndex(self.paths.project_dir(project), "summary_index") \
            .terms_for(section_hash, list(labels))

    def _source_roots(self, project: str) -> "SourceRoots":
        """Per-project registry of docs indexed in-situ (prefix -> repo root)."""
        from .sources import SourceRoots
        return SourceRoots(self.paths.project_dir(project) / "doc-sources.json")

    def abspath(self, project: str, relpath: str) -> Path:
        """On-disk file for a note. Source-anchored docs (`sources/<repo>/…`)
        resolve to the repo file via the registry; everything else lives under the
        crib notes tree."""
        if relpath.startswith(SRC_PREFIX):
            src = self._source_roots(project).resolve(relpath)
            if src is not None:
                return src
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
    # near-duplicate nudge: a stored note whose probe matches an existing note
    # this closely is flagged in the result (the descriptions preach append/edit
    # over duplicating; this is what detects it).
    DEDUPE_WARN_SCORE = 0.85

    def project_is_new(self, proj: str) -> bool:
        """True if `proj` has no notes dir yet (a write would create it)."""
        return not self.paths.notes_dir(proj).exists()

    def _unique_relpath(self, proj: str, slug: str) -> str:
        """`<slug>.md`, with a numeric suffix only on collision — predictable and
        hand-referenceable. Stable identity is the frontmatter `id`, not the path."""
        base = self.notes_dir(proj)
        if not (base / f"{slug}.md").exists():
            return f"{slug}.md"
        i = 2
        while (base / f"{slug}-{i}.md").exists():
            i += 1
        return f"{slug}-{i}.md"

    def _similar(self, proj: str, content: str, exclude: str) -> list[dict[str, Any]]:
        """Near-duplicate hints for a just-stored note (excludes the note itself)."""
        probe = content.strip().splitlines()[0][:200] if content.strip() else ""
        if not probe:
            return []
        try:
            hits = self.lookup(probe, project=proj, k=4)
        except Exception:  # noqa: BLE001 — a nudge must never fail the store
            return []
        return [{"relpath": h.relpath, "heading": h.heading, "score": h.score}
                for h in hits
                if h.relpath != exclude and h.score >= self.DEDUPE_WARN_SCORE]

    async def store_note(self, content: str, title: str | None = None,
                         project: str | None = None, tags: list[str] | None = None,
                         cwd: Path | None = None) -> dict[str, Any]:
        proj = self.resolve_project(project, cwd)
        created = self.project_is_new(proj)
        title = title or content.strip().splitlines()[0][:60] if content.strip() else "note"
        relpath = self._unique_relpath(proj, _slug(title))
        fm: dict[str, Any] = {"title": title, "source": "manual"}
        if tags:
            fm["tags"] = tags
        note = Note(path=self.abspath(proj, relpath), frontmatter=fm, body=content)
        res = await self._write_note(proj, relpath, note)
        return {"project": proj, "relpath": relpath, "indexed": res.upserted,
                "created": created, "similar": self._similar(proj, content, relpath)}

    async def move_note(self, relpath: str, to_project: str | None = None,
                        to_relpath: str | None = None, project: str | None = None,
                        cwd: Path | None = None) -> dict[str, Any]:
        """Relocate a note across projects and/or rename it, preserving its `id`
        (and thus version-ring history). One-way: write destination, drop source."""
        src_proj = self.resolve_project(project, cwd)
        dst_proj = to_project or src_proj
        dst_relpath = to_relpath or relpath
        src = self.abspath(src_proj, relpath)
        if not src.exists():
            raise ValueError(f"no such note: {relpath} in project {src_proj!r}")
        if src_proj == dst_proj and dst_relpath == relpath:
            raise ValueError("source and destination are the same")
        # capture BEFORE any abspath(dst_proj) call — abspath mkdir's the notes dir
        created = self.project_is_new(dst_proj)
        if self.abspath(dst_proj, dst_relpath).exists():
            raise ValueError(f"destination exists: {dst_relpath} in {dst_proj!r}")
        note = notes.load(src)              # carries the id in frontmatter
        dst = Note(path=self.abspath(dst_proj, dst_relpath),
                   frontmatter=note.frontmatter, body=note.body)
        notes.save_atomic(dst)
        await self.index.index_file(dst_proj, self.notes_dir(dst_proj), dst_relpath)
        src.unlink()                        # drop source + its chunks
        await self.index.index_file(src_proj, self.notes_dir(src_proj), relpath)
        return {"from": {"project": src_proj, "relpath": relpath},
                "to": {"project": dst_proj, "relpath": dst_relpath},
                "id": note.id, "created": created}

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
            # Full reindex is the one safe place to switch embedder: if the stored
            # vectors' dim differs from the current embedder (e.g. a profile flip
            # to a bigger model), recreate the collection so all chunks re-embed at
            # the new dim. Chroma is shared across projects, so this wipes them all
            # — hence full-reindex-only; a --all sweep re-embeds the rest.
            cur = self.store.current_dim()
            if cur is not None and cur != self.embedder.dim:
                print(f"crib: embedder dim {cur}→{self.embedder.dim}; recreating "
                      f"the vector collection (full re-embed)", file=sys.stderr)
                self.store.recreate()
            disk = {str(p.relative_to(nd)) for p in nd.rglob("*.md")}
            # Source-anchored docs (`sources/<repo>/…`) live in the REPO, not under
            # the notes tree — the notes reconcile's on-disk sweep must NOT treat them
            # as deleted (that wiped in-situ doc chunks). They're owned by
            # index_docs_insitu + the code watcher.
            indexed = {m.get("relpath")
                       for m in self.store.get_meta({"project": proj}).values()
                       if not (m.get("relpath") or "").startswith(SRC_PREFIX)}
            targets = sorted(disk | {r for r in indexed if r})
        changed = removed = 0
        for rp in targets:
            res = await self.index.index_file(proj, nd, rp)
            changed += int(res.changed)
            removed += res.deleted
        return {"project": proj, "files": len(targets),
                "changed": changed, "removed": removed}

    async def reconcile_all(self) -> dict[str, Any]:
        """Startup/post-pull sweep across every project — catch up on offline
        note changes, then eagerly rebuild merge-dirtied code files (a pull
        that merged divergent symbol records lands here via the CLI's
        post-pull reconcile)."""
        projects = sorted(set(self.projects()) | self._indexed_projects())
        total: dict[str, Any] = {"projects": len(projects), "changed": 0, "removed": 0}
        for proj in projects:
            r = await self.reindex(project=proj)
            total["changed"] += r["changed"]
            total["removed"] += r["removed"]
        dirty = await self._reindex_dirty_code()
        if dirty:
            total["code_files_rebuilt"] = dirty
        return total

    async def _reindex_dirty_code(self) -> dict[str, int]:
        """Rebuild every code file carrying a merge-dirtied symbol (blank
        `content_hash`, written by the sync merge driver on divergent code
        states) — CONCURRENTLY, bounded by [generate].concurrency, so the
        post-pull reconcile pays this cost in parallel instead of the first
        code query absorbing it serially in `_revalidate` (which remains the
        lazy backstop for pulls done outside crib). Best-effort per file.
        → {project: files rebuilt} for the projects that had any."""
        from .codeindex import SymbolIndex
        sem = asyncio.Semaphore(max(1, self.config.generate.concurrency))
        out: dict[str, int] = {}
        for proj in self.projects():
            store = SymbolIndex(self.paths.project_dir(proj))
            if not store.is_populated():
                continue
            root = store.source_root()
            if root is None or not root.exists():
                continue                 # index synced from another machine — lazy path
            src_root: Path = root        # narrowed rebind (mypy: closure default)
            files = sorted({e["file"] for e in store.all()
                            if e.get("file") and not e.get("content_hash")})
            if not files:
                continue

            async def _one(rel: str, proj: str = proj, root: Path = src_root) -> bool:
                async with sem:
                    try:
                        await asyncio.to_thread(
                            self._index_code_file_tracked, root, rel, proj, True)
                        return True
                    except Exception:  # noqa: BLE001 — the lazy gate backstops
                        return False
            done = await asyncio.gather(*(_one(f) for f in files))
            out[proj] = sum(done)
        return out

    def _indexed_projects(self) -> set[str]:
        out: set[str] = set()
        for m in self.store.get_meta({}).values():
            if p := m.get("project"):
                out.add(p)
        return out

    @property
    def reranker(self) -> Any:
        """Lazy reranker, built once and kept warm (daemon-resident)."""
        if self._reranker is None:
            from .retrieve import build_reranker
            self._reranker = build_reranker(self.config.retrieve.rerank_model)
        return self._reranker

    def _rerank(self, query: str, hits: list[Hit]) -> list[Hit]:
        """Blend the cross-encoder into the ranking by RRF-fusing its order with
        the existing fused order, rather than letting it fully reorder — so the
        reranker is a third voter that can *promote* a better match but can't
        single-handedly *break* a strong hybrid result on one bad judgment. Only
        the top `rerank_top_n` are scored. Degrades to input order if unavailable."""
        n = self.config.retrieve.rerank_top_n
        head = hits[:n]
        if not head:
            return hits
        try:
            scores = self.reranker.scores(query, [h.document for h in head])
        except Exception as e:  # noqa: BLE001 — reranker optional; degrade to fused order
            print(f"[crib] reranker disabled: {e}", file=sys.stderr)
            return hits
        from .retrieve import reciprocal_rank_fusion

        rerank_order = [head[i].id for i in sorted(
            range(len(head)), key=lambda i: scores[i], reverse=True)]
        fused_order = [h.id for h in hits]   # existing dense⊕BM25 RRF order
        new_order = reciprocal_rank_fusion(
            [fused_order, rerank_order], k=self.config.retrieve.rrf_k)
        by_id = {h.id: h for h in hits}
        return [by_id[i] for i in new_order]

    def _retrieve(self, proj: str, query: str, vec: list[float], topn: int,
                  hybrid: bool, rerank: bool,
                  keyword_labels: tuple[str, ...] = (),
                  keyword_weight: float = 1.0,
                  summary_labels: tuple[str, ...] = (),
                  summary_weight: float = 0.3) -> list[Hit]:
        """Candidate Hits in rank order: dense retrieval, optionally RRF-fused with
        a BM25 (keyword_index) ranking and/or a summary_index alias-vector ranking,
        then optionally cross-encoder reranked.

        Three independent recall signals, fused by rank: dense cosine (paraphrase),
        BM25 (exact terms + keyword_index), and summary aliases (dense match on LLM
        rephrasings — bridges the pure-paraphrase gap the section body misses).
        Hits keep the cosine in `.score`; an id absent from the dense finalists has
        its cosine filled by re-embedding just that handful."""
        from .retrieve import reciprocal_rank_fusion, tokenize

        where = {"project": proj}
        dense = self.store.query(vec, k=topn, where=where)
        rankings: list[list[str]] = [[h.id for h in dense]]
        weights: list[float] = [1.0]            # dense list votes at full weight
        docs: dict = {}

        if hybrid:
            ids, docs, bm25 = self.index.lexical.get(
                proj, keyword_labels, keyword_weight)
            if ids:
                sparse = bm25.scores(tokenize(query))
                rankings.append([ids[j] for j in sorted(
                    range(len(ids)), key=lambda j: sparse[j], reverse=True)
                    if sparse[j] > 0][:topn])
                weights.append(1.0)             # BM25 (keyword downweight is in-corpus)

        if summary_labels:
            summary_ranked = self.index.summaries.ranking(
                proj, summary_labels, vec, topn)
            if summary_ranked:
                rankings.append(summary_ranked)
                weights.append(summary_weight)  # broad aliases vote below primaries

        if len(rankings) == 1:          # dense only
            out = dense
        else:
            fused = reciprocal_rank_fusion(
                rankings, k=self.config.retrieve.rrf_k, weights=weights)[:topn]
            dense_by = {h.id: h for h in dense}
            if not docs:                # need doc text to fill non-dense cosines
                docs = {i: (d, m) for i, (d, m)
                        in self.store.get_docs(where).items()
                        if not (m or {}).get("alias")}
            missing = [cid for cid in fused if cid not in dense_by and cid in docs]
            cos: dict[str, float] = {}
            if missing:
                for cid, dv in zip(missing, self.embedder.embed(
                        [docs[cid][0] for cid in missing])):
                    cos[cid] = sum(a * b for a, b in zip(vec, dv))  # L2-normalized
            out = []
            for cid in fused:
                if cid in dense_by:
                    out.append(dense_by[cid])
                elif cid in docs:
                    doc, meta = docs[cid]
                    out.append(Hit(cid, doc, meta, round(cos.get(cid, 0.0), 4)))
        if rerank:
            out = self._rerank(query, out)
        return out

    def lookup(self, query: str, project: str | None = None, k: int = 8,
               tags: list[str] | None = None, dedupe: str = "section",
               min_score: float = 0.0, cwd: Path | None = None,
               hybrid: bool | None = None, rerank: bool | None = None,
               keyword_labels: list[str] | None = None,
               keyword_weight: float | None = None,
               summary_labels: list[str] | None = None,
               summary_weight: float | None = None
               ) -> list[LookupHit]:
        """Ranked sections matching `query`.

        `dedupe` collapses duplicates: "section" (default) keeps one hit per
        distinct heading — so a note's several relevant sections all surface, and
        only repeated windows of the *same* section merge; "file" keeps one hit
        per note (breadth across notes, hides a note's other sections); "none"
        keeps every chunk. Section is right for retrieval; file suits a
        what-notes-are-relevant overview.
        """
        proj = self.resolve_project(project, cwd)
        vec = self.embedder.embed_query([query])[0]
        use_hybrid = self.config.retrieve.hybrid if hybrid is None else hybrid
        use_rerank = self.config.retrieve.rerank if rerank is None else rerank
        kw_labels = tuple(self.config.retrieve.keyword_labels
                          if keyword_labels is None else keyword_labels)
        kw_weight = (self.config.retrieve.keyword_weight
                     if keyword_weight is None else keyword_weight)
        sum_labels = tuple(self.config.retrieve.summary_labels
                           if summary_labels is None else summary_labels)
        sum_weight = (self.config.retrieve.summary_weight
                      if summary_weight is None else summary_weight)
        # Hybrid pulls a wider candidate pool so BM25 can promote terms dense ranked low.
        topn = max(k * 3, 30) if use_hybrid else (k if dedupe == "none" else k * 3)
        raw = self._retrieve(proj, query, vec, topn, use_hybrid, use_rerank,
                             kw_labels, kw_weight, sum_labels, sum_weight)
        hits, seen = [], set()
        line_maps: dict[str, dict[str, tuple[int, int]]] = {}
        for h in raw:
            if h.score <= min_score:        # drop orthogonal / irrelevant matches
                continue
            if tags and not (set(tags) & set(
                    filter(None, (h.metadata.get("tags") or "").split(",")))):
                continue
            rp = h.metadata.get("relpath", "")
            heading = h.metadata.get("heading_path", "")
            key = rp if dedupe == "file" else (rp, heading)
            if dedupe != "none" and key in seen:
                continue
            seen.add(key)
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

    def apropos(self, query: str, project: str | None = None, k: int = 8,
                tags: list[str] | None = None,
                cwd: Path | None = None) -> list[dict[str, Any]]:
        """`lookup` (same section-level dedupe) but each hit carries the FULL
        matching section markdown — sliced from the file by its line span — rather
        than a 280-char snippet, for rendering the matches for a human to read."""
        proj = self.resolve_project(project, cwd)
        out: list[dict[str, Any]] = []
        for h in self.lookup(query, project, k, tags, dedupe="section", cwd=cwd):
            section = h.snippet
            if h.line_start and h.line_end:
                try:
                    lines = self.abspath(proj, h.relpath).read_text().splitlines()
                    section = "\n".join(lines[h.line_start - 1:h.line_end])
                except OSError:
                    pass
            out.append({**vars(h), "section": section})
        return out

    # --- generation: distill + elaborate (knowledge-capture §2/§4, §3.1) ---
    async def distill(self, relpath: str, project: str | None = None,
                      cwd: Path | None = None) -> dict[str, Any]:
        """Revise a note in place via the LLM: compress, dedupe, normalize; keep
        facts/decisions, drop deliberation, preserve code verbatim. Thrash-guarded
        (no write if the body is unchanged), marked `source: distilled`, written
        through the version ring so a bad revision is a cheap rollback."""
        proj = self.resolve_project(project, cwd)
        path = self.abspath(proj, relpath)
        if not path.exists():
            raise ValueError(f"no such note: {relpath} in project {proj!r}")
        note = notes.load(path)
        prompt = self.project_config(proj).distill_prompt or DEFAULT_DISTILL_PROMPT
        from .generate import agenerate
        new_body = (await agenerate(
            self.config.generate, prompt, note.body, purpose="distill",
            timeout=self.config.generate.timeout)).strip()
        if not new_body or new_body == note.body.strip():
            return {"project": proj, "relpath": relpath, "changed": False}
        note.frontmatter["source"] = "distilled"
        note.body = new_body
        res = await self._write_note(proj, relpath, note)
        return {"project": proj, "relpath": relpath, "changed": True,
                "indexed": res.upserted}

    async def elaborate(self, label: str, relpath: str | None = None,
                        project: str | None = None, cwd: Path | None = None,
                        overwrite: bool = False) -> dict[str, Any]:
        """keyword_index: generate search terms per section for BM25 (§3.1).
        `crib elaborate <label>`; activate via `[retrieve].keyword_labels`."""
        from .section_index import KEYWORD_PROMPTS, resolve_prompt
        prompt = resolve_prompt(label, self.config.elaborate, KEYWORD_PROMPTS)
        return await self._generate_index(
            "keyword_index", "elaborate", label, prompt, relpath, project, cwd,
            overwrite)

    async def summarize(self, label: str, relpath: str | None = None,
                        project: str | None = None, cwd: Path | None = None,
                        overwrite: bool = False) -> dict[str, Any]:
        """summary_index: generate LLM rephrasings per section, embedded as dense
        alias vectors (§3). `crib summarize <label>`; activate via
        `[retrieve].summary_labels`."""
        from .section_index import SUMMARY_PROMPTS, resolve_prompt
        prompt = resolve_prompt(label, self.config.summarize, SUMMARY_PROMPTS)
        return await self._generate_index(
            "summary_index", "summarize", label, prompt, relpath, project, cwd,
            overwrite)

    # --- code symbol index (docs/code-symbol-index.md) --------------------
    async def code_index(self, path: str, project: str | None = None,
                         cwd: Path | None = None,
                         patch_edges: bool = True) -> dict[str, Any]:
        """Extract a source file's symbols + call graph via the LSP and persist them
        content-addressed under `<project>/symbol_index/`. Idempotent per file: drops
        symbols that vanished from it, records the file's mtime (the staleness gate),
        and — when `patch_edges` (a standalone/incremental reindex) — patches other
        files' `called_by` from this file's fresh outbound calls, so a single-file
        reindex keeps the cross-file call graph consistent. `patch_edges=False` in a
        full-project sweep (the LSP hands each file its edges directly). Off the loop."""
        from .codeindex import (NoServer, SymbolIndex, describe_file,
                                 describe_symbols, extract_file, find_root,
                                 match_description)
        p = Path(path)
        if not p.is_absolute():
            if cwd:
                p = Path(cwd) / p
            else:
                raise ValueError(
                    f"code_index needs an ABSOLUTE path (got relative {path!r}) — a "
                    f"relative path resolves against the daemon's cwd, not yours. Pass "
                    f"an absolute path, or cwd=<your working dir>.")
        p = p.resolve()
        root = find_root(p)
        rel = str(p.relative_to(root))
        proj = self.resolve_project(project, cwd)
        return await asyncio.to_thread(self._index_code_file_tracked, root, rel, proj, patch_edges)

    def _index_code_file_tracked(self, root: Path, rel: str, proj: str,
                         patch_edges: bool,
                         existing: dict[str, dict] | None = None) -> dict[str, Any]:
        """Tracked entry point for one-file indexing: registers (proj, rel) as
        in-flight for `status`, then runs `_index_code_file`."""
        with self.code.indexing_lock:
            self.code.indexing.setdefault(proj, []).append(rel)
        try:
            return self._index_code_file(root, rel, proj, patch_edges, existing)
        finally:
            with self.code.indexing_lock:
                files = self.code.indexing.get(proj, [])
                if rel in files:
                    files.remove(rel)
                if not files:
                    self.code.indexing.pop(proj, None)

    def _ref_edge_ctx(self, proj: str, root: Path | None = None) -> "RefProjects":
        """`extract_file`'s cross-project attribution context: each `.crib` ref's
        locally-resolved root (pre-resolved) + its indexed file set (for the
        site-packages suffix match). When `root` is given, an IN-TREE checkout of
        a ref'd project (nested `.crib` naming it — e.g. `vendor/llmkit`) is added
        as an extra attribution root for that ref: same repo, same relative paths,
        so edges into the vendored copy resolve to the ref project. Reads the
        refs' resident caches — cheap after first touch."""
        out: RefProjects = []
        nested_by_proj: dict[str, Path] = {}
        if root is not None:
            for nd in self._nested_project_roots(root):
                link = CribLink.find(nd)
                if link is not None:
                    nested_by_proj[link.project] = nd.resolve()
            root = root.resolve()
        for ref in self._project_refs(proj):
            files: frozenset[str] = frozenset()
            if ref["indexed"]:
                try:
                    files = frozenset(
                        e.get("file", "")
                        for e in self._resident_code(ref["project"]).entries)
                except Exception:  # noqa: BLE001 — a broken ref never fails indexing
                    pass
            rroot = ref["root"].resolve() if ref["root"] else None
            out.append((ref["project"], rroot, files))
            nested = nested_by_proj.get(ref["project"])
            if nested is not None and nested != rroot:
                out.append((ref["project"], nested, files))
        return out

    def _index_code_file(self, root: Path, rel: str, proj: str,
                          patch_edges: bool,
                          existing: dict[str, dict] | None = None) -> dict[str, Any]:
        """The blocking core of code_index — extract + describe + persist one file. Sync
        so the lazy revalidation path (also sync) can reuse it directly; code_index runs
        it off the event loop via to_thread. `existing` is the by-fqname snapshot of the
        prior index (for the content_hash gate + vanished-symbol drop); a full-project
        sweep parses it ONCE and passes it here so we don't re-`store.all()` per file
        (that made a cold onboard O(files × symbols)). None → parse it (standalone path)."""
        from .codeindex import (NoServer, SymbolIndex, describe_file,
                                 describe_symbols, extract_file, match_description)
        ref_ctx = self._ref_edge_ctx(proj, root)
        abs_p = (root / rel).resolve()
        for rname, rroot, _files in ref_ctx:
            # an in-tree ref checkout (e.g. vendor/llmkit) belongs to ITS project,
            # not this one — never index it into the parent (refs supersede the
            # old vendored-code-indexed-as-parent model)
            if rroot is not None and rroot != root.resolve() \
                    and abs_p.is_relative_to(rroot):
                return {"project": proj, "root": str(root), "file": rel,
                        "symbols": 0, "skipped": f"belongs to ref'd project {rname!r}"}
        # likewise a nested `.crib` bounds another project (watcher events for
        # files inside it must not index into the parent). Strictly UNDER root:
        # an ancestor .crib above a rootless project must not skip everything.
        link = CribLink.find(abs_p.parent)
        if link is not None and link.root is not None:
            lroot = link.root.resolve()
            if lroot != root.resolve() and lroot.is_relative_to(root.resolve()):
                return {"project": proj, "root": str(root), "file": rel,
                        "symbols": 0,
                        "skipped": f"inside nested project {link.project!r}"}
        try:
            entries = extract_file(root, rel, ref_projects=ref_ctx)
        except NoServer as exc:
            return {"project": proj, "root": str(root), "file": rel,
                    "symbols": 0, "skipped": str(exc)}
        # Semantic facet: LLM one-line descriptions, merged by fqname (§4).
        # content_hash GATE: reuse a cached description when the symbol's body is
        # unchanged; only call the LLM when something is stale/new. BEST-EFFORT: a
        # generation hiccup never loses the structural call graph (facets independent).
        store = SymbolIndex(self.paths.project_dir(proj))
        if existing is None:
            existing = {e["fqname"]: e for e in store.all()}
        old_in_file = {fq for fq, e in existing.items() if e.get("file") == rel}
        # KEEP-PRIOR-ON-EMPTY: a still-present, non-trivial file that extracts to ZERO
        # symbols is almost always a flaky LSP pass (empty documentSymbol from an init
        # race / short settle — shuck does this on zsh), not a real emptying. Pruning
        # here would silently delete real symbols until the next good reindex, so skip
        # it. A genuinely-emptied file (no code left) still prunes.
        if not entries and old_in_file:
            try:
                body = (root / rel).read_text(errors="ignore")
            except OSError:
                body = ""
            codeish = [ln for ln in body.splitlines()
                       if ln.strip() and not ln.lstrip().startswith("#")]
            if len(codeish) > 3:
                return {"project": proj, "root": str(root), "file": rel,
                        "symbols": len(old_in_file), "skipped": "empty-extract-kept-prior"}
        # PARTIAL-extract guard — the empty guard's unguarded cousin. Deleting a
        # symbol from the index on the LSP's say-so is only safe if the listing is
        # complete; a server answering mid-settle (esp. on the short warm-session
        # settle) can return a partial documentSymbol. Signature of partial:
        # strictly FEWER symbols and NOTHING new — a genuine edit that removes a
        # symbol virtually always also changes another (hash/line churn). Confirm
        # with one slow re-extract before trusting the shrink.
        fresh_fqns = {e["fqname"] for e in entries}
        if entries and old_in_file and len(fresh_fqns) < len(old_in_file) \
                and not (fresh_fqns - old_in_file):
            try:
                entries = extract_file(root, rel, settle=3.0, ref_projects=ref_ctx)
            except Exception:  # noqa: BLE001 — keep the fast read if the slow one fails
                pass
        stale = [e for e in entries
                 if existing.get(e["fqname"], {}).get("content_hash") != e["content_hash"]
                 or not existing.get(e["fqname"], {}).get("description")]
        gen_error: str | None = None
        descs: dict[str, str] = {}
        if stale:
            try:
                descs = describe_file(self.config.generate, root, rel)
            except Exception as exc:  # noqa: BLE001 — LLM down → structural-only
                gen_error = str(exc)
        for sym in entries:
            ex = existing.get(sym["fqname"], {})
            if ex.get("content_hash") == sym["content_hash"] and ex.get("description"):
                sym["description"] = ex["description"]          # cached, unchanged body
            else:
                sym["description"] = match_description(sym["fqname"], descs)
        # MOP-UP: symbols the whole-file bulk pass missed (low-yield / partial LLM
        # response) get a focused describe over just their bodies — far higher hit
        # rate on a small set. Best-effort; content_hash gate keeps future runs cheap.
        missed = [e for e in stale if not e.get("description")]
        if missed:
            try:
                mop = describe_symbols(self.config.generate, missed)
                for e in missed:
                    e["description"] = (mop.get(e["name"])
                                        or match_description(e["fqname"], mop))
            except Exception:  # noqa: BLE001 — mop-up is best-effort
                pass
        # Serialize only the store read-modify-write (NOT the LSP/LLM work above),
        # so a concurrent reindex of another file — watcher vs query vs explicit
        # index — can't interleave writes and corrupt the cross-file call graph
        # (`CodeStore.patch_called_by`). Kept off the slow describe path so the loop-thread
        # revalidation never blocks on a worker's LLM call.
        with self._code_lock(proj):
            store.write_all(entries)
            store.set_source_root(root)                     # for query-time revalidation
            # drop symbols that vanished from this file (renamed/removed) — else orphan
            for fq in old_in_file - {e["fqname"] for e in entries}:
                store.delete(fq)
            if patch_edges:
                self.code.patch_called_by(store, entries, rel)
        self._register_code_root(proj, root)                # live-watch this repo's source
        self._bump_code_epoch(proj)                         # invalidate the resident cache
        out: dict[str, Any] = {
            "project": proj, "root": str(root), "file": rel,
            "symbols": len(entries),
            "described": sum(1 for e in entries if e["description"]),
            "store": str(store.root)}
        if gen_error:
            out["descriptions_error"] = gen_error
        return out

    # ── Resident code cache: delegates to CodeStore (crib/codestore.py) ────────
    # Thin delegators so existing call sites are untouched; the state + its
    # invariants live in `self.code`. `_code_watched` (watcher, not code state) and
    # `_revalidate`/`_drop_file` (pipeline-coupled) stay as real methods below.
    def _code_lock(self, proj: str) -> threading.Lock:
        return self.code.lock(proj)

    def _bump_code_epoch(self, proj: str) -> None:
        self.code.bump_epoch(proj)

    def _code_freshness(self) -> str:
        return self.code.freshness()

    def _code_watched(self, proj: str) -> bool:
        """True when the code watcher is live-watching this project's source, so a
        per-query source revalidation sweep is redundant (edits refresh on save)."""
        cw = self._code_watcher
        return cw is not None and cw.watches(proj)

    def _dir_sig(self, proj: str) -> tuple[int, int]:
        return self.code.dir_sig(proj)

    def _code_tok(self, proj: str) -> tuple[str, Any]:
        return self.code.tok(proj)

    def _resident_code(self, proj: str) -> _ResidentCode:
        return self.code.resident(proj, revalidate=self._revalidate,
                                  watched=self._code_watched(proj))

    def _reload_code(self, proj: str, tok: Any,
                     prev: _ResidentCode | None) -> _ResidentCode:
        return self.code.reload(proj, tok, prev)

    def code_indexed_projects(self) -> list[dict[str, Any]]:
        """Projects that have a symbol_index, with counts — for orienting an agent
        whose call resolved to the wrong/empty project."""
        from .codeindex import SymbolIndex
        out = []
        for name in self.projects():
            si = SymbolIndex(self.paths.project_dir(name))
            if si.is_populated():
                out.append({"project": name, "symbols": len(si.all())})
        return sorted(out, key=lambda x: -x["symbols"])

    def _revalidate(self, proj: str) -> None:
        """Lazy staleness gate: stat every indexed source file; reindex any whose mtime
        moved since it was indexed, and drop symbols of deleted files. Keeps queries
        honest under live editing without a watcher (the watcher just makes it eager).
        Best-effort — an LSP hiccup leaves the stale entry rather than failing the query.
        No-op when the source root is unknown (older index / no meta). Sync (called from
        the sync query path); a reindex blocks only when a file actually changed."""
        from .codeindex import SymbolIndex, _parse
        store = SymbolIndex(self.paths.project_dir(proj))
        root = store.source_root()
        if root is None or not store.root.exists():
            return
        # Baseline = the on-disk mtime of a source file's symbol tomls (= WHEN indexed),
        # derived locally + cheap. NOT the toml's stored `mtime` field (that's a portable
        # git-date record, not comparable to a local st_mtime — and would need git here).
        baseline: dict[str, int] = {}        # source file → oldest mtime of its tomls
        dirty: set[str] = set()              # files with merge-dirtied symbols (blank
        for p in store.root.glob("*.toml"):  # content_hash, written by the sync merge
            try:                             # driver on divergent code states) — their
                mt = p.stat().st_mtime_ns    # tomls are FRESH (post-pull mtime), so the
                e = _parse(p.read_text())    # mtime gate alone would never catch them
                f = e.get("file", "")
            except OSError:
                continue
            if f:
                baseline[f] = min(baseline.get(f, mt), mt)
                if not e.get("content_hash"):
                    dirty.add(f)
        for rel, base_mt in baseline.items():
            src = root / rel
            try:
                cur = src.stat().st_mtime_ns
            except OSError:                  # deleted → drop all its symbols + its edges
                self._drop_file(proj, rel)
                continue
            if rel in dirty or cur > base_mt:   # merge-dirtied, or edited after indexing
                try:                         # (content_hash gate no-ops if unchanged)
                    self._index_code_file_tracked(root, rel, proj, patch_edges=True)
                except Exception:  # noqa: BLE001 — keep the stale entry over a failed query
                    pass

    def _drop_file(self, proj: str, relpath: str) -> None:
        self.code.drop_file(proj, relpath)

    def _require_code_index(self, proj: str) -> None:
        """Raise a self-diagnosing error when `proj` has no code index — so a call
        that resolved to the wrong/unset project tells the agent how to fix it (set the
        project, or name a different one; which projects ARE indexed) rather than
        returning a bare `[]` it misreads as 'this codebase isn't indexed'."""
        from .codeindex import SymbolIndex
        if SymbolIndex(self.paths.project_dir(proj)).is_populated():
            return
        avail = self.code_indexed_projects()
        if avail:
            names = ", ".join(f"{p['project']}" for p in avail)
            hint = (f" If you meant a DIFFERENT, already-indexed project ({names}), name "
                    f"it: pass project=<name> or project_path=<a path in that repo> (or "
                    f"use_project <name> to switch your current project).")
        else:
            hint = ""
        raise ValueError(
            f"project {proj!r} isn't code-indexed yet. If this is the repo you're working "
            f"in, INDEX IT NOW then retry: run project_index (project_path=<this repo dir>) "
            f"— it indexes the source so lookup/dossier/xref work; do NOT grep or read "
            f"files instead.{hint}")

    # ── Whole-project lifecycle: setup / index / forget / status ──────────────
    # Shared engine behind `crib project <verb>` (superset) and the code/notes
    # facets. Everything defers to `_ensure_crib`, the sensible-default .crib
    # creator, so onboarding an unfamiliar repo is one call (docs §…).
    def _code_ignore(self) -> frozenset[str]:
        return frozenset({".git", "node_modules", ".venv", "venv", "__pycache__",
                          ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist",
                          "build", "target", ".tox", ".idea", "site-packages",
                          ".cache", ".claude", ".DS_Store"})

    def _detect_code_globs(self, root: Path) -> list[str]:
        """Auto-detect source globs: which LSP-supported extensions actually occur
        under `root` (junk dirs pruned) → `**/*.<ext>` per present type."""
        from .codeindex import load_specs
        exts = {e for spec in load_specs().values() if isinstance(spec, dict)
                for e in (spec.get("extensionToLanguage") or {})}
        junk, present = self._code_ignore(), set()
        for dp, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in junk and not d.startswith(".")]
            for fn in files:
                if (e := Path(fn).suffix.lower()) in exts:
                    present.add(e)
        return sorted(f"**/*{e}" for e in present)

    def _nested_project_roots(self, root: Path) -> list[Path]:
        """Directories under `root` carrying their OWN `.crib` — project
        BOUNDARIES: their code belongs to the nested project (a vendored
        submodule with a .crib, say), never to the parent's index. `refs:` is
        how the parent xrefs into it (cross-project refs)."""
        junk = self._code_ignore()
        out: list[Path] = []
        for dp, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in junk and not d.startswith(".")]
            if ".crib" in files and Path(dp) != root:
                out.append(Path(dp))
                dirs[:] = []                 # bounded — no need to descend
        return out

    def _enumerate_code_files(self, root: Path, globs: list[str]) -> list[Path]:
        """Files to index: the extension globs, PLUS extensionless/unknown-suffix
        files whose grammar (shebang / bare name / `#compdef`|`#autoload` marker)
        resolves to a language served by an installed LSP — so shell autoload
        functions and dotfiles get indexed, not just `*.zsh`. Subtrees with
        their own `.crib` are skipped (project boundaries)."""
        from .codeindex import content_lang, load_grammar, load_specs, resolve_command
        junk, seen = self._code_ignore(), set()
        bounds = self._nested_project_roots(root)
        for g in globs:
            for p in root.glob(g):
                if p.is_file() and not any(part in junk
                                           for part in p.relative_to(root).parts) \
                        and not any(p.is_relative_to(b) for b in bounds):
                    seen.add(p)
        # extensionless / unknown-extension files matched by the grammar map
        specs = load_specs()
        served: set[str] = set()
        for sp in specs.values():
            if isinstance(sp, dict) and resolve_command(sp):
                served.update((sp.get("extensionToLanguage") or {}).values())
        known_exts = {e for sp in specs.values() if isinstance(sp, dict)
                      for e in (sp.get("extensionToLanguage") or {})}
        grammar = load_grammar()
        for dp, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in junk and not d.startswith(".")
                       and not any((Path(dp) / d) == b for b in bounds)]
            for fn in files:
                p = Path(dp) / fn
                if p in seen or p.suffix.lower() in known_exts:
                    continue                    # globs already cover known extensions
                lang = content_lang(p, grammar)
                if lang and lang in served:
                    seen.add(p)
        return sorted(seen)

    def _ensure_crib(self, cwd: Path | None, project: str | None,
                     want_code: bool, want_docs: bool) -> tuple[Any, bool]:
        """Find the repo's `.crib`, or CREATE one with sensible defaults (project =
        repo dir name; auto-detected code `paths:` + doc `docs:` globs indexed
        in-situ). Returns (CribLink, created?). The one primitive both `project
        setup` and the facet setups defer to."""
        base = (Path(cwd) if cwd else Path.cwd()).resolve()
        link = CribLink.find(base)
        if link and link.root:
            return link, False
        # Anchor at the nearest repo marker, else at `base` ITSELF — never escape to
        # base.parent (find_root's no-marker fallback), which would write .crib in the
        # wrong dir and index the parent tree.
        root = base
        for d in (base, *base.parents):
            if (d / ".git").exists() or (d / "pyproject.toml").exists() \
                    or (d / "setup.py").exists():
                root = d
                break
        name = project or root.name.replace(" ", "-")
        lines = ["# Auto-created by `crib project setup` — ties this repo to a crib "
                 "project.", f"project: {name}"]
        # globs are quoted: a bare `- **/*.py` makes YAML read `*` as an alias anchor.
        if want_code and (globs := self._detect_code_globs(root)):
            lines += ["paths:", *[f'  - "{g}"' for g in globs]]
        if want_docs:
            lines += ["docs:", '  - "README.md"', '  - "docs/**/*.md"']
        (root / ".crib").write_text("\n".join(lines) + "\n")
        return CribLink.find(root), True

    async def _index_project_code(self, proj: str, root: Path,
                                  globs: list[str]) -> dict[str, Any]:
        """Index every source file under `globs`. Non-code files self-skip (NoServer)."""
        from .codeindex import SymbolIndex
        files = self._enumerate_code_files(root, globs)
        # Parse the prior index ONCE (by fqname) and share it across the whole sweep —
        # each file only needs its own prior entries (content_hash gate + vanished-drop),
        # so re-`store.all()` per file made a cold onboard O(files × symbols). Now O(N).
        existing = {e["fqname"]: e for e in SymbolIndex(self.paths.project_dir(proj)).all()}
        # Index files CONCURRENTLY, bounded by [generate].concurrency (same default as
        # the notes describe path). The per-file describe is a network-bound LLM call
        # and _index_code_file_tracked takes the project lock only for the tiny write (not the
        # LLM), so N-at-once cuts the cold-onboard wall-clock ~N×. Bulk sweep pins the
        # root to the project's `.crib` root (consistent source_root) and skips the
        # per-file edge-patch (the LSP hands each file its cross-file edges directly).
        sem = asyncio.Semaphore(max(1, self.config.generate.concurrency))

        # MEMBERSHIP pins (docs §3.2): didOpen the sweep's FULL doc set on servers
        # whose spec opts in (`pinWorkspace`) — an open doc is in the server's
        # analysis set even when its own discovery would miss it (shuck can't find
        # extensionless autoloads), so cross-file edges cover everything crib
        # enumerated. Held for the sweep, released in the finally.
        from .codeindex import _POOL, server_for
        extra_roots = [r["root"].resolve() for r in self._project_refs(proj)
                       if r["root"] is not None
                       and not r["root"].resolve().is_relative_to(root.resolve())]
        pins: dict[str, tuple[list[str], dict, list[tuple[Path, str]]]] = {}
        for f in files:
            sel = server_for(str(f.resolve().relative_to(root.resolve())), abspath=f)
            if sel and sel[3].get("pinWorkspace"):
                label, argv, lang, spec = sel
                pins.setdefault(label, (argv, spec, []))[2].append((f, lang))
        for label, (argv, spec, docs) in pins.items():
            try:
                await asyncio.to_thread(_POOL.pin_docs, root, label, argv, spec,
                                        docs, extra_roots)
            except Exception:  # noqa: BLE001 — pinning is best-effort enrichment
                pass

        async def _one(f: Path) -> tuple[Path, dict[str, Any] | None, str | None]:
            rel = str(f.resolve().relative_to(root.resolve()))
            async with sem:
                try:
                    r = await asyncio.to_thread(self._index_code_file_tracked, root, rel, proj,
                                                False, existing)
                    return f, r, None
                except Exception as exc:  # noqa: BLE001 — one bad file never aborts the sweep
                    return f, None, str(exc)
                finally:
                    with self.code.indexing_lock:   # live progress for `status` pollers
                        if proj in self.code.sweeps:
                            self.code.sweeps[proj]["done"] += 1

        syms = desc = indexed = 0
        errors: list[dict[str, str]] = []
        with self.code.indexing_lock:
            self.code.sweeps[proj] = {"done": 0, "total": len(files)}
        try:
            for f, r, err in await asyncio.gather(*(_one(f) for f in files)):
                if err is not None:
                    errors.append({"file": str(f), "error": err})
                elif not (r or {}).get("skipped"):
                    indexed += 1
                    syms += (r or {}).get("symbols", 0)
                    desc += (r or {}).get("described", 0)
        finally:
            with self.code.indexing_lock:
                self.code.sweeps.pop(proj, None)
            if pins:
                await asyncio.to_thread(_POOL.unpin, root)
        out: dict[str, Any] = {"files_indexed": indexed, "files_seen": len(files),
                               "symbols": syms, "described": desc}
        if errors:
            out["errors"] = errors
        return out

    async def project_setup(self, project: str | None = None,
                            cwd: Path | None = None) -> dict[str, Any]:
        """Onboard a repo end-to-end: ensure `.crib` (create with sensible defaults if
        missing), index its declared docs IN-SITU (source is master, never copied),
        AND index all its source code. The superset — `code`+`notes`. Idempotent;
        safe to re-run. (`import` — copy a file into memory — stays a separate,
        explicit verb.)"""
        link, created = self._ensure_crib(cwd, project, want_code=True, want_docs=True)
        proj = project or link.project
        docs = await self.index_docs_insitu(proj, cwd) if link.doc_patterns else {}
        globs = link.paths or self._detect_code_globs(link.root)
        code = await self._index_project_code(proj, link.root, globs)
        return {"project": proj, "root": str(link.root), "crib_created": created,
                "docs_indexed": docs.get("docs", 0), **code}

    async def project_index(self, project: str | None = None,
                            cwd: Path | None = None) -> dict[str, Any]:
        """(Re)index the project's SOURCE CODE and in-situ docs from its `.crib`
        (ensuring a `.crib` first). Cheap re-run via the content-hash gate."""
        link, created = self._ensure_crib(cwd, project, want_code=True, want_docs=False)
        proj = project or link.project
        docs = await self.index_docs_insitu(proj, cwd) if link.doc_patterns else {}
        globs = link.paths or self._detect_code_globs(link.root)
        code = await self._index_project_code(proj, link.root, globs)
        return {"project": proj, "root": str(link.root), "crib_created": created,
                "docs_indexed": docs.get("docs", 0), **code}

    def _project_refs(self, proj: str) -> list[dict[str, Any]]:
        """The projects `proj`'s `.crib` names in `refs:` — cross-project xref
        targets. Each ref resolves to its own LOCAL checkout via that project's
        `.source_root` (machine-local, gitignored), so the committed `.crib`
        carries NAMES only and survives differently-located clones. → [{project,
        root (None when not locally present), indexed}]."""
        from .codeindex import SymbolIndex
        my_root = SymbolIndex(self.paths.project_dir(proj)).source_root()
        link = (CribLink.find(my_root)
                if my_root is not None and my_root.exists() else None)
        out: list[dict[str, Any]] = []
        for name in (link.refs if link else []):
            if name == proj:
                continue                     # self-reference is meaningless
            si = SymbolIndex(self.paths.project_dir(name))
            root = si.source_root()
            out.append({"project": name,
                        "root": root if root is not None and root.exists() else None,
                        "indexed": si.is_populated()})
        return out

    def project_status(self, project: str | None = None,
                       cwd: Path | None = None) -> dict[str, Any]:
        """Is this project code-indexed? symbol/file counts, kind breakdown, the
        `.crib` source paths — for orienting before setup/index."""
        from collections import Counter

        from .codeindex import SymbolIndex
        proj = self.resolve_project(project, cwd)
        si = SymbolIndex(self.paths.project_dir(proj))
        entries = si.all()
        link = CribLink.find(Path(cwd)) if cwd else None
        srcs = self._source_roots(proj).all()
        doc_count = sum(1 for m in self.store.get_meta({"project": proj}).values()
                        if m.get("relpath", "").startswith(SRC_PREFIX))
        refs = [{**r, "root": str(r["root"]) if r["root"] else None}
                for r in self._project_refs(proj)]
        return {"project": proj, "indexed": si.is_populated(),
                "symbols": len(entries),
                "files": len({e.get("file") for e in entries}),
                "kinds": dict(Counter(e.get("kind", "?") for e in entries)),
                "paths": (link.paths if link else []),
                "refs": refs,
                "doc_sources": srcs, "doc_chunks": doc_count,
                "crib": (str(link.root / ".crib") if link and link.root else None)}

    def status(self) -> dict[str, Any]:
        """One-call health summary (the `status` CLI verb / MCP tool): every
        project's inventory (notes, in-situ doc chunks, code symbols, learnings),
        git-sync state, the warm LSP sessions (which servers are attached,
        alive/busy/idle), and any in-flight indexing. Counts are cheap file
        counts (1 toml = 1 symbol), never full parses."""
        from .codeindex import _POOL, LEARNINGS_DIR
        projects = []
        for name in self.projects():
            nd = self.paths.notes_dir(name)
            ld = nd / LEARNINGS_DIR
            sd = self.paths.project_dir(name) / "symbol_index"
            doc_chunks = sum(1 for m in self.store.get_meta({"project": name}).values()
                             if m.get("relpath", "").startswith(SRC_PREFIX))
            projects.append({
                "project": name,
                "notes": sum(1 for _ in nd.rglob("*.md")) if nd.exists() else 0,
                "learnings": sum(1 for _ in ld.glob("*.md")) if ld.exists() else 0,
                "symbols": sum(1 for _ in sd.glob("*.toml")) if sd.exists() else 0,
                "doc_chunks": doc_chunks,
            })
        with self.code.indexing_lock:
            indexing = {p: list(v) for p, v in self.code.indexing.items() if v}
            sweeps = {p: dict(v) for p, v in self.code.sweeps.items()}
        return {"projects": projects,
                "git": self.git.state(),
                "lsp_sessions": _POOL.stats(),
                "indexing": indexing,
                # sweep progress: {proj: {done, total}} while a project index runs,
                # ABSENT when finished — poll this to wait on a background index
                "sweeps": sweeps,
                "store": type(self.store).__name__,
                "embed_model": self.config.embed.model}

    def project_forget(self, project: str | None = None, cwd: Path | None = None,
                       with_learnings: bool = False) -> dict[str, Any]:
        """Clear the project's code index (the symbol_index). KEEPS attached learnings,
        notes and `.crib` by default — learnings are durable human source-of-truth;
        pass with_learnings=True to drop those too."""
        import shutil

        from .codeindex import LEARNINGS_DIR, SymbolIndex
        proj = self.resolve_project(project, cwd)
        si_root = SymbolIndex(self.paths.project_dir(proj)).root
        removed = len(list(si_root.glob("*.toml"))) if si_root.exists() else 0
        if si_root.exists():
            shutil.rmtree(si_root)
        self.code.cache.pop(proj, None)     # drop resident cache (trust-mode won't see rmtree)
        self._bump_code_epoch(proj)
        # In-situ docs are index-only (source is master) — drop their chunks + the
        # source registry too; re-runnable via `project index`.
        reg = self._source_roots(proj)
        doc_ids = [i for i, m in self.store.get_meta({"project": proj}).items()
                   if m.get("relpath", "").startswith(SRC_PREFIX)]
        if doc_ids:
            self.store.delete(doc_ids)
            self.index.invalidate_caches(proj)
        for prefix in list(reg.all()):
            reg.remove(prefix)
        learnings = 0
        if with_learnings:
            ldir = self.notes_dir(proj) / LEARNINGS_DIR
            if ldir.exists():
                learnings = len(list(ldir.glob("*.md")))
                shutil.rmtree(ldir)
        return {"project": proj, "symbols_removed": removed,
                "doc_chunks_removed": len(doc_ids), "learnings_removed": learnings}

    def code_xref(self, symbol: str, project: str | None = None,
                  cwd: Path | None = None) -> list[dict[str, Any]]:
        """Callers/callees for a symbol from the persisted symbol_index — no live
        LSP. A local miss falls through to the project's `.crib` `refs:` (cross-
        project xref); every entry carries `project`."""
        proj = self.resolve_project(project, cwd)
        self._require_code_index(proj)
        rc = self._resident_code(proj)       # resident; refreshes source per freshness mode
        matches = rc.by_fqname(symbol)
        owner = proj
        if not matches:
            for ref in self._project_refs(proj):
                if not ref["indexed"]:
                    continue
                matches = self._resident_code(ref["project"]).by_fqname(symbol)
                if matches:
                    owner = ref["project"]
                    break
        for m in matches:
            m["project"] = owner
        return self._attach_learnings(owner, matches)

    def code_dossier(self, symbol: str, project: str | None = None,
                     cwd: Path | None = None, edge_cap: int = 20) -> dict[str, Any]:
        """Everything about ONE symbol in a single call: its signature + description,
        its callers / callees / references — each annotated with the NEIGHBOUR'S own
        description — and any attached learning. Built for agents: read a symbol and
        understand its neighbourhood without a dozen follow-up lookups."""
        proj = self.resolve_project(project, cwd)
        self._require_code_index(proj)
        rc = self._resident_code(proj)
        # local first, then the `.crib` refs — the neighbourhood (edges, learnings)
        # lives with the OWNING project, so everything below reads from there
        owner, entry = self._resolve_symbol_or_ref(proj, symbol, rc)
        if owner != proj:
            rc = self._resident_code(owner)
        self._attach_learnings(owner, [entry])
        idx = rc.entries
        desc = {e["fqname"]: e.get("description", "") for e in idx}
        by_nf = {(e.get("name", ""), e.get("file", "")): e["fqname"] for e in idx}

        ref_maps: dict[str, tuple[dict, dict]] = {}   # ref proj → (desc, by_nf)

        def _maps(rp: str) -> tuple[dict, dict]:
            if rp not in ref_maps:
                try:
                    rrc = self._resident_code(rp)
                    ref_maps[rp] = (
                        {e["fqname"]: e.get("description", "") for e in rrc.entries},
                        {(e.get("name", ""), e.get("file", "")): e["fqname"]
                         for e in rrc.entries})
                except Exception:  # noqa: BLE001 — unindexed ref → unresolved edge
                    ref_maps[rp] = ({}, {})
            return ref_maps[rp]

        def neigh(edges: list[str] | None) -> list[dict[str, Any]]:
            out = []
            for ref in (edges or [])[:edge_cap]:
                name, _, rest = ref.partition(" [")
                nm, fref = name.strip(), rest.rstrip("]")
                if ":" in fref:            # QUALIFIED edge — lives in a ref'd project
                    rp, _, rrel = fref.partition(":")
                    rdesc, rnf = _maps(rp)
                    fq = rnf.get((nm, rrel))
                    out.append({"symbol": fq or nm, "file": rrel, "project": rp,
                                "description": rdesc.get(fq or "", "")})
                    continue
                fq = by_nf.get((nm, fref))
                out.append({"symbol": fq or nm, "file": fref,
                            "description": desc.get(fq or "", "")})
            extra = max(len(edges or []) - edge_cap, 0)
            if extra:
                out.append({"symbol": f"… +{extra} more", "file": "", "description": ""})
            return out

        return {
            "fqname": entry["fqname"], "kind": entry.get("kind"),
            "project": owner,
            "file": entry.get("file"), "line": entry.get("line"),
            "signature": entry.get("signature"), "description": entry.get("description"),
            "learning": entry.get("learning"),
            "calls": neigh(entry.get("calls")),
            "called_by": neigh(entry.get("called_by")),
            "references": neigh(entry.get("references")),
        }

    # ── Durable learnings attached to a symbol (docs/code-symbol-index.md) ─────
    # Same primitives as notes — append / edit / forget / read — scoped to a code
    # symbol. Each resolves the fqn to a note under <project>/code-learnings/ and
    # reuses the standard note machinery, so learnings inherit versioning, git
    # sync and the frontmatter merge driver. They are SEPARATE from the LLM
    # description (a regenerable cache): re-indexing never disturbs them.
    def _resolve_symbol(self, proj: str, symbol: str,
                        rc: "_ResidentCode | None" = None) -> dict[str, Any]:
        """Resolve a user-supplied symbol to exactly one indexed entry. Exact fqn
        wins; a bare/partial name resolves only if unique — else raise, listing
        candidates (never silently pick, so a learning can't land on the wrong one).
        Resolves against a resident cache when one is passed (avoids a disk read)."""
        from .codeindex import SymbolIndex
        matches = (rc.by_fqname(symbol) if rc is not None
                   else SymbolIndex(self.paths.project_dir(proj)).by_fqname(symbol))
        if not matches:
            raise ValueError(f"unknown symbol {symbol!r} in project {proj!r} — "
                             f"code_lookup it, or code_index the file first")
        exact = [m for m in matches if m.get("fqname") == symbol]
        cands = exact or matches
        if len(cands) > 1:
            names = ", ".join(sorted(m.get("fqname", "") for m in cands)[:8])
            raise ValueError(f"ambiguous symbol {symbol!r} → {names}; pass a full fqname")
        return cands[0]

    def _resolve_symbol_or_ref(self, proj: str, symbol: str,
                               rc: "_ResidentCode | None" = None,
                               ) -> tuple[str, dict[str, Any]]:
        """Resolve locally first; on a LOCAL MISS, try the project's `.crib`
        `refs:` (cross-project xref) → (owning_project, entry). Exactly one ref
        matching wins; several → ambiguous error naming the projects; none →
        the local unknown-symbol error. A local AMBIGUITY still raises (refs
        never paper over it)."""
        try:
            return proj, self._resolve_symbol(proj, symbol, rc)
        except ValueError as local_err:
            if "unknown symbol" not in str(local_err):
                raise
            found: list[tuple[str, dict[str, Any]]] = []
            for ref in self._project_refs(proj):
                if not ref["indexed"]:
                    continue
                try:
                    found.append((ref["project"],
                                  self._resolve_symbol(ref["project"], symbol)))
                except ValueError:
                    continue
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                names = ", ".join(f"{p}:{e['fqname']}" for p, e in found)
                raise ValueError(
                    f"ambiguous symbol {symbol!r} across refs → {names}; "
                    f"pass project= to disambiguate") from None
            raise local_err

    def _learning_relpath(self, entry: dict[str, Any]) -> str:
        from .codeindex import LEARNINGS_DIR, learning_slug
        return f"{LEARNINGS_DIR}/{learning_slug(entry['fqname'])}.md"

    def _attach_learnings(self, proj: str,
                          entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Enrich symbol entries in place with any attached learning (📌) + a
        staleness flag, so pinned understanding resurfaces exactly where you're
        already looking (code_lookup / code_xref). Keyed O(1) by learning_slug(fqn)
        — only symbols that actually carry a note pay a read. `stale` = the symbol's
        body changed (content_hash) since the learning was written; a heads-up, not
        an invalidation — the subtlety usually still holds."""
        from .codeindex import LEARNINGS_DIR, learning_slug
        ldir = self.notes_dir(proj) / LEARNINGS_DIR
        if not ldir.exists():
            return entries
        for e in entries:
            fq = e.get("fqname")
            if not fq:
                continue
            relpath = f"{LEARNINGS_DIR}/{learning_slug(fq)}.md"
            path = self.notes_dir(proj) / relpath
            if not path.exists():
                continue
            note = notes.load(path)
            wrote, cur = note.frontmatter.get("content_hash"), e.get("content_hash")
            e["learning"] = {"relpath": relpath, "path": str(path),
                             "stale": bool(wrote and cur and wrote != cur),
                             "body": note.body.strip()}
        return entries

    async def code_append(self, symbol: str, text: str, project: str | None = None,
                          cwd: Path | None = None) -> dict[str, Any]:
        """Attach a durable learning to a symbol: append a dated entry to its
        running note (create it, with symbol-keyed frontmatter, on first use)."""
        proj = self.resolve_project(project, cwd)
        entry = self._resolve_symbol(proj, symbol)
        fqn = entry["fqname"]
        relpath = self._learning_relpath(entry)
        path = self.abspath(proj, relpath)
        existed = path.exists()
        if existed:
            note = notes.load(path)
            # refresh the drift/staleness snapshot to the most-recent authoring
            note.frontmatter["content_hash"] = entry.get("content_hash", "")
            note.frontmatter["file"] = entry.get("file", note.frontmatter.get("file", ""))
            note.frontmatter["signature"] = entry.get("signature",
                                                      note.frontmatter.get("signature", ""))
        else:
            note = Note(path=path, body="", frontmatter={
                "title": fqn, "kind": "code-learning", "symbol": fqn,
                "lang": entry.get("lang", ""), "file": entry.get("file", ""),
                "signature": entry.get("signature", ""),
                "content_hash": entry.get("content_hash", ""),
                "source": "code-note"})
        today = datetime.date.today().isoformat()
        note.body = note.body.rstrip() + f"\n\n### {today}\n{text.strip()}\n"
        res = await self._write_note(proj, relpath, note)
        return {"project": proj, "symbol": fqn, "relpath": relpath,
                "created": not existed, "indexed": res.upserted}

    async def code_edit(self, symbol: str, new_content: str, project: str | None = None,
                        cwd: Path | None = None) -> dict[str, Any]:
        """Replace a symbol's learning body wholesale (fix/rewrite), frontmatter
        preserved. Errors if no learning exists yet — use code_append to create."""
        proj = self.resolve_project(project, cwd)
        entry = self._resolve_symbol(proj, symbol)
        relpath = self._learning_relpath(entry)
        path = self.abspath(proj, relpath)
        if not path.exists():
            raise ValueError(f"no learning for {entry['fqname']!r} yet — code_append first")
        note = notes.load(path)
        note.frontmatter["content_hash"] = entry.get("content_hash", "")
        note.body = new_content.strip() + "\n"
        res = await self._write_note(proj, relpath, note)
        return {"project": proj, "symbol": entry["fqname"], "relpath": relpath,
                "indexed": res.upserted}

    async def code_forget(self, symbol: str, project: str | None = None,
                          cwd: Path | None = None) -> dict[str, Any]:
        """Remove a symbol's learning (stashed to the version ring first, so it's
        recoverable). Works on ORPHANS too: if the symbol no longer resolves, forget
        by its recorded fqn — so cleanup never requires a live symbol."""
        from .codeindex import LEARNINGS_DIR, learning_slug
        proj = self.resolve_project(project, cwd)
        try:
            fqn = self._resolve_symbol(proj, symbol)["fqname"]
        except ValueError:
            fqn = symbol                      # orphan: gone from the index, note lingers
        relpath = f"{LEARNINGS_DIR}/{learning_slug(fqn)}.md"
        if not self.abspath(proj, relpath).exists():
            raise ValueError(f"no learning for {symbol!r} in project {proj!r}")
        res = await self.forget(relpath, proj)
        return {**res, "symbol": fqn}

    async def code_reaffirm(self, symbol: str, project: str | None = None,
                            cwd: Path | None = None) -> dict[str, Any]:
        """Clear a learning's ⚠ stale flag WITHOUT editing the body — you re-checked
        it against the current code and it still holds. Re-snapshots content_hash /
        file / signature to the symbol's current state and stamps `reaffirmed`."""
        proj = self.resolve_project(project, cwd)
        entry = self._resolve_symbol(proj, symbol)
        relpath = self._learning_relpath(entry)
        path = self.abspath(proj, relpath)
        if not path.exists():
            raise ValueError(f"no learning for {entry['fqname']!r} yet — code_append first")
        note = notes.load(path)
        note.frontmatter["content_hash"] = entry.get("content_hash", "")
        note.frontmatter["file"] = entry.get("file", note.frontmatter.get("file", ""))
        note.frontmatter["signature"] = entry.get("signature",
                                                  note.frontmatter.get("signature", ""))
        note.frontmatter["reaffirmed"] = datetime.date.today().isoformat()
        res = await self._write_note(proj, relpath, note)
        return {"project": proj, "symbol": entry["fqname"], "relpath": relpath,
                "reaffirmed": note.frontmatter["reaffirmed"], "indexed": res.upserted}

    def _learning_fqns(self, proj: str) -> set[str]:
        """Set of fqns that carry a learning (read from `symbol:` frontmatter — the
        authoritative fqn, so the lossy slug never has to be reversed)."""
        from .codeindex import LEARNINGS_DIR
        ldir = self.notes_dir(proj) / LEARNINGS_DIR
        out: set[str] = set()
        if ldir.exists():
            for p in ldir.glob("*.md"):
                fq = notes.load(p).frontmatter.get("symbol")
                if fq:
                    out.add(fq)
        return out

    def code_learnings(self, project: str | None = None, cwd: Path | None = None,
                       orphans_only: bool = False) -> list[dict[str, Any]]:
        """Health of every attached learning: `ok` | `moved` | `orphan`. `moved` =
        the fqn still resolves but the symbol's file drifted from the snapshot;
        `orphan` = the fqn no longer resolves (rename/move/delete). Report-only —
        never gates indexing; drives cleanup (code_rehome / code_forget)."""
        from .codeindex import LEARNINGS_DIR, SymbolIndex
        proj = self.resolve_project(project, cwd)
        ldir = self.notes_dir(proj) / LEARNINGS_DIR
        by_fq = {e["fqname"]: e
                 for e in SymbolIndex(self.paths.project_dir(proj)).all()}
        out: list[dict[str, Any]] = []
        if ldir.exists():
            for p in sorted(ldir.glob("*.md")):
                fm = notes.load(p).frontmatter
                fq = fm.get("symbol", "")
                cur = by_fq.get(fq)
                if cur is None:
                    status, new_file = "orphan", None
                elif cur.get("file") != fm.get("file"):
                    status, new_file = "moved", cur.get("file")
                else:
                    status, new_file = "ok", None
                if orphans_only and status == "ok":
                    continue
                out.append({"symbol": fq, "status": status, "file": fm.get("file", ""),
                            "new_file": new_file, "signature": fm.get("signature", ""),
                            "relpath": f"{LEARNINGS_DIR}/{p.name}"})
        return out

    def _rehome_candidates(self, fm: dict[str, Any], entries: list[dict[str, Any]],
                           top: int = 6) -> list[dict[str, Any]]:
        """Rank index symbols as rehome targets for an orphaned learning from the
        snapshot we kept — unqualified name (survives a container/module rename),
        signature token overlap, same file. Structural only; the human/LLM confirms.
        (git-history ranking is the documented next lever — see docs § step 5.)"""
        oldname = fm.get("symbol", "").split(".")[-1]
        oldfile = fm.get("file", "")
        oldsig = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", fm.get("signature", "")))
        scored: list[tuple[float, dict[str, Any]]] = []
        for e in entries:
            if e.get("fqname") == fm.get("symbol"):
                continue                                    # itself, if it resolves
            s = 0.0
            if e.get("name") == oldname:
                s += 3.0
            if oldfile and e.get("file") == oldfile:
                s += 2.0
            sig = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", e.get("signature", "")))
            if oldsig and sig:
                s += 2.0 * len(oldsig & sig) / len(oldsig | sig)
            if s > 0:
                scored.append((s, e))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [{"fqname": e["fqname"], "file": e.get("file", ""),
                 "signature": e.get("signature", ""), "score": round(s, 2)}
                for s, e in scored[:top]]

    async def code_rehome(self, old_fqn: str, new_fqn: str | None = None,
                          project: str | None = None,
                          cwd: Path | None = None) -> dict[str, Any]:
        """Re-point an orphaned learning at the symbol it became. Without `new_fqn`:
        return ranked candidates (never auto-move — a wrong attach is worse than a
        dangling one). With `new_fqn`: move the note to the new symbol's slug,
        re-snapshot its frontmatter, preserve the note id/history."""
        from .codeindex import LEARNINGS_DIR, SymbolIndex, learning_slug
        proj = self.resolve_project(project, cwd)
        old_rel = f"{LEARNINGS_DIR}/{learning_slug(old_fqn)}.md"
        old_path = self.abspath(proj, old_rel)
        if not old_path.exists():
            raise ValueError(f"no learning for {old_fqn!r} in project {proj!r}")
        entries = SymbolIndex(self.paths.project_dir(proj)).all()
        if new_fqn is None:
            fm = notes.load(old_path).frontmatter
            return {"old": old_fqn, "relpath": old_rel,
                    "candidates": self._rehome_candidates(fm, entries)}
        new_entry = next((e for e in entries if e.get("fqname") == new_fqn), None)
        if new_entry is None:                               # allow a unique bare name
            m = [e for e in entries if e["fqname"].endswith("." + new_fqn)
                 or e.get("name") == new_fqn]
            if len(m) != 1:
                raise ValueError(f"target {new_fqn!r} not found or not unique in index")
            new_entry = m[0]
        new_rel = f"{LEARNINGS_DIR}/{learning_slug(new_entry['fqname'])}.md"
        note = notes.load(old_path)
        note.frontmatter.update({
            "symbol": new_entry["fqname"], "title": new_entry["fqname"],
            "lang": new_entry.get("lang", ""), "file": new_entry.get("file", ""),
            "signature": new_entry.get("signature", ""),
            "content_hash": new_entry.get("content_hash", ""), "rehomed_from": old_fqn})
        res = await self._write_note(proj, new_rel, note)   # id preserved
        if new_rel != old_rel:
            old_path.unlink()
            await self.index.index_file(proj, self.notes_dir(proj), old_rel)
        return {"project": proj, "old": old_fqn, "new": new_entry["fqname"],
                "relpath": new_rel, "indexed": res.upserted}

    def code_read(self, symbol: str, project: str | None = None,
                  cwd: Path | None = None) -> dict[str, Any]:
        """Read a symbol's learning note (frontmatter + body), or None if unwritten."""
        proj = self.resolve_project(project, cwd)
        entry = self._resolve_symbol(proj, symbol)
        relpath = self._learning_relpath(entry)
        path = self.abspath(proj, relpath)
        if not path.exists():
            return {"project": proj, "symbol": entry["fqname"], "relpath": relpath,
                    "path": str(path), "found": False, "body": None}
        note = notes.load(path)
        return {"project": proj, "symbol": entry["fqname"], "relpath": relpath,
                "path": str(path), "found": True,
                "frontmatter": note.frontmatter, "body": note.body}

    def code_lookup(self, query: str, project: str | None = None, k: int = 8,
                    cwd: Path | None = None,
                    sparse_weight: float = 0.2) -> list[dict[str, Any]]:
        """Find a symbol — HYBRID: dense concept search over LLM `description`s ⊕ BM25
        over `name_terms` (unqualified/qualified/subtokens), RRF-fused. So a concept
        query hits the dense side and a bare/partial *name* hits the sparse side. Served
        from the content-addressed store at query time; no live LSP needed.

        FANS OUT to the project's `.crib` `refs:` (cross-project xref): each indexed
        ref is searched the same way and the per-project rankings RRF-fuse, the
        queried project weighted above its refs. Every hit carries `project`."""
        from .retrieve import reciprocal_rank_fusion
        proj = self.resolve_project(project, cwd)
        self._require_code_index(proj)
        pools: dict[str, list[dict[str, Any]]] = {
            proj: self._code_lookup_one(proj, query, k, sparse_weight)}
        for ref in self._project_refs(proj):
            if not ref["indexed"] or ref["project"] in pools:
                continue
            try:
                pools[ref["project"]] = self._code_lookup_one(
                    ref["project"], query, k, sparse_weight)
            except Exception:  # noqa: BLE001 — a broken ref never fails the query
                continue
        if len(pools) == 1:
            hits = pools[proj][:k]
        else:
            # EQUAL weights: rank decides (a ref's best hit must be able to beat a
            # local mid-ranker — a damped weight buries refs below any full local
            # top-k, since RRF is rank- not score-based). The queried project is
            # the FIRST ranking, so exact rank ties break local-first.
            by_key = {f"{p}:{h['fqname']}": h for p, hs in pools.items() for h in hs}
            fused = reciprocal_rank_fusion(
                [[f"{p}:{h['fqname']}" for h in hs] for p, hs in pools.items()])
            hits = [by_key[key] for key in fused[:k]]
        for i, h in enumerate(hits):
            h["rank"] = i + 1
        return hits

    def _code_lookup_one(self, proj: str, query: str, k: int,
                         sparse_weight: float) -> list[dict[str, Any]]:
        """The single-project core of `code_lookup` (dense ⊕ sparse, RRF-fused),
        project-tagged and learning-annotated. `rank` is per-pool; the caller
        re-ranks after any cross-project fusion."""
        from .retrieve import _subtokens, reciprocal_rank_fusion, tokenize
        rc = self._resident_code(proj)                       # resident: no re-parse/re-embed
        entries = rc.lk
        if not entries:
            return []
        ids = rc.lk_ids
        by_id = rc.by_fq
        rankings: list[list[str]] = []
        weights: list[float] = []
        # dense: cosine over the resident description embeddings (only the query embeds)
        dense = rc.dense(self.embedder)
        if any(v for v in dense):
            qv = self.embedder.embed_query([query])[0]
            ds = [sum(a * b for a, b in zip(qv, v)) if v else -2.0 for v in dense]
            rankings.append([ids[i] for i in sorted(range(len(ids)),
                                                    key=lambda i: ds[i], reverse=True)])
            weights.append(1.0)
        # sparse: BM25 over the name terms (the unqualified name is the load-bearing one)
        ss = rc.bm25.scores(tokenize(query) + _subtokens(query))
        sparse = [ids[i] for i in sorted(range(len(ids)), key=lambda i: ss[i],
                                         reverse=True) if ss[i] > 0]
        if sparse:
            rankings.append(sparse)
            # Sparse damped below dense so a stray name-token match ("result" →
            # IndexResult) can't outrank a concept hit — but a strong exact-name
            # match still surfaces when dense is weak (a bare-name query). Mirrors
            # the keyword_weight=0.3 damping on the note side (retrieve.py); the
            # default is tuned on scripts/eval_code.py.
            weights.append(sparse_weight)
        if not rankings:
            return []
        fused = reciprocal_rank_fusion(rankings, weights=weights)
        keys = ("fqname", "name", "kind", "file", "line", "signature", "description",
                "parent", "calls", "called_by", "references", "content_hash")
        # echo the resolved project on each hit — cheap orientation so the agent
        # always sees which project answered (esp. useful across related codebases)
        hits = [{**{key: by_id[fid].get(key) for key in keys},
                 "project": proj, "rank": i + 1}
                for i, fid in enumerate(fused[:k])]
        return self._attach_learnings(proj, hits)

    def code_graph(self, symbol: str, direction: str = "callees", depth: int = 6,
                   project: str | None = None,
                   cwd: Path | None = None) -> dict[str, Any]:
        """Build a call-graph TREE around `symbol` from the persisted symbol_index —
        `callees` follows `calls`, `callers` follows `called_by`. Returns a nested
        {fqname, kind, file, line, children[]} with DAG-repeats marked `repeat` and
        unresolved edges `external`. Rendered pstree-style by the CLI. No LSP/LLM."""
        proj = self.resolve_project(project, cwd)
        self._require_code_index(proj)
        rc = self._resident_code(proj)
        entries = rc.entries
        # per-project (name, file) maps — the tree can cross into ref'd projects
        # via QUALIFIED edges ("name [proj:rel]") and keeps walking there
        nf_maps: dict[str, dict[tuple[str, str], dict]] = {}

        def _nf(p: str) -> dict[tuple[str, str], dict]:
            if p not in nf_maps:
                try:
                    m: dict[tuple[str, str], dict] = {}
                    for e in self._resident_code(p).entries:
                        m.setdefault((e.get("name", ""), e.get("file", "")), e)
                    nf_maps[p] = m
                except Exception:  # noqa: BLE001 — unindexed ref → external leaf
                    nf_maps[p] = {}
            return nf_maps[p]

        _nf(proj)
        root = (rc.by_fq.get(symbol)
                or next((e for e in entries if e.get("name") == symbol
                         or e["fqname"].endswith("." + symbol)), None))
        root_proj = proj
        if not root:                          # local miss → the `.crib` refs
            try:
                root_proj, root = self._resolve_symbol_or_ref(proj, symbol, rc)
            except ValueError:
                return {}
        edge = {"callees": "calls", "callers": "called_by",
                "references": "references"}.get(direction, "calls")
        seen: set[str] = set()

        def build(e: dict, p: str, d: int) -> dict:
            node = {"fqname": e["fqname"], "kind": e.get("kind", ""),
                    "file": e.get("file", ""), "line": e.get("line"), "children": []}
            if p != proj:
                node["project"] = p
            key = f"{p}:{e['fqname']}"
            if key in seen:
                node["repeat"] = True
                return node
            seen.add(key)
            if d <= 0:
                return node
            for ref in e.get(edge) or []:
                name, _, rest = ref.partition(" [")
                fref = rest.rstrip("]")
                if ":" in fref:               # qualified → hop into the ref project
                    tp, _, trel = fref.partition(":")
                else:
                    tp, trel = p, fref
                child = _nf(tp).get((name.strip(), trel))
                if child:
                    node["children"].append(build(child, tp, d - 1))
                else:
                    node["children"].append({"fqname": name.strip(), "kind": "?",
                                             "file": fref, "external": True,
                                             "children": []})
            return node

        tree = build(root, root_proj, depth)
        # glyph carriers, per owning project (a cross-project node's learning
        # lives with ITS project, and same-named local fqns must not false-mark)
        marks: dict[str, set[str]] = {}
        stack = [tree]
        while stack:
            n = stack.pop()
            p = n.get("project") or proj
            if p not in marks:
                marks[p] = self._learning_fqns(p)
            if n.get("fqname") in marks[p]:
                n["has_learning"] = True
            stack.extend(n.get("children") or [])
        return tree

    async def _generate_index(self, root_name: str, purpose: str, label: str,
                              prompt: str | None, relpath: str | None,
                              project: str | None, cwd: Path | None,
                              overwrite: bool) -> dict[str, Any]:
        """Shared section-level generation for keyword_index / summary_index: one
        LLM call per **section** with the full section as context (windows share
        one result), persisted section-addressed so it survives re-windowing.
        Bounded-concurrent, per-call timeout, error-isolated; skips cached sections
        unless `overwrite`. Off the write path."""
        proj = self.resolve_project(project, cwd)
        from .section_index import SectionIndex, parse_terms
        if prompt is None:
            raise ValueError(
                f"unknown {purpose} label {label!r}: no builtin and no "
                f"[{purpose}.{label}].prompt in config")
        store = SectionIndex(self.paths.project_dir(proj), root_name)
        from .generate import agenerate, agenerate_structured, resolve_provider
        try:                                    # record the resolved provider for provenance
            _p = resolve_provider(self.config.generate, purpose)
            model = _p.model or _p.adapter or ""
        except Exception:  # noqa: BLE001 — provenance only; generation reports real errors
            model = self.config.generate.model or self.config.generate.adapter

        # Group chunks into their sections (section_hash), reconstructing the full
        # section text once per note from the file (windows would double-count
        # overlap). One generation per section.
        section_line_maps: dict[str, dict[str, tuple[int, int]]] = {}

        def _section_text(rp: str, heading: str, fallback: str) -> str:
            if rp not in section_line_maps:
                try:
                    section_line_maps[rp] = section_line_map(
                        self.abspath(proj, rp).read_text())
                except OSError:
                    section_line_maps[rp] = {}
            span = section_line_maps[rp].get(heading)
            if span:
                try:
                    lines = self.abspath(proj, rp).read_text().splitlines()
                    return "\n".join(lines[span[0] - 1:span[1]])
                except OSError:
                    pass
            return fallback

        seen_sections: set[str] = set()
        targets: list[tuple[str, str, str, str]] = []   # (section_hash, relpath, heading, body)
        skipped = 0
        for _cid, (doc, meta) in self.store.get_docs({"project": proj}).items():
            if relpath and meta.get("relpath") != relpath:
                continue
            sh = meta.get("section_hash") or meta.get("content_hash")
            if not sh or sh in seen_sections:
                continue
            seen_sections.add(sh)
            if not overwrite and store.has(label, sh):
                skipped += 1
                continue
            rp = meta.get("relpath", "")
            heading = meta.get("heading_path", "")
            targets.append((sh, rp, heading, _section_text(rp, heading, doc)))

        gen = self.config.generate
        total = len(targets)
        sem = asyncio.Semaphore(max(1, gen.concurrency))
        counters = {"written": 0, "errors": 0, "done": 0, "bulk_docs": 0}
        done_sh: set[str] = set()   # sections already written (bulk covered them)
        tag = f"{purpose} {label}"
        mode = "bulk+mop-up" if gen.bulk else "per-section"
        print(f"[{tag}] {total} sections to generate ({mode}; skipped {skipped} "
              f"cached; concurrency {gen.concurrency}, timeout {gen.timeout:g}s)",
              file=sys.stderr, flush=True)

        def _record(sh: str, rp: str, heading: str, terms: list[str]) -> None:
            if terms and sh not in done_sh:
                store.write(label, sh, terms, relpath=rp, heading=heading, model=model)
                counters["written"] += 1
                done_sh.add(sh)

        async def _one(sh: str, rp: str, heading: str, body: str) -> None:
            # Per-section: bounded-concurrent, timeout-capped, error-isolated —
            # one hung/failed section is skipped, never sinks the pass. Serves both
            # the legacy path (bulk off) and the mop-up for bulk-missed sections.
            user = f"{heading}\n\n{body}" if heading else body
            try:
                async with sem:
                    text = await agenerate(gen, prompt, user, purpose=purpose,
                                           timeout=gen.timeout)
                terms = parse_terms(text)
            except Exception as e:  # noqa: BLE001 — timeout or generation failure
                counters["errors"] += 1
                counters["done"] += 1
                print(f"[{tag}] {counters['done']}/{total} ERR  {rp} :: "
                      f"{heading[-44:]} — {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
                return
            _record(sh, rp, heading, terms)
            counters["done"] += 1
            print(f"[{tag}] {counters['done']}/{total} ok   {rp} :: "
                  f"{heading[-44:]} ({len(terms)} terms)", file=sys.stderr, flush=True)

        # Phase 1 — whole-doc bulk authoring (structured, one call per note/batch):
        # the model sees a note's sections together, so it can pick section-
        # *distinctive* terms (blind per-section authoring made them generic — see
        # docs/retrieval-and-adoption.md §5.5). Content-addressing makes it safe: a
        # skipped/malformed section simply isn't written and is swept by Phase 2, so
        # strict model conformance is an efficiency, not a correctness, property.
        if gen.bulk and targets:
            from collections import defaultdict

            bulk_schema = {
                "type": "object",
                "properties": {"sections": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"heading": {"type": "string"},
                                   "terms": {"type": "array",
                                             "items": {"type": "string"}}},
                    "required": ["heading", "terms"]}}},
                "required": ["sections"],
            }
            bulk_system = (
                f"{prompt}\n\nYou are given a DOCUMENT of several sections, each "
                "introduced by a line `<<< SECTION: <heading> >>>`. Apply the "
                "instruction above to EACH section independently and return one "
                "result per section, using that section's exact <heading> string. "
                "Cover every section.")

            def _match(h: str, by_head: dict[str, tuple]) -> tuple | None:
                t = by_head.get(h)
                if t:
                    return t
                hl = h.strip().lower().lstrip("#").strip()
                for head, tt in by_head.items():
                    hh = head.lower()
                    if hl and (hl == hh.split("/")[-1].strip() or hl in hh):
                        return tt
                return None

            by_doc: dict[str, list[tuple]] = defaultdict(list)
            for t in targets:
                by_doc[t[1]].append(t)
            step = max(1, gen.bulk_max_sections)
            batches = [items[i:i + step] for items in by_doc.values()
                       for i in range(0, len(items), step)]

            async def _bulk(batch: list[tuple]) -> None:
                rp = batch[0][1]
                by_head = {t[2]: t for t in batch}
                user = "\n\n".join(f"<<< SECTION: {t[2]} >>>\n{t[3]}" for t in batch)
                try:
                    async with sem:
                        data = await agenerate_structured(
                            gen, bulk_system, user, bulk_schema, purpose=purpose,
                            schema_name="emit_terms",
                            schema_description="Per-section search terms for the document.",
                            timeout=gen.timeout)
                except Exception as e:  # noqa: BLE001 — bulk fails → mop-up covers it
                    print(f"[{tag}] bulk ERR {rp} ({len(batch)} sec) — "
                          f"{type(e).__name__}: {e} → mop-up",
                          file=sys.stderr, flush=True)
                    return
                items = (data.get("sections") if isinstance(data, dict) else None) or []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    terms = [str(x).strip() for x in (it.get("terms") or [])
                             if str(x).strip()]
                    tgt = _match(str(it.get("heading") or ""), by_head)
                    if tgt and terms:
                        _record(tgt[0], tgt[1], tgt[2], terms)
                counters["bulk_docs"] += 1
                got = sum(1 for t in batch if t[0] in done_sh)
                print(f"[{tag}] bulk {rp}: {got}/{len(batch)} sections",
                      file=sys.stderr, flush=True)

            await asyncio.gather(*(_bulk(b) for b in batches))

        # Phase 2 — per-section mop-up for anything Phase 1 didn't cover (or every
        # section when bulk is off). Idempotent: `done_sh` gates re-writes.
        mop = [t for t in targets if t[0] not in done_sh]
        if mop:
            if gen.bulk and counters["bulk_docs"]:
                print(f"[{tag}] mop-up: {len(mop)} of {total} sections uncovered",
                      file=sys.stderr, flush=True)
            await asyncio.gather(*(_one(*t) for t in mop))

        if targets:
            self.index.invalidate_caches(proj)   # feeds BM25 (keywords) + aliases (summaries)
        return {"project": proj, "label": label, "written": counters["written"],
                "skipped": skipped, "errors": counters["errors"], "total": total,
                "bulk_docs": counters["bulk_docs"]}

    # --- in-situ docs (default: source is master, never copied) ------------
    async def index_docs_insitu(self, project: str | None = None,
                                cwd: Path | None = None) -> dict[str, Any]:
        """Index a repo's `.crib`-declared docs IN-SITU — the source tree is the
        master; crib holds only the index, never a copy. Each doc is a
        source-anchored note keyed `sources/<repo>/<rel>`; `read`/`locate` return
        the repo file, so an LLM that edits it edits the master. Re-runnable
        (hash-gated), and it prunes docs deleted from the source."""
        link = CribLink.find(cwd or Path.cwd())
        if link is None or link.root is None:
            raise ValueError("no .crib found from cwd upward")
        proj = project or link.project
        repo = link.root.name
        prefix = f"{SRC_PREFIX}{repo}/"
        self._source_roots(proj).upsert(prefix, link.root)
        self._register_code_root(proj, link.root)   # watch source-tree edits (docs + code)

        nd = self.notes_dir(proj)
        seen: set[str] = set()
        indexed: list[str] = []
        for pattern in link.doc_patterns:
            for src in sorted(link.root.glob(pattern)):
                if not src.is_file():
                    continue
                rel = src.relative_to(link.root)
                relpath = src_relpath(repo, rel.as_posix())
                seen.add(relpath)
                res = await self.index.index_file(proj, nd, relpath, content_path=src)
                if res.changed:
                    indexed.append(relpath)
        # Prune so the `docs:` globs are AUTHORITATIVE: anything indexed under this
        # prefix that the current globs no longer match is dropped — whether it was
        # removed from the source tree OR indexed out-of-glob by the watcher (which
        # now filters by the same globs, but this cleans up ones that leaked in
        # before that). The source file, if any, stays; crib only owned the index.
        removed = 0
        for rp in self._indexed_relpaths(proj, prefix) - seen:
            removed += await self.index.forget(proj, rp)
        return {"project": proj, "root": str(link.root), "prefix": prefix,
                "docs": len(seen), "changed": len(indexed), "removed": removed}

    def _indexed_relpaths(self, project: str, prefix: str) -> set[str]:
        """Relpaths currently indexed under `prefix` (one meta scan)."""
        out: set[str] = set()
        for m in self.store.get_meta({"project": project}).values():
            rp = m.get("relpath", "")
            if rp.startswith(prefix):
                out.add(rp)
        return out

    # --- import ------------------------------------------------------------
    async def import_files(self, paths: list[str], project: str | None = None,
                           cwd: Path | None = None) -> dict[str, Any]:
        """Copy explicit files INTO memory as crib-owned notes — manual only.

        Unlike in-situ docs (source is master, never copied), this takes a list of
        paths you name and pulls a snapshot into `imported/<name>.md`: git-synced,
        editable in crib, versioned. Use it to *own* a copy (annotate it, carry it
        cross-machine). Source wins on re-import; the note id (and history) is
        preserved across re-pulls. Provenance is byte-identical across machines so a
        git sync never conflicts on it (DESIGN §14)."""
        proj = self.resolve_project(project, cwd)
        created = self.project_is_new(proj)
        today = datetime.date.today().isoformat()

        imported: list[str] = []
        for p in paths:
            src = Path(p).expanduser()
            if not src.is_absolute():
                # Relative paths anchor to the CALLER: the CLI ships its shell cwd,
                # an MCP agent's `project_path` names the repo. Without an anchor,
                # error — resolving against the daemon's own cwd would be silent
                # nonsense (and could even hit an unrelated same-named file).
                if cwd is None:
                    raise ValueError(
                        f"relative path {p!r} has no anchor: pass absolute paths, "
                        "or project_path=<repo dir> to resolve them against")
                src = cwd / src
            src = src.resolve()
            if not src.is_file():
                raise ValueError(f"not a file: {p}")
            relpath = f"imported/{src.name}"
            sfm, sbody = notes.parse(src.read_text())
            tgt = self.abspath(proj, relpath)
            prev = notes.load(tgt) if tgt.exists() else None
            fm = dict(sfm)
            fm.update({
                "source": "imported",
                "source_path": portable_path(src, self.config.locations),
                "imported": (prev.frontmatter.get("imported") if prev else None)
                            or today,
            })
            note_id = (prev.id if prev and prev.id else None) or derived_ulid(relpath)
            fm = {"id": note_id, **fm}
            note = Note(path=tgt, frontmatter=fm, body=sbody)
            await self._write_note(proj, relpath, note)
            imported.append(relpath)
        return {"project": proj, "imported": len(imported), "files": imported,
                "created": created}

    # --- claude harness memory mirror (DESIGN §13) -------------------------
    async def import_claude_memory(self, project: str | None = None,
                                   cwd: Path | None = None,
                                   root: Path | None = None) -> dict[str, Any]:
        """Mirror Claude Code's harness memory into
        `<project>/notes/claude-memory/<host>/`.

        One-way: the harness owns those files; we copy+index, never write back.
        Host-namespaced so a git-synced data dir merges (not collides) two
        machines' memories. The crib note id is preserved across syncs, so
        history/identity survive; files removed upstream are dropped (reconcile,
        scoped to THIS host). Records a binding for the daemon's live mirror.
        """
        start = root or cwd or Path.cwd()
        src_root = root or claudemem.find_harness_root(start)
        if src_root is None or not claudemem.harness_memory_dir(src_root).is_dir():
            raise ValueError(
                f"no Claude memory dir found from {start} upward "
                f"(looked under {claudemem.claude_config_dir() / 'projects'})")
        mem_dir = claudemem.harness_memory_dir(src_root)
        proj = self.resolve_project(project, cwd)
        created = self.project_is_new(proj)
        prefix = f"claude-memory/{claudemem.hostslug()}/"
        today = datetime.date.today().isoformat()

        synced: list[str] = []
        seen: set[str] = set()
        # MEMORY.md is the harness's index/TOC, not content — skip it.
        for src in sorted(mem_dir.glob("*.md")):
            if src.name == "MEMORY.md" or not src.is_file():
                continue
            relpath = f"{prefix}{src.name}"
            seen.add(relpath)
            sfm, sbody = notes.parse(src.read_text())
            mtype = ((sfm.get("metadata") or {}) if isinstance(sfm.get("metadata"), dict)
                     else {}).get("type")
            tags = list(dict.fromkeys(
                [*(sfm.get("tags") or []), "claude-memory", *( [mtype] if mtype else [])]))
            fm = {**sfm, "source": "claude_memory", "host": claudemem.hostslug(),
                  "source_path": str(src), "memory_name": sfm.get("name"),
                  "synced": today, "tags": tags}
            tgt = self.abspath(proj, relpath)
            prev_id = notes.load(tgt).id if tgt.exists() else None
            # Derived id (from the host-namespaced path) is stable across syncs,
            # so identity/history survive without a per-machine random ULID (§14).
            fm = {"id": prev_id or derived_ulid(relpath), **fm}
            note = Note(path=tgt, frontmatter=fm, body=sbody)
            note.path = tgt
            notes.save_atomic(note)             # derived: bypass the version ring
            await self.index.index_file(proj, self.notes_dir(proj), relpath)
            synced.append(relpath)

        removed = await self._reconcile_memory_dir(proj, prefix, seen)
        self.memory_bindings.upsert(src_root, proj)
        return {"project": proj, "source": str(mem_dir),
                "synced": len(synced), "removed": removed, "files": synced,
                "created": created}

    async def _reconcile_memory_dir(self, proj: str, prefix: str,
                                    keep: set[str]) -> int:
        """Drop mirrored memory files gone upstream — scoped to THIS host's
        subdir, so a synced peer machine's memories are never reaped."""
        host_dir = self.notes_dir(proj) / prefix
        if not host_dir.is_dir():
            return 0
        removed = 0
        for f in sorted(host_dir.glob("*.md")):
            relpath = f"{prefix}{f.name}"
            if relpath not in keep:
                f.unlink()
                await self.index.index_file(proj, self.notes_dir(proj), relpath)
                removed += 1
        return removed

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
