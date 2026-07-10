"""The code-index pipeline (extract → describe → persist), extracted from Crib.

`CodeIndexer` indexes one source file — or a whole project's source — into the
symbol_index via the warm LSP sessions, depending only on `CodeStore` (the index
state) and `ProjectServices` (the project-layer surface: refs, enumeration,
source-root registration, project resolution). It holds no reference to the Crib god
object. Crib keeps thin delegators so the notes watcher, the resident-cache
`revalidate` hook, and project setup/index call the pipeline unchanged.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import CribLink

if TYPE_CHECKING:
    from .project_services import ProjectServices


class CodeIndexer:
    def __init__(self, services: ProjectServices) -> None:
        self.services = services
        self.code = services.code          # CodeStore: index state + invariants
        self.paths = services.paths
        self.config = services.config
        # Set by Crib once the event loop exists (start_watchers). When present, the
        # live watch path DEFERS the LLM describe here instead of running it inline;
        # None → always describe inline (CLI/one-shot with no daemon, tests).
        self._describe_q: Any = None

    def set_describe_queue(self, q: Any) -> None:
        self._describe_q = q

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
        from .codeindex import find_root
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
        proj = self.services.resolve_project(project, cwd)
        return await asyncio.to_thread(self._index_code_file_tracked, root, rel, proj, patch_edges)

    def _index_code_file_tracked(self, root: Path, rel: str, proj: str,
                                 patch_edges: bool,
                                 existing: dict[str, dict] | None = None,
                                 describe_mode: str = "inline") -> dict[str, Any]:
        """Tracked entry point for one-file indexing: registers (proj, rel) as
        in-flight for `status`, then runs `_index_code_file`."""
        with self.code.indexing_lock:
            self.code.indexing.setdefault(proj, []).append(rel)
        try:
            return self._index_code_file(root, rel, proj, patch_edges, existing,
                                         describe_mode)
        finally:
            with self.code.indexing_lock:
                files = self.code.indexing.get(proj, [])
                if rel in files:
                    files.remove(rel)
                if not files:
                    self.code.indexing.pop(proj, None)

    def _index_code_file(self, root: Path, rel: str, proj: str,
                         patch_edges: bool,
                         existing: dict[str, dict] | None = None,
                         describe_mode: str = "inline") -> dict[str, Any]:
        """The blocking core of code_index — extract + describe + persist one file. Sync
        so the lazy revalidation path (also sync) can reuse it directly; code_index runs
        it off the event loop via to_thread. `existing` is the by-fqname snapshot of the
        prior index (for the content_hash gate + vanished-symbol drop); a full-project
        sweep parses it ONCE and passes it here so we don't re-`store.all()` per file
        (that made a cold onboard O(files × symbols)). None → parse it (standalone path)."""
        from .codeindex import (NoServer, SymbolIndex, describe_file,
                                 describe_symbols, extract_file, match_description)
        ref_ctx = self.services.ref_edge_ctx(proj, root)
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
        # DEFER (the live watch path): persist STRUCTURE now and hand the changed
        # symbols to the backoff queue — the LLM pass is coalesced off the save path
        # so an edit burst spends one focused describe, not one per keystroke-save.
        # Carry a still-valid description forward; BLANK a changed/new one, so
        # `content_hash present + empty description` is the durable "needs describing"
        # signal the startup backlog scan reconciles after a crash (docs § Deferred
        # describe). INLINE (cold onboard / explicit code_index): describe right here.
        defer = describe_mode == "defer" and self._describe_q is not None
        if defer:
            for sym in entries:
                ex = existing.get(sym["fqname"], {})
                sym["description"] = (
                    ex["description"]
                    if ex.get("content_hash") == sym["content_hash"] and ex.get("description")
                    else "")
        else:
            descs: dict[str, str] = {}
            if stale:
                try:
                    descs = describe_file(self.config.generate, root, rel)
                except Exception as exc:  # noqa: BLE001 — LLM down → structural-only
                    gen_error = str(exc)
            for sym in entries:
                ex = existing.get(sym["fqname"], {})
                if ex.get("content_hash") == sym["content_hash"] and ex.get("description"):
                    sym["description"] = ex["description"]       # cached, unchanged body
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
        with self.code.lock(proj):
            store.write_all(entries)
            store.set_source_root(root)                     # for query-time revalidation
            # drop symbols that vanished from this file (renamed/removed) — else orphan
            for fq in old_in_file - {e["fqname"] for e in entries}:
                store.delete(fq)
            if patch_edges:
                self.code.patch_called_by(store, entries, rel)
        self.services.register_code_root(proj, root)        # live-watch this repo's source
        self.code.bump_epoch(proj)                          # invalidate the resident cache
        if defer and stale:
            # Structure is durable; schedule the description pass. Bodies ride along
            # so the settle uses the focused describe_symbols over only what changed.
            self._describe_q.enqueue(proj, root, rel, {
                e["fqname"]: {"name": e["name"], "kind": e.get("kind", ""),
                              "content_hash": e["content_hash"],
                              "_body": e.get("_body", "")}
                for e in stale})
        out: dict[str, Any] = {
            "project": proj, "root": str(root), "file": rel,
            "symbols": len(entries),
            "described": sum(1 for e in entries if e["description"]),
            "store": str(store.root)}
        if defer and stale:
            out["describe_deferred"] = len(stale)
        if gen_error:
            out["descriptions_error"] = gen_error
        return out

    async def _describe_and_patch(self, proj: str, root: Path, rel: str,
                                  pending: dict[str, dict]) -> dict[str, Any]:
        """DescribeQueue callback: focused-describe the changed symbols of one settled
        file and patch their descriptions in. RAISES on LLM failure so the queue re-arms
        (backoff-as-retry). Clobber-guarded: re-reads each symbol and skips one whose
        body moved again since it was queued (a newer edit already re-queued it)."""
        from .codeindex import SymbolIndex, describe_symbols, match_description
        syms = list(pending.values())
        descs = await asyncio.to_thread(describe_symbols, self.config.generate, syms)
        if not descs:
            return {"described": 0, "file": rel}
        store = SymbolIndex(self.paths.project_dir(proj))
        patched = 0
        with self.code.lock(proj):
            for fq, sym in pending.items():
                cur = store.read(fq)
                if cur is None or cur.get("content_hash") != sym.get("content_hash"):
                    continue                            # dropped / re-edited → skip
                d = descs.get(sym.get("name", "")) or match_description(fq, descs)
                if d:
                    cur["description"] = d
                    store.write(cur)
                    patched += 1
        if patched:
            self.code.bump_epoch(proj)                  # queries now see fresh descriptions
        return {"described": patched, "file": rel}

    async def _index_project_code(self, proj: str, root: Path,
                                  globs: list[str]) -> dict[str, Any]:
        """Index every source file under `globs`. Non-code files self-skip (NoServer)."""
        from .codeindex import SymbolIndex
        files = self.services.enumerate_code_files(root, globs)
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
        extra_roots = [r["root"].resolve() for r in self.services.project_refs(proj)
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
