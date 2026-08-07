"""The one path to the index (DESIGN §4).

`index_file` is the single, idempotent, content-hash-gated routine that every
writer — tools, the watcher, direct LLM edits — funnels through. It is wrapped
in a per-path async lock. The hash gate makes it a no-op when content is
unchanged, so racing writers and noisy filesystem events degrade to redundant
work, never a wrong index.

Everything blocking inside that lock — the file read + chunking, the embed, the
store write — runs in a worker thread. The lock is deliberately held across
those awaits: it still serializes writers per path, but a 100-chunk embed no
longer freezes the event loop (and with it every other MCP client).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import notes
from .chunk import Chunk, WINDOW_OVERLAP, WINDOW_WORDS, chunk_note
from .util import derived_ulid as _derive_id
from .embed import Embedder, embed_batch
from .retrieve import LexicalCache, SummaryVectorCache
from .store import Record, Store


def _meta_stable(meta: dict) -> dict:
    """Metadata minus fields that change on their own every reindex — so drift
    detection fires on real schema/frontmatter changes, not a fresh mtime."""
    return {k: v for k, v in meta.items() if k != "file_mtime"}


@dataclass
class IndexResult:
    relpath: str
    changed: bool
    upserted: int
    deleted: int
    note_id: str | None = None


@dataclass
class _Plan:
    """What the read+diff stage decided, handed from its worker thread to the
    async orchestrator: chunks needing an embed (paired with the metadata to
    store), ids to drop, cheap metadata-only refreshes. `result` set means the
    decision was terminal (file gone, or the hash gate said no-op) — no embed and
    no store write follow."""
    note_id: str | None = None
    to_embed: list[tuple[Chunk, dict]] = field(default_factory=list)
    stale_ids: list[str] = field(default_factory=list)
    meta_updates: dict[str, dict] = field(default_factory=dict)
    result: IndexResult | None = None
    invalidate: bool = False


class IndexEngine:
    def __init__(self, store: Store, embedder: Embedder,
                 window_words: int = WINDOW_WORDS,
                 overlap: int = WINDOW_OVERLAP,
                 keyword_terms=None, summary_terms=None) -> None:
        self.store = store
        self.embedder = embedder
        self.window_words = window_words
        self.overlap = overlap
        # warm per-project BM25 (DESIGN §10.3); keyword_terms folds keyword_index
        # labels into the corpus when activated (§3.1)
        self.lexical = LexicalCache(store, keyword_terms)
        # warm per-project summary_index alias vectors (dense side, §3)
        self.summaries = SummaryVectorCache(store, embedder, summary_terms)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _key(self, project: str, store: str, relpath: str) -> str:
        return f"{project}\x00{store}\x00{relpath}"

    def invalidate_caches(self, project: str) -> None:
        """Drop both derived retrieval caches for a project — BM25 corpus and
        summary alias vectors — after any mutation to its chunks or index assets."""
        self.lexical.invalidate(project)
        self.summaries.invalidate(project)

    def invalidate_all_caches(self) -> None:
        """Drop the derived retrieval caches for EVERY project — after a store-wide
        event (`Store.recreate()` on an embedder-dim change) that no per-project
        invalidation covers. Both caches are built from the store, so a wipe leaves
        every warm entry describing chunks that no longer exist."""
        self.lexical.invalidate_all()
        self.summaries.invalidate_all()

    async def index_file(self, project: str, notes_dir: Path, relpath: str,
                         content_path: Path | None = None, *,
                         store: str = "notes") -> IndexResult:
        """Reindex one note. Idempotent + hash-gated under a per-path lock.

        `content_path` decouples where the bytes are READ from how the note is
        KEYED: source-anchored docs are read from the repo (`content_path`) but
        keyed by their `sources/<repo>/…` relpath. Default reads `notes_dir/relpath`.
        `store` is the pillar the note belongs to — it reaches chunk identity and
        metadata, and scopes the per-path lock."""
        async with self._locks[self._key(project, store, relpath)]:
            return await self._index_locked(project, notes_dir, relpath,
                                            content_path, store=store)

    async def forget(self, project: str, relpath: str, *,
                     store: str = "notes") -> int:
        """Drop a note's index entry (all its chunks) REGARDLESS of disk state, under
        the per-path lock — unlike `index_file`, which only deletes a note gone from
        disk. For pruning an in-situ doc that no longer matches the `.crib` `docs:`
        globs (the source file stays; crib never owned it). Returns chunks removed."""
        async with self._locks[self._key(project, store, relpath)]:
            existing = await asyncio.to_thread(
                self._existing_meta, project, store, relpath)
            await asyncio.to_thread(self.store.delete, list(existing))
            if existing:
                self.invalidate_caches(project)
            return len(existing)

    def _existing_meta(self, project: str, store: str, relpath: str) -> dict:
        """This note's indexed chunks — project + relpath narrowed to its pillar.
        Store-relative relpaths repeat across pillars, so the relpath alone is
        ambiguous; chunks indexed before the split lack the `store` key, hence
        the absence rule rather than an equality where-clause."""
        return {cid: m for cid, m in self.store.get_meta(
                    {"project": project, "relpath": relpath}).items()
                if ((m or {}).get("store") or "notes") == store}

    async def _index_locked(self, project: str, notes_dir: Path, relpath: str,
                            content_path: Path | None = None, *,
                            store: str = "notes") -> IndexResult:
        """The locked body: read+diff, embed, write — each stage offloaded, the
        lock held across them (see the module docstring)."""
        plan = await asyncio.to_thread(
            self._plan, project, notes_dir, relpath, content_path, store)
        if plan.result is not None:
            if plan.invalidate:
                self.invalidate_caches(project)
            return plan.result

        records: list[Record] = []
        if plan.to_embed:
            vectors = await asyncio.to_thread(
                embed_batch, self.embedder, [c.index_text for c, _ in plan.to_embed])
            records = [Record(id=c.chunk_id, embedding=vec, document=c.text,
                              metadata=meta)
                       for (c, meta), vec in zip(plan.to_embed, vectors)]
        await asyncio.to_thread(self._commit, records, plan.stale_ids,
                                plan.meta_updates)
        self.invalidate_caches(project)   # corpus + aliases changed -> rebuild lazily
        return IndexResult(relpath, changed=True, upserted=len(records),
                           deleted=len(plan.stale_ids), note_id=plan.note_id)

    def _commit(self, records: list[Record], stale_ids: list[str],
                meta_updates: dict[str, dict]) -> None:
        """The store writes as one worker-thread job (the stores are thread-safe)."""
        self.store.upsert(records)
        self.store.delete(stale_ids)
        if meta_updates:
            self.store.set_meta(meta_updates)

    def _plan(self, project: str, notes_dir: Path, relpath: str,
              content_path: Path | None = None, store: str = "notes") -> _Plan:
        """Read the file, chunk it, diff against the index — all blocking, so this
        runs in a worker thread. Decides what (if anything) needs embedding."""
        path = content_path if content_path is not None else notes_dir / relpath

        # Deleted on disk -> drop all its chunks.
        if not path.exists():
            existing = self._existing_meta(project, store, relpath)
            self.store.delete(list(existing))
            return _Plan(
                invalidate=bool(existing),
                result=IndexResult(relpath, changed=bool(existing), upserted=0,
                                   deleted=len(existing)))

        # A source-anchored doc (content_path given) is READ-ONLY: the repo owns
        # it, so never heal/rewrite it or stamp an id into it — derive a stable id
        # from its relpath instead.
        read_only = content_path is not None
        if not read_only:
            notes.heal_file(path)           # self-heal merge-duplicated frontmatter (§14)
        note = notes.load(path)
        if read_only:
            note_id = note.id or _derive_id(relpath)
        else:
            if notes.ensure_id(note):       # assign + persist a stable id
                notes.save_atomic(note)
            note_id = note.id or ""
        mtime = path.stat().st_mtime

        new_chunks = chunk_note(project, relpath, note_id, note.body,
                                self.window_words, self.overlap, store=store)
        new_by_id = {c.chunk_id: c for c in new_chunks}
        source = note.frontmatter.get("source",
                                      "doc-insitu" if read_only else "manual")
        note_type = str(note.frontmatter.get("type") or "")   # design/plan facets

        existing = self._existing_meta(project, store, relpath)
        existing_hash = {i: m.get("content_hash") for i, m in existing.items()}

        # Hash gate: embed/upsert only chunks whose content changed.
        to_embed = [c for cid, c in new_by_id.items()
                    if existing_hash.get(cid) != c.content_hash]
        stale_ids = [cid for cid in existing if cid not in new_by_id]

        # Metadata drift on content-UNCHANGED chunks: a new schema field (e.g.
        # section_hash) or edited frontmatter (tags/title/source) that the
        # content gate misses. Refresh metadata cheaply — no re-embed. Compare
        # ignoring file_mtime, which changes every reindex on its own.
        meta_updates: dict[str, dict] = {}
        for cid, c in new_by_id.items():
            if existing_hash.get(cid) == c.content_hash:   # content unchanged
                new_meta = c.metadata(note.title, note.tags, source, mtime, note_type)
                if _meta_stable(existing.get(cid, {})) != _meta_stable(new_meta):
                    meta_updates[cid] = new_meta

        if not to_embed and not stale_ids and not meta_updates:
            return _Plan(note_id=note_id,
                         result=IndexResult(relpath, changed=False, upserted=0,
                                            deleted=0, note_id=note_id))

        return _Plan(
            note_id=note_id,
            to_embed=[(c, c.metadata(note.title, note.tags, source, mtime, note_type))
                      for c in to_embed],
            stale_ids=stale_ids, meta_updates=meta_updates)
