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
from .embed import Embedder
from .retrieve import LexicalCache
from .store import Record, Store


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
                 overlap: int = WINDOW_OVERLAP) -> None:
        self.store = store
        self.embedder = embedder
        self.window_words = window_words
        self.overlap = overlap
        self.lexical = LexicalCache(store)   # warm per-project BM25 (DESIGN §10.3)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _key(self, project: str, relpath: str) -> str:
        return f"{project}\x00{relpath}"

    async def index_file(self, project: str, notes_dir: Path, relpath: str) -> IndexResult:
        """Reindex one note. Idempotent + hash-gated under a per-path lock."""
        async with self._locks[self._key(project, relpath)]:
            return self._index_locked(project, notes_dir, relpath)

    def _index_locked(self, project: str, notes_dir: Path, relpath: str) -> IndexResult:
        path = notes_dir / relpath

        # Deleted on disk -> drop all its chunks.
        if not path.exists():
            existing = self.store.get_meta({"project": project, "relpath": relpath})
            self.store.delete(list(existing))
            if existing:
                self.lexical.invalidate(project)
            return IndexResult(relpath, changed=bool(existing), upserted=0,
                               deleted=len(existing))

        notes.heal_file(path)               # self-heal merge-duplicated frontmatter (§14)
        note = notes.load(path)
        if notes.ensure_id(note):           # assign + persist a stable id
            notes.save_atomic(note)
        note_id = note.id or ""
        mtime = path.stat().st_mtime

        new_chunks = chunk_note(project, relpath, note_id, note.body,
                                self.window_words, self.overlap)
        new_by_id = {c.chunk_id: c for c in new_chunks}

        existing = self.store.get_meta({"project": project, "relpath": relpath})
        existing_hash = {i: m.get("content_hash") for i, m in existing.items()}

        # Hash gate: embed/upsert only chunks whose content changed.
        to_embed = [c for cid, c in new_by_id.items()
                    if existing_hash.get(cid) != c.content_hash]
        stale_ids = [cid for cid in existing if cid not in new_by_id]

        if not to_embed and not stale_ids:
            return IndexResult(relpath, changed=False, upserted=0, deleted=0,
                               note_id=note_id)

        source = note.frontmatter.get("source", "manual")
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
        self.lexical.invalidate(project)   # corpus changed -> rebuild BM25 lazily
        return IndexResult(relpath, changed=True, upserted=len(records),
                           deleted=len(stale_ids), note_id=note_id)
