"""The one path to the index (DESIGN §4).

`index_file` is the single, idempotent, content-hash-gated routine that every
writer — tools, the watcher, direct LLM edits — funnels through. It is wrapped
in a per-path async lock. The hash gate makes it a no-op when content is
unchanged, so racing writers and noisy filesystem events degrade to redundant
work, never a wrong index.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from . import notes
from .chunk import WINDOW_OVERLAP, WINDOW_WORDS, chunk_note
from .util import derived_ulid as _derive_id
from .embed import Embedder
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

    def _key(self, project: str, relpath: str) -> str:
        return f"{project}\x00{relpath}"

    def invalidate_caches(self, project: str) -> None:
        """Drop both derived retrieval caches for a project — BM25 corpus and
        summary alias vectors — after any mutation to its chunks or index assets."""
        self.lexical.invalidate(project)
        self.summaries.invalidate(project)

    async def index_file(self, project: str, notes_dir: Path, relpath: str,
                         content_path: Path | None = None) -> IndexResult:
        """Reindex one note. Idempotent + hash-gated under a per-path lock.

        `content_path` decouples where the bytes are READ from how the note is
        KEYED: source-anchored docs are read from the repo (`content_path`) but
        keyed by their `sources/<repo>/…` relpath. Default reads `notes_dir/relpath`."""
        async with self._locks[self._key(project, relpath)]:
            return self._index_locked(project, notes_dir, relpath, content_path)

    def _index_locked(self, project: str, notes_dir: Path, relpath: str,
                      content_path: Path | None = None) -> IndexResult:
        path = content_path if content_path is not None else notes_dir / relpath

        # Deleted on disk -> drop all its chunks.
        if not path.exists():
            existing = self.store.get_meta({"project": project, "relpath": relpath})
            self.store.delete(list(existing))
            if existing:
                self.invalidate_caches(project)
            return IndexResult(relpath, changed=bool(existing), upserted=0,
                               deleted=len(existing))

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
                                self.window_words, self.overlap)
        new_by_id = {c.chunk_id: c for c in new_chunks}
        source = note.frontmatter.get("source",
                                      "doc-insitu" if read_only else "manual")

        existing = self.store.get_meta({"project": project, "relpath": relpath})
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
                new_meta = c.metadata(note.title, note.tags, source, mtime)
                if _meta_stable(existing.get(cid, {})) != _meta_stable(new_meta):
                    meta_updates[cid] = new_meta

        if not to_embed and not stale_ids and not meta_updates:
            return IndexResult(relpath, changed=False, upserted=0, deleted=0,
                               note_id=note_id)

        records: list[Record] = []
        if to_embed:
            vectors = self.embedder.embed([c.index_text for c in to_embed])
            for c, vec in zip(to_embed, vectors):
                records.append(Record(
                    id=c.chunk_id, embedding=vec, document=c.text,
                    metadata=c.metadata(note.title, note.tags, source, mtime),
                ))
        self.store.upsert(records)
        self.store.delete(stale_ids)
        if meta_updates:
            self.store.set_meta(meta_updates)
        self.invalidate_caches(project)   # corpus + aliases changed -> rebuild lazily
        return IndexResult(relpath, changed=True, upserted=len(records),
                           deleted=len(stale_ids), note_id=note_id)
