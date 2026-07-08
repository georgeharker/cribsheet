"""Durable human learnings attached to code symbols, extracted from Crib.

A learning is a first-class NOTE under `<project>/code-learnings/`, keyed by the
symbol's slugged fqn — deliberately separate from the regenerable LLM description, so
pinned understanding survives re-indexing and rides git sync. `Learnings` is the CRUD
+ audit + rehome over those notes. It depends on three clean things: `paths` (to read
the symbol index for audit/rehome), `refs` (to resolve a symbol, falling through to
cross-project refs), and `notestore` (the note file ops — write/delete/read/reindex).
Cores take an explicit resolved `project`; Crib keeps resolve_project + delegate.
"""

from __future__ import annotations

import datetime
import re
from typing import TYPE_CHECKING, Any

from . import notes
from .notes import Note

if TYPE_CHECKING:
    from .notestore import NoteStore
    from .paths import Paths
    from .refs import Refs


class Learnings:
    def __init__(self, paths: Paths, refs: Refs, notestore: NoteStore) -> None:
        self.paths = paths
        self.refs = refs
        self.notestore = notestore

    def relpath(self, entry: dict[str, Any]) -> str:
        from .codeindex import LEARNINGS_DIR, learning_slug
        return f"{LEARNINGS_DIR}/{learning_slug(entry['fqname'])}.md"

    def attach(self, proj: str,
               entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Enrich symbol entries in place with any attached learning (📌) + a staleness
        flag, so pinned understanding resurfaces exactly where you're already looking
        (code_lookup / code_xref). Keyed O(1) by learning_slug(fqn). `stale` = the
        symbol's body changed (content_hash) since the learning was written — a heads-up,
        not an invalidation."""
        from .codeindex import LEARNINGS_DIR, learning_slug
        ldir = self.notestore.dir(proj) / LEARNINGS_DIR
        if not ldir.exists():
            return entries
        for e in entries:
            fq = e.get("fqname")
            if not fq:
                continue
            relpath = f"{LEARNINGS_DIR}/{learning_slug(fq)}.md"
            path = self.notestore.dir(proj) / relpath
            if not path.exists():
                continue
            note = notes.load(path)
            wrote, cur = note.frontmatter.get("content_hash"), e.get("content_hash")
            e["learning"] = {"relpath": relpath, "path": str(path),
                             "stale": bool(wrote and cur and wrote != cur),
                             "body": note.body.strip()}
        return entries

    def fqns(self, proj: str) -> set[str]:
        """Set of fqns that carry a learning (read from `symbol:` frontmatter — the
        authoritative fqn, so the lossy slug never has to be reversed)."""
        from .codeindex import LEARNINGS_DIR
        ldir = self.notestore.dir(proj) / LEARNINGS_DIR
        out: set[str] = set()
        if ldir.exists():
            for p in ldir.glob("*.md"):
                fq = notes.load(p).frontmatter.get("symbol")
                if fq:
                    out.add(fq)
        return out

    async def append(self, proj: str, symbol: str, text: str) -> dict[str, Any]:
        """Attach a durable learning to a symbol: append a dated entry to its running
        note (create it, with symbol-keyed frontmatter, on first use)."""
        entry = self.refs.resolve_symbol(proj, symbol)
        fqn = entry["fqname"]
        relpath = self.relpath(entry)
        path = self.notestore.abspath(proj, relpath)
        existed = path.exists()
        if existed:
            note = notes.load(path)
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
        res = await self.notestore.write(proj, relpath, note)
        return {"project": proj, "symbol": fqn, "relpath": relpath,
                "created": not existed, "indexed": res.upserted}

    async def edit(self, proj: str, symbol: str, new_content: str) -> dict[str, Any]:
        """Replace a symbol's learning body wholesale (fix/rewrite), frontmatter
        preserved. Errors if no learning exists yet — use append to create."""
        entry = self.refs.resolve_symbol(proj, symbol)
        relpath = self.relpath(entry)
        path = self.notestore.abspath(proj, relpath)
        if not path.exists():
            raise ValueError(f"no learning for {entry['fqname']!r} yet — code_append first")
        note = notes.load(path)
        note.frontmatter["content_hash"] = entry.get("content_hash", "")
        note.body = new_content.strip() + "\n"
        res = await self.notestore.write(proj, relpath, note)
        return {"project": proj, "symbol": entry["fqname"], "relpath": relpath,
                "indexed": res.upserted}

    async def forget(self, proj: str, symbol: str) -> dict[str, Any]:
        """Remove a symbol's learning (stashed to the version ring first, recoverable).
        Works on ORPHANS: if the symbol no longer resolves, forget by its recorded fqn."""
        from .codeindex import LEARNINGS_DIR, learning_slug
        try:
            fqn = self.refs.resolve_symbol(proj, symbol)["fqname"]
        except ValueError:
            fqn = symbol                      # orphan: gone from the index, note lingers
        relpath = f"{LEARNINGS_DIR}/{learning_slug(fqn)}.md"
        if not self.notestore.abspath(proj, relpath).exists():
            raise ValueError(f"no learning for {symbol!r} in project {proj!r}")
        res = await self.notestore.delete(proj, relpath)
        return {**res, "symbol": fqn}

    async def reaffirm(self, proj: str, symbol: str) -> dict[str, Any]:
        """Clear a learning's ⚠ stale flag WITHOUT editing the body — you re-checked it
        and it still holds. Re-snapshots content_hash/file/signature and stamps
        `reaffirmed`."""
        entry = self.refs.resolve_symbol(proj, symbol)
        relpath = self.relpath(entry)
        path = self.notestore.abspath(proj, relpath)
        if not path.exists():
            raise ValueError(f"no learning for {entry['fqname']!r} yet — code_append first")
        note = notes.load(path)
        note.frontmatter["content_hash"] = entry.get("content_hash", "")
        note.frontmatter["file"] = entry.get("file", note.frontmatter.get("file", ""))
        note.frontmatter["signature"] = entry.get("signature",
                                                  note.frontmatter.get("signature", ""))
        note.frontmatter["reaffirmed"] = datetime.date.today().isoformat()
        res = await self.notestore.write(proj, relpath, note)
        return {"project": proj, "symbol": entry["fqname"], "relpath": relpath,
                "reaffirmed": note.frontmatter["reaffirmed"], "indexed": res.upserted}

    def report(self, proj: str, orphans_only: bool = False) -> list[dict[str, Any]]:
        """Health of every attached learning: `ok` | `moved` | `orphan`. `moved` = the
        fqn still resolves but the symbol's file drifted from the snapshot; `orphan` =
        the fqn no longer resolves. Report-only — drives cleanup (rehome / forget)."""
        from .codeindex import LEARNINGS_DIR, SymbolIndex
        ldir = self.notestore.dir(proj) / LEARNINGS_DIR
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

    def candidates(self, fm: dict[str, Any], entries: list[dict[str, Any]],
                   top: int = 6) -> list[dict[str, Any]]:
        """Rank index symbols as rehome targets for an orphaned learning from the
        snapshot we kept — unqualified name, signature token overlap, same file.
        Structural only; the human/LLM confirms."""
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

    async def rehome(self, proj: str, old_fqn: str,
                     new_fqn: str | None = None) -> dict[str, Any]:
        """Re-point an orphaned learning at the symbol it became. Without `new_fqn`:
        ranked candidates (never auto-move). With `new_fqn`: move the note to the new
        symbol's slug, re-snapshot frontmatter, preserve the note id/history."""
        from .codeindex import LEARNINGS_DIR, SymbolIndex, learning_slug
        old_rel = f"{LEARNINGS_DIR}/{learning_slug(old_fqn)}.md"
        old_path = self.notestore.abspath(proj, old_rel)
        if not old_path.exists():
            raise ValueError(f"no learning for {old_fqn!r} in project {proj!r}")
        entries = SymbolIndex(self.paths.project_dir(proj)).all()
        if new_fqn is None:
            fm = notes.load(old_path).frontmatter
            return {"old": old_fqn, "relpath": old_rel,
                    "candidates": self.candidates(fm, entries)}
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
        res = await self.notestore.write(proj, new_rel, note)   # id preserved
        if new_rel != old_rel:
            old_path.unlink()
            await self.notestore.reindex(proj, old_rel)     # drop the old note's chunks
        return {"project": proj, "old": old_fqn, "new": new_entry["fqname"],
                "relpath": new_rel, "indexed": res.upserted}

    def read(self, proj: str, symbol: str) -> dict[str, Any]:
        """Read a symbol's learning note (frontmatter + body), or found=False if unwritten."""
        entry = self.refs.resolve_symbol(proj, symbol)
        relpath = self.relpath(entry)
        path = self.notestore.abspath(proj, relpath)
        if not path.exists():
            return {"project": proj, "symbol": entry["fqname"], "relpath": relpath,
                    "path": str(path), "found": False, "body": None}
        note = notes.load(path)
        return {"project": proj, "symbol": entry["fqname"], "relpath": relpath,
                "path": str(path), "found": True,
                "frontmatter": note.frontmatter, "body": note.body}
