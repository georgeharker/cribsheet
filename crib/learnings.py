"""Durable human learnings attached to code symbols, extracted from Crib.

A learning is a first-class note in the LEARNINGS pillar store
(`projects/<p>/learnings/<slug>.md`), bound to its symbol's `symbol_ref` —
deliberately separate from the regenerable LLM description, so pinned
understanding survives re-indexing and rides git sync, and deliberately NOT in
the notes pillar, so it never surfaces in (or re-weights) note search.
`Learnings` is the CRUD + audit + rehome over those notes. It depends on three
clean things: `paths` (to read the symbol index for audit/rehome), `refs` (to
resolve a symbol, falling through to cross-project refs), and `store` (the
learnings pillar's note file ops — write/delete/read/reindex).
Cores take an explicit resolved `project`; Crib keeps resolve_project + delegate.
"""

from __future__ import annotations

import datetime
import re
from typing import TYPE_CHECKING, Any

from . import notes
from .errors import CribUserError
from .notes import Note
from .refs import UnknownSymbol, resolution
from .symbols import bindings as symbol_bindings
from .symbols import fqn_of
from .symbols import id_parts
from .symbols import key as symbol_key
from .symbols import ref_slug, legacy_ref_slug
from .symbols import match_entry
from .symbols import tail as _tail

if TYPE_CHECKING:
    from .notestore import NoteStore
    from .paths import Paths
    from .refs import Refs


