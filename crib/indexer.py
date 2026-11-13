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
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import filesync, notes
from .chunk import WINDOW_OVERLAP, WINDOW_WORDS, Chunk, chunk_note, kind_of
from .embed import Embedder, embed_batch
from .items import Item, body_text, edges_of, facet_of, item_hash
from .retrieve import LexicalCache, SummaryVectorCache
from .store import Record, Store
from .util import derived_ulid as _derive_id


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
    # The file's item row (crib.items): written with the chunks, even when the
    # chunks themselves are unchanged. `item_embed` is (title, body) when its
    # vectors must be (re)computed; otherwise `item` already carries them.
    item: Item | None = None
    # The file's stamp right after the plan rewrote it (a heal, a new id), so
    # the index stamps the bytes it wrote rather than the ones it first saw.
    wrote: filesync.Stamp | None = None
    item_embed: tuple[str, str] | None = None
    item_deletes: list[str] = field(default_factory=list)



def index_facet(store: str) -> str:
    """The file-stamp facet (crib.filesync) for a pillar's chunk index: separate
    from the design/plan graph's own stamps, which map items only."""
    return f"index:{store}"


class IndexEngine:
    def __init__(self, store: Store, embedder: Embedder,
                 window_words: int = WINDOW_WORDS,
                 overlap: int = WINDOW_OVERLAP,
                 keyword_map=None, alias_vectors=None) -> None:
        self.store = store
        self.embedder = embedder
        self.window_words = window_words
        self.overlap = overlap
        # warm per-project BM25 (DESIGN §10.3); keyword_map folds keyword_index
        # labels into the corpus when activated (§3.1)
        self.lexical = LexicalCache(store, keyword_map)
        # the in-memory index over the store's summary alias vectors (§3)
        self.summaries = SummaryVectorCache(store, alias_vectors)
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
            if content_path is not None:  # a repo-owned doc: no stamp to keep
                return await self._index_locked(project, notes_dir, relpath,
                                                content_path, store=store)
            # Stamped BEFORE the read: an edit landing mid-index leaves a stamp
            # older than the file, so the next sweep looks again (never the
            # reverse, a stamp claiming content that was not indexed). When the
            # index itself rewrote the file (a heal, a new id), the stamp is of
            # the bytes it wrote.
            before = filesync.stamp(notes_dir / relpath)
            wrote: list[filesync.Stamp] = []
            res = await self._index_locked(project, notes_dir, relpath,
                                           content_path, store=store, wrote=wrote)
            await asyncio.to_thread(self._stamp, project, store, relpath,
                                    wrote[-1] if wrote else before)
            return res

    def _stamp(self, project: str, store: str, relpath: str,
               st: filesync.Stamp | None) -> None:
        """Record that `relpath` is indexed as of `st` (None: it is gone), for
        the stamp-gated reconcile (NoteStore.reindex)."""
        facet = index_facet(store)
        if st is None:
            self.store.replace_files(facet, project, [], [relpath])
        else:
            self.store.replace_files(facet, project, [(relpath, *st, [])], [])

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
        ambiguous."""
        return self.store.get_meta(
            {"project": project, "kind": kind_of(store, relpath), "relpath": relpath})

    async def _index_locked(self, project: str, notes_dir: Path, relpath: str,
                            content_path: Path | None = None, *,
                            store: str = "notes",
                            wrote: list[filesync.Stamp] | None = None) -> IndexResult:
        """The locked body: read+diff, embed, write — each stage offloaded, the
        lock held across them (see the module docstring). `wrote` receives the
        stamp of the file as the plan rewrote it, if it did."""
        plan = await asyncio.to_thread(
            self._plan, project, notes_dir, relpath, content_path, store)
        if wrote is not None and plan.wrote is not None:
            wrote.append(plan.wrote)
        if plan.result is not None:
            if plan.item is not None or plan.item_deletes:
                # The chunks needed nothing, but the item row did (e.g. the
                # first index after upgrading): write just that.
                item = await self._embed_item(plan)
                await asyncio.to_thread(
                    self.store.apply, [], [], {},
                    [item] if item is not None else [], plan.item_deletes)
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
        item = await self._embed_item(plan)
        await asyncio.to_thread(self._commit, records, plan.stale_ids,
                                plan.meta_updates,
                                [item] if item is not None else [],
                                plan.item_deletes)
        self.invalidate_caches(project)   # corpus + aliases changed -> rebuild lazily
        return IndexResult(relpath, changed=True, upserted=len(records),
                           deleted=len(plan.stale_ids), note_id=plan.note_id)

    def _commit(self, records: list[Record], stale_ids: list[str],
                meta_updates: dict[str, dict], items: list[Item] | None = None,
                item_deletes: list[str] | None = None) -> None:
        """The store writes as one worker-thread job and one transaction: a crash
        never leaves a note's new chunks beside its stale ones, or an item row
        that disagrees with them."""
        self.store.apply(records, stale_ids, meta_updates, items, item_deletes)

    async def _embed_item(self, plan: _Plan) -> Item | None:
        """The plan's item, with its title and body vectors computed if the plan
        says they changed (one embed call for both)."""
        if plan.item is None or plan.item_embed is None:
            return plan.item
        title_vec, body_vec = await asyncio.to_thread(
            embed_batch, self.embedder, list(plan.item_embed))
        return replace(plan.item, title_vec=title_vec, body_vec=body_vec)

    @staticmethod
    def _want_item(project: str, store: str, relpath: str, note_id: str,
                   title: str, body: str, frontmatter: dict) -> Item:
        """The item row (vectors not yet attached) a file's content implies."""
        kind = kind_of(store, relpath)
        return Item(id=note_id, kind=kind, project=project, relpath=relpath,
                    title=title, content_hash=item_hash(title, body),
                    facet=facet_of(kind, frontmatter, body),
                    edges=edges_of(kind, frontmatter, project))

    @staticmethod
    def _against(want: Item, have: Item | None) -> tuple[Item | None, bool]:
        """(the row to write, whether its vectors need computing), judged against
        the stored row: (None, False) when the stored row is already right."""
        if have is None or have.content_hash != want.content_hash:
            return want, True
        if replace(have, title_vec=None, body_vec=None) == want:
            return None, False
        return replace(want, title_vec=have.title_vec, body_vec=have.body_vec), False

    def _item_for(self, project: str, store: str, relpath: str, note_id: str,
                  title: str, body: str, frontmatter: dict) -> tuple[Item | None, tuple[str, str] | None]:
        """The item row this file should have, and whether its vectors need
        computing: (None, None) when the stored row is already right."""
        if not note_id:
            return None, None
        want = self._want_item(project, store, relpath, note_id, title, body,
                               frontmatter)
        item, needs = self._against(want, self.store.items({"id": note_id}).get(note_id))
        return item, ((title, body_text(body)) if needs else None)

    def item_rows(self, project: str, store: str,
                  files: list[tuple[str, dict, str]]) -> dict[str, Item]:
        """The item row each file implies, vectors attached, by relpath — for
        files as just parsed, `(relpath, frontmatter, body)` each; a file with
        no id yet has none. A row whose content matches the stored one keeps
        its vectors; the rest are embedded in one batch. Writes nothing: the
        facet hands the rows to the store (Store.replace_files)."""
        ids = [str(fm.get("id") or "") for _r, fm, _b in files]
        have = self.store.items({"id": {"$in": [i for i in ids if i]}}) if any(ids) else {}
        out: dict[str, Item] = {}
        embed: list[tuple[str, Item, str, str]] = []
        for relpath, fm, body in files:
            note_id = str(fm.get("id") or "")
            if not note_id:
                continue
            title = str(fm.get("title") or "")
            want = self._want_item(project, store, relpath, note_id, title, body, fm)
            item, needs = self._against(want, have.get(note_id))
            if item is None:
                out[relpath] = have[note_id]
            elif needs:
                embed.append((relpath, item, title, body_text(body)))
            else:
                out[relpath] = item
        if embed:
            vecs = embed_batch(self.embedder,
                               [t for *_, t, _b in embed] + [b for *_, b in embed])
            n = len(embed)
            for i, (relpath, it, _t, _b) in enumerate(embed):
                out[relpath] = replace(it, title_vec=vecs[i], body_vec=vecs[n + i])
        return out

    def _plan(self, project: str, notes_dir: Path, relpath: str,
              content_path: Path | None = None, store: str = "notes") -> _Plan:
        """Read the file, chunk it, diff against the index — all blocking, so this
        runs in a worker thread. Decides what (if anything) needs embedding."""
        path = content_path if content_path is not None else notes_dir / relpath

        # Deleted on disk -> drop all its chunks.
        if not path.exists():
            existing = self._existing_meta(project, store, relpath)
            gone = sorted({str(m.get("item_id")) for m in existing.values()
                           if m.get("item_id")})
            self.store.apply([], list(existing), {}, None, gone)
            return _Plan(
                invalidate=bool(existing),
                result=IndexResult(relpath, changed=bool(existing), upserted=0,
                                   deleted=len(existing)))

        # A source-anchored doc (content_path given) is READ-ONLY: the repo owns
        # it, so never heal/rewrite it or stamp an id into it — derive a stable id
        # from its relpath instead.
        read_only = content_path is not None
        rewrote = False
        if not read_only:
            rewrote = notes.heal_file(path)  # self-heal merge-duplicated frontmatter (§14)
        note = notes.load(path)
        if read_only:
            note_id = note.id or _derive_id(relpath)
        else:
            if notes.ensure_id(note):       # assign + persist a stable id
                notes.save_atomic(note)
                rewrote = True
            note_id = note.id or ""
        wrote = filesync.stamp(path) if rewrote else None
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

        item, item_embed = self._item_for(
            project, store, relpath, note_id, note.title or "", note.body,
            note.frontmatter)
        if not to_embed and not stale_ids and not meta_updates:
            return _Plan(note_id=note_id, item=item, item_embed=item_embed,
                         result=IndexResult(relpath, changed=False, upserted=0,
                                            deleted=0, note_id=note_id), wrote=wrote)

        return _Plan(
            note_id=note_id,
            to_embed=[(c, c.metadata(note.title, note.tags, source, mtime, note_type))
                      for c in to_embed],
            stale_ids=stale_ids, meta_updates=meta_updates,
            item=item, item_embed=item_embed, wrote=wrote)
