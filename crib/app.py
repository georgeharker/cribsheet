"""Crib — the core service. Implements the tool verbs (DESIGN §5).

Both the MCP server and the CLI call into this; tests exercise it directly. All
writes go through `_write_note` so every mutation stashes a version and funnels
through the one hash-gated `index_file`.
"""

from __future__ import annotations

import asyncio
import datetime
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import claudemem, notes
from .chunk import section_line_map
from .claudemem import MemoryBindings
from .config import (Config, CribLink, ProjectConfig, portable_path,
                     resolve_project)
from .embed import build_embedder
from .gitbacking import GitBacking
from .indexer import IndexEngine, IndexResult
from .notes import Note
from .paths import Paths
from .store import Hit, InMemoryStore, Store
from .util import derived_ulid
from .versions import VersionRing
from .watch import Watcher


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


class Crib:
    def __init__(self, paths: Paths, config: Config, store: Store) -> None:
        self.paths = paths
        self.config = config
        self.store = store
        self.embedder = build_embedder(config.embed)
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
        self._mirror: Any = None        # MemoryMirror, started by the daemon
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
        if self._mirror is not None:
            self._mirror.stop()
            self._mirror = None

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
        from .generate import agenerate, resolve_provider
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
        targets: list[tuple[str, str, str, str]] = []   # (section_hash, relpath, heading, user)
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
            body = _section_text(rp, heading, doc)
            user = f"{heading}\n\n{body}" if heading else body
            targets.append((sh, rp, heading, user))

        gen = self.config.generate
        total = len(targets)
        sem = asyncio.Semaphore(max(1, gen.concurrency))
        counters = {"written": 0, "errors": 0, "done": 0}
        tag = f"{purpose} {label}"
        print(f"[{tag}] {total} sections to generate "
              f"(skipped {skipped} cached; concurrency {gen.concurrency}, "
              f"timeout {gen.timeout:g}s)", file=sys.stderr, flush=True)

        async def _one(sh: str, rp: str, heading: str, user: str) -> None:
            # Per-section: bounded-concurrent, timeout-capped, error-isolated —
            # one hung/failed section is skipped, never sinks the pass.
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
            if terms:
                store.write(label, sh, terms, relpath=rp, heading=heading, model=model)
                counters["written"] += 1
            counters["done"] += 1
            print(f"[{tag}] {counters['done']}/{total} ok   {rp} :: "
                  f"{heading[-44:]} ({len(terms)} terms)", file=sys.stderr, flush=True)

        if targets:
            await asyncio.gather(*(_one(*t) for t in targets))
            self.index.invalidate_caches(proj)   # feeds BM25 (keywords) + aliases (summaries)
        return {"project": proj, "label": label, "written": counters["written"],
                "skipped": skipped, "errors": counters["errors"], "total": total}

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
        created = self.project_is_new(proj)
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
                tgt = self.abspath(proj, relpath)
                prev = notes.load(tgt) if tgt.exists() else None
                fm = dict(sfm)
                # Provenance built to be byte-identical across machines, so a
                # git sync never conflicts on it (DESIGN §14): id derived from the
                # target path (not a per-machine random ULID), source_repo stored
                # as a portable $LOCATION token (not an absolute path), and the
                # import date pinned to first-import (preserved across re-pulls).
                fm.update({
                    "source": "imported",
                    "source_repo": portable_path(link.root, self.config.locations),
                    "source_path": rel_to_repo.as_posix(),
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