class Learnings:
    def __init__(self, paths: Paths, refs: Refs, store: NoteStore) -> None:
        self.paths = paths
        self.refs = refs
        # The learnings PILLAR store (`projects/<p>/learnings/`), not the notes
        # one — learnings share the store implementation, never the notes tree.
        self.store = store

    def notes_by_binding(self, proj: str) -> dict[str, str]:
        """binding → note relpath, read from FRONTMATTER.

        The join is built from what each note SAYS it is about, never from its
        filename. A filename is a slug of one spelling, so looking a note up by
        guessing its name means guessing which spelling it was written under — and
        that guess is what `forget` got wrong while its four siblings got it right.
        There is one note per symbol and a handful per project, so reading them is
        cheaper than the bug."""
        out: dict[str, str] = {}
        root = self.store.root(proj)
        if not root.exists():
            return out
        for p in sorted(root.glob("*.md")):
            fm = notes.load(p).frontmatter
            bound = fm.get("symbol_ref") or fm.get("symbol")
            if bound:
                out.setdefault(str(bound), p.name)
            for was in fm.get("symbol_was") or ():
                out.setdefault(str(was), p.name)
        return out

    def rel_for_binding(self, proj: str, binding: str) -> str | None:
        """The note bound to this exact spelling, or None. For an ORPHAN, whose
        symbol no longer resolves, so there is no entry to ask."""
        rel = self.notes_by_binding(proj).get(binding)
        if rel:
            return rel
        # A note written before the case-hash slug keeps its old filename — those are
        # deliberately never renamed, since a human may have the path in hand.
        for cand in (f"{ref_slug(binding)}.md",
                     f"{legacy_ref_slug(binding)}.md"):
            if self.store.abspath(proj, cand).exists():
                return cand
        return None

    def relpath(self, proj: str, entry: dict[str, Any]) -> str:
        """THE learning-note relpath for a symbol — the ONE function every verb uses.

        An existing note bound to ANY spelling this entry answers to, else the
        canonical name for one that does not exist yet. `bindings` owns the list of
        spellings, so this cannot disagree with the rest of the system about what a
        symbol is called, and a note stays found across the id change without being
        moved."""
        by_binding = self.notes_by_binding(proj)
        for b in symbol_bindings(entry):
            rel = by_binding.get(b)
            if rel:
                return rel
            for cand in (f"{ref_slug(b)}.md", f"{legacy_ref_slug(b)}.md"):
                if self.store.abspath(proj, cand).exists():
                    return cand
        return f"{ref_slug(symbol_key(entry))}.md"

    def attach(self, proj: str,
               entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Enrich symbol entries in place with any attached learning (※) + a staleness
        flag, so pinned understanding resurfaces exactly where you're already looking
        (code_lookup / code_xref), joined by BINDING via frontmatter. `stale` = the
        symbol's body changed (content_hash) since the learning was written — a heads-up,
        not an invalidation."""
        ldir = self.store.root(proj)
        if not ldir.exists():
            return entries
        by_binding = self.notes_by_binding(proj)      # read ONCE, not per entry
        for e in entries:
            rel = next((by_binding[b] for b in symbol_bindings(e) if b in by_binding),
                       None)
            relpath = rel if rel is not None else self.relpath(proj, e)
            path = ldir / relpath
            if not path.exists():
                continue
            note = notes.load(path)
            wrote, cur = note.frontmatter.get("content_hash"), e.get("content_hash")
            e["learning"] = {"relpath": relpath, "path": str(path),
                             "stale": bool(wrote and cur and wrote != cur),
                             "body": note.body.strip()}
        return entries

    def marks(self, proj: str) -> set[str]:
        """The KEY of every symbol that carries a learning — for the ※ glyph.

        Resolved through the index, because the two sides of the join hold
        different spellings: a graph node carries only its `symbol_ref`, while a
        note may be bound to any spelling its symbol ever had. Meeting at the KEY
        makes the comparison one string against one string — comparing a node's
        name to a set of note bindings is exactly the mismatch that killed the
        glyph on every migrated project, with both halves individually correct.

        A binding that resolves to NO entry passes through raw: its symbol is
        gone, but an orphan should still show the mark if something renders it."""
        from .codeindex import SymbolIndex
        by_binding: dict[str, str] = {}
        for e in SymbolIndex(self.paths.project_dir(proj)).all():
            k = symbol_key(e)
            for b in symbol_bindings(e):
                by_binding.setdefault(b, k)
        ldir = self.store.root(proj)
        out: set[str] = set()
        if ldir.exists():
            for p in ldir.glob("*.md"):
                _fm = notes.load(p).frontmatter
                for b in (_fm.get("symbol_ref"), _fm.get("symbol"),
                          *(_fm.get("symbol_was") or ())):
                    if b:
                        out.add(by_binding.get(str(b), str(b)))
        return out

    async def append(self, proj: str, symbol: str, text: str) -> dict[str, Any]:
        """Attach a durable learning to a symbol: append a dated entry to its running
        note (create it, with symbol-keyed frontmatter, on first use)."""
        entry = self.refs.resolve_symbol(proj, symbol)
        # BIND to the reference, TITLE with the language's own name. A note created
        # from here is already on the current binding, so the migration surface
        # stops growing while the rollout is under way.
        ref = symbol_key(entry)
        fqn = str(entry.get("fqn") or ref)
        relpath = self.relpath(proj, entry)
        path = self.store.abspath(proj, relpath)
        existed = path.exists()
        if existed:
            note = notes.load(path)
            note.frontmatter["content_hash"] = entry.get("content_hash", "")
            note.frontmatter["file"] = entry.get("file", note.frontmatter.get("file", ""))
            note.frontmatter["signature"] = entry.get("signature",
                                                      note.frontmatter.get("signature", ""))
            note.frontmatter["title"] = fqn        # tracks the name, never goes stale
        else:
            # `symbol_was` snapshots the entry's PRIOR bindings at authoring, like
            # `file` and `signature` snapshot its location. While the symbol lives,
            # the join resolves through the entry and this is redundant — it earns
            # its keep when the symbol DIES: an orphan's note must still answer to
            # the name a human has in hand, and by then the entry is gone.
            note = Note(path=path, body="", frontmatter={
                "title": fqn, "kind": "code-learning", "symbol_ref": ref,
                **({"symbol_was": symbol_bindings(entry)[1:]}
                   if symbol_bindings(entry)[1:] else {}),
                "lang": entry.get("lang", ""), "file": entry.get("file", ""),
                "signature": entry.get("signature", ""),
                "content_hash": entry.get("content_hash", ""),
                "source": "code-note"})
        today = datetime.date.today().isoformat()
        note.body = note.body.rstrip() + f"\n\n### {today}\n{text.strip()}\n"
        res = await self.store.write(proj, relpath, note)
        return {"project": proj, "symbol": ref, "relpath": relpath,
                "resolved": resolution(entry, symbol),
                "created": not existed, "indexed": res.upserted}

    async def edit(self, proj: str, symbol: str, new_content: str) -> dict[str, Any]:
        """Replace a symbol's learning body wholesale (fix/rewrite), frontmatter
        preserved. Errors if no learning exists yet — use append to create."""
        entry = self.refs.resolve_symbol(proj, symbol)
        relpath = self.relpath(proj, entry)
        path = self.store.abspath(proj, relpath)
        if not path.exists():
            raise CribUserError(f"no learning for {symbol_key(entry)!r} yet — learning_add first")
        note = notes.load(path)
        note.frontmatter["content_hash"] = entry.get("content_hash", "")
        note.body = new_content.strip() + "\n"
        res = await self.store.write(proj, relpath, note)
        return {"project": proj, "symbol": symbol_key(entry), "relpath": relpath,
                "resolved": resolution(entry, symbol),
                "indexed": res.upserted}

    async def forget(self, proj: str, symbol: str) -> dict[str, Any]:
        """Remove a symbol's learning (stashed to the version ring first, recoverable).
        Works on ORPHANS: if the symbol no longer resolves, forget by its binding.

        Goes through `relpath` like every other verb. It used to derive its own path
        from `entry["fqname"]` while its four siblings went through the shared one —
        so once a note was bound to a reference, `learning_forget` could not find it
        and reported it as absent. A learning that cannot be deleted is worse than
        one that cannot be found, because the note is right there."""
        resolved: dict[str, Any] | None = None
        try:
            entry = self.refs.resolve_symbol(proj, symbol)
            bound = symbol_key(entry)
            resolved = resolution(entry, symbol)
            relpath: str | None = self.relpath(proj, entry)
        except UnknownSymbol:
            # orphan: gone from the index, note lingers — look it up by the literal
            # spelling the caller gave, which is all there is left to go on
            bound = symbol
            relpath = self.rel_for_binding(proj, symbol)
        if relpath is None or not self.store.abspath(proj, relpath).exists():
            raise CribUserError(f"no learning for {symbol!r} in project {proj!r}")
        res = await self.store.delete(proj, relpath)
        return {**res, "symbol": bound,
                **({"resolved": resolved} if resolved else {})}

    async def reaffirm(self, proj: str, symbol: str) -> dict[str, Any]:
        """Clear a learning's ⚠︎ stale flag WITHOUT editing the body — you re-checked it
        and it still holds. Re-snapshots content_hash/file/signature and stamps
        `reaffirmed`."""
        entry = self.refs.resolve_symbol(proj, symbol)
        relpath = self.relpath(proj, entry)
        path = self.store.abspath(proj, relpath)
        if not path.exists():
            raise CribUserError(f"no learning for {symbol_key(entry)!r} yet — learning_add first")
        note = notes.load(path)
        note.frontmatter["content_hash"] = entry.get("content_hash", "")
        note.frontmatter["file"] = entry.get("file", note.frontmatter.get("file", ""))
        note.frontmatter["signature"] = entry.get("signature",
                                                  note.frontmatter.get("signature", ""))
        note.frontmatter["reaffirmed"] = datetime.date.today().isoformat()
        res = await self.store.write(proj, relpath, note)
        return {"project": proj, "symbol": symbol_key(entry), "relpath": relpath,
                "resolved": resolution(entry, symbol),
                "reaffirmed": note.frontmatter["reaffirmed"], "indexed": res.upserted}

    async def convert_notes(self, proj: str, apply: bool = False) -> dict[str, Any]:
        """Rebind every learning note to its symbol's CURRENT identity, per record.

        The note-side twin of the entry conversion, and the same shape: each note is
        independently classifiable from its own frontmatter plus the index, so there
        is no plan to persist, no resume marker, and re-running IS the resume.

            done                              -> noop
            binding answers to a live symbol  -> CONVERT (rebind + canonical rename)
            binding answers to nothing        -> ORPHAN  (report; rehome repairs)

        There is NO "pending" state, and that is the point of `key()` being
        derivable: an unconverted entry still has its reference, so a note can be
        rebound against a store the entry conversion has not reached yet — the entry
        will land on the same key when its turn comes. The old sweep had to refuse a
        half-converted store precisely because it read `symbol_ref` off the entry
        and treated absence as death.

        DRY RUN by default — it renames files under a git-synced store. Writes go
        THROUGH NoteStore, so the chunk index moves with the file (the old sweep
        bypassed it with a raw save, leaving every migrated note invisible to
        `note_lookup` until some later reindex). Two notes claiming one symbol:
        skip and report, never merge — the bodies are hand-written. The manifest is
        written AFTER the work, describing what happened rather than what was
        intended."""
        import hashlib
        import json
        from .codeindex import SymbolIndex
        entries = SymbolIndex(self.paths.project_dir(proj)).all()
        by_binding: dict[str, dict[str, Any]] = {}
        for e in entries:
            for b in symbol_bindings(e):
                by_binding.setdefault(b, e)
        ldir = self.store.root(proj)
        rows: dict[str, list[dict[str, Any]]] = {"noop": [], "convert": [],
                                                 "orphan": [], "collision": []}
        detail: list[dict[str, Any]] = []
        claimed: dict[str, str] = {}
        for path in (sorted(ldir.glob("*.md")) if ldir.exists() else []):
            note = notes.load(path)
            fm = note.frontmatter
            bound = str(fm.get("symbol_ref") or fm.get("symbol") or "")
            entry = by_binding.get(bound)
            detail.append({"relpath": path.name, "binding": bound,
                           "id": fm.get("id"),
                           "body_sha1": hashlib.sha1(note.body.encode()).hexdigest()})
            if entry is None:
                rows["orphan"].append({"relpath": path.name, "binding": bound})
                continue
            ref = symbol_key(entry)
            target = f"{ref_slug(ref)}.md"
            if fm.get("symbol_ref") == ref and "symbol" not in fm \
                    and path.name == target:
                rows["noop"].append({"relpath": path.name, "binding": bound})
                continue
            prior = claimed.get(ref)
            tpath = self.store.abspath(proj, target)
            if prior is not None or (tpath.exists() and tpath != path
                                     and notes.load(tpath).frontmatter.get("id")
                                     != fm.get("id")):
                # two hand-written notes for one symbol: both kept, said out loud
                rows["collision"].append({"relpath": path.name, "binding": bound,
                                          "target": target,
                                          "held_by": prior or target})
                continue
            claimed[ref] = path.name
            rows["convert"].append({"relpath": path.name, "binding": bound,
                                    "new_relpath": target, "symbol_ref": ref})
        applied = 0
        if apply:
            for row in rows["convert"]:
                src = ldir / str(row["relpath"])
                note = notes.load(src)
                fm = note.frontmatter
                was = str(fm.get("symbol_ref") or fm.get("symbol") or "")
                fm["symbol_ref"] = row["symbol_ref"]
                fm.pop("symbol", None)     # the field is renamed, not duplicated
                # HISTORIED, not replaced: a list, because this will not be the last
                # identity change and a scalar loses the first one
                if was and was != row["symbol_ref"]:
                    fm["symbol_was"] = [*(fm.get("symbol_was") or []), was]
                fm["title"] = fqn_of(by_binding[was])
                rel = str(row["new_relpath"])
                await self.store.write(proj, rel, note)
                if rel != row["relpath"]:
                    src.unlink(missing_ok=True)
                    await self.store.reindex(proj, str(row["relpath"]))
                applied += 1
        counts = {k: len(v) for k, v in rows.items()}
        # counts under the state names; the ACTIONABLE lists under their own keys
        # (an orphan wants a rehome, a collision wants a human merge)
        out: dict[str, Any] = {"project": proj, "applied": apply, **counts,
                               **({"orphans": rows["orphan"]} if rows["orphan"]
                                  else {}),
                               **({"collisions": rows["collision"]}
                                  if rows["collision"] else {})}
        if apply:
            d = self.paths.project_dir(proj) / "migrations"
            d.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
            mpath = d / f"learning-convert-{stamp}.json"
            mpath.write_text(json.dumps({**out, "notes_detail": detail},
                                        indent=1, sort_keys=True, default=str))
            out["manifest"] = str(mpath)
        return out

    def report(self, proj: str, orphans_only: bool = False) -> list[dict[str, Any]]:
        """Health of every attached learning: `ok` | `moved` | `orphan`. `moved` = the
        fqn still resolves but the symbol's file drifted from the snapshot; `orphan` =
        the fqn no longer resolves. Report-only — drives cleanup (rehome / forget)."""
        from .codeindex import SymbolIndex
        ldir = self.store.root(proj)
        # keyed by BOTH bindings: a note may be on the current id or on the legacy
        # qualified name, and a report that only knows one calls the other an orphan
        by_fq: dict[str, dict[str, Any]] = {}
        for e in SymbolIndex(self.paths.project_dir(proj)).all():
            for b in symbol_bindings(e):
                by_fq.setdefault(b, e)
            for k in ("symbol_ref", "fqn"):
                if e.get(k):
                    by_fq[str(e[k])] = e
        out: list[dict[str, Any]] = []
        if ldir.exists():
            for p in sorted(ldir.glob("*.md")):
                fm = notes.load(p).frontmatter
                fq = fm.get("symbol_ref") or fm.get("symbol", "")
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
                            "relpath": p.name})
        return out

    def candidates(self, fm: dict[str, Any], entries: list[dict[str, Any]],
                   top: int = 6) -> list[dict[str, Any]]:
        """Rank index symbols as rehome targets for an orphaned learning from the
        snapshot we kept — unqualified name, signature token overlap, same file.
        Structural only; the human/LLM confirms.

        Reads the binding through the same accessor every other verb uses. Reading
        `symbol` alone silently scored every migrated note against an EMPTY name,
        because the rebind renames that field to `symbol_ref` rather than keeping
        both — so the ranking still returned six plausible-looking candidates with
        the strongest signal switched off."""
        bound = str(fm.get("symbol_ref") or fm.get("symbol") or "")
        # the bare leaf, from either spelling: a reference ends at its tail, a
        # qualified name at its last segment
        oldname = _tail(id_parts(bound)[1])   # no-# input passes through id_parts
        oldfile = fm.get("file", "")
        oldsig = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", fm.get("signature", "")))
        scored: list[tuple[float, dict[str, Any]]] = []
        for e in entries:
            if bound and bound in symbol_bindings(e):
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
        return [{"symbol_ref": symbol_key(e), "fqn": e.get("fqn", ""),
                 "file": e.get("file", ""),
                 "signature": e.get("signature", ""), "score": round(s, 2)}
                for s, e in scored[:top]]

    async def rehome(self, proj: str, old_fqn: str,
                     new_fqn: str | None = None) -> dict[str, Any]:
        """Re-point an orphaned learning at the symbol it became. Without `new_fqn`:
        ranked candidates (never auto-move). With `new_fqn`: move the note to the new
        symbol's slug, re-snapshot frontmatter, preserve the note id/history."""
        from .codeindex import SymbolIndex
        # `old_fqn` is whatever spelling the caller has — a reference, a legacy
        # qualified name, or an orphan's recorded binding — so look it up by binding
        # rather than by deriving a filename from it.
        old_rel = self.rel_for_binding(proj, old_fqn)
        if old_rel is None:
            raise CribUserError(f"no learning for {old_fqn!r} in project {proj!r}")
        old_path = self.store.abspath(proj, old_rel)
        if not old_path.exists():
            raise CribUserError(f"no learning for {old_fqn!r} in project {proj!r}")
        entries = SymbolIndex(self.paths.project_dir(proj)).all()
        if new_fqn is None:
            fm = notes.load(old_path).frontmatter
            return {"old": old_fqn, "relpath": old_rel,
                    "candidates": self.candidates(fm, entries)}
        new_entry = next((e for e in entries if new_fqn in symbol_bindings(e)), None)
        if new_entry is None:                               # allow a unique bare name
            m = [e for e in entries if match_entry(e, new_fqn)]
            if len(m) != 1:
                raise CribUserError(f"target {new_fqn!r} not found or not unique in index")
            new_entry = m[0]
        new_rel = self.relpath(proj, new_entry)
        # A rehome must never CLOBBER: the target symbol may already carry its own
        # learning, and writing over it would destroy hand-written understanding
        # with no prompt (the ring keeps the bytes, but nothing would say so).
        # Refuse like `NoteStore.move` does and let the human merge or forget one.
        if new_rel != old_rel and self.store.abspath(proj, new_rel).exists():
            raise CribUserError(
                f"{symbol_key(new_entry)!r} already has a learning ({new_rel}) — "
                f"read both and merge with learning_edit, or learning_forget one "
                f"first; rehome refuses to overwrite")
        note = notes.load(old_path)
        note.frontmatter.pop("symbol", None)   # rebind, never leave two bindings
        note.frontmatter.update({
            "symbol_ref": symbol_key(new_entry),
            "title": str(new_entry.get("fqn") or symbol_key(new_entry)),
            "lang": new_entry.get("lang", ""), "file": new_entry.get("file", ""),
            "signature": new_entry.get("signature", ""),
            "content_hash": new_entry.get("content_hash", ""), "rehomed_from": old_fqn})
        res = await self.store.write(proj, new_rel, note)   # id preserved
        if new_rel != old_rel:
            old_path.unlink()
            await self.store.reindex(proj, old_rel)     # drop the old note's chunks
        return {"project": proj, "old": old_fqn, "new": symbol_key(new_entry),
                "relpath": new_rel, "indexed": res.upserted}

    def read(self, proj: str, symbol: str) -> dict[str, Any]:
        """Read a symbol's learning note (frontmatter + body), or found=False if unwritten."""
        entry = self.refs.resolve_symbol(proj, symbol)
        relpath = self.relpath(proj, entry)
        path = self.store.abspath(proj, relpath)
        if not path.exists():
            return {"project": proj, "symbol": symbol_key(entry), "relpath": relpath,
                    "resolved": resolution(entry, symbol),
                    "path": str(path), "found": False, "body": None}
        note = notes.load(path)
        return {"project": proj, "symbol": symbol_key(entry), "relpath": relpath,
                "resolved": resolution(entry, symbol),
                "path": str(path), "found": True,
                "frontmatter": note.frontmatter, "body": note.body}
