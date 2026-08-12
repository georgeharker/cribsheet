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
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import CribLink
from .errors import CribUserError

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
                raise CribUserError(
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
        from .codeindex import (
            SYMBOL_SCHEMA_VERSION,
            FileReadError,
            NoServer,
            SymbolIndex,
            describe_file,
            describe_symbols,
            extract_file,
            match_meta,
        )
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
        except FileReadError as exc:
            # The FILE is unreadable, not the server: skip this one and report it
            # (the sweep collects these into `skipped` and warns once). The warm
            # session is untouched — a single undecodable file used to cold-start
            # the whole language server, over and over.
            return {"project": proj, "root": str(root), "file": rel, "symbols": 0,
                    "skipped": str(exc), "skipped_kind": "unreadable"}
        # Semantic facet: LLM one-line descriptions, merged by fqname (§4).
        # content_hash GATE: reuse a cached description when the symbol's body is
        # unchanged; only call the LLM when something is stale/new. BEST-EFFORT: a
        # generation hiccup never loses the structural call graph (facets independent).
        store = SymbolIndex(self.paths.project_dir(proj))
        # `existing` is the full-project sweep's shared snapshot, so its absence is
        # what marks this call as a STANDALONE one — the watcher, the lazy
        # revalidation, an explicit code_index. Only a whole sweep may write into a
        # store holding another entry shape; one file into it would leave the project
        # half in each, which is the state nothing downstream can reason about.
        standalone = existing is None
        if existing is None:
            if store.schema_stale():
                raise CribUserError(
                    f"project {proj!r} was indexed at symbol schema "
                    f"{store.stored_schema()}, this crib writes "
                    f"{SYMBOL_SCHEMA_VERSION} — reindex it whole "
                    f"(`crib project index`, or project_index) before indexing a "
                    f"single file into it")
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
        # PARTIAL-extract guard — the empty guard's unguarded cousin. A server
        # answering mid-settle (esp. on the short warm-session settle) can return a
        # partial documentSymbol. Signature of partial: strictly FEWER symbols and
        # NOTHING new — a genuine edit that removes a symbol virtually always also
        # changes another (hash/line churn). One slow re-extract (settle=3.0, now
        # actually honored — it used to be clamped to 0.3s) recovers the full
        # listing, so the *reindex* is right rather than merely non-destructive;
        # `_deletion_allowed` is the hard guarantee behind it.
        fresh_fqns = {e["fqname"] for e in entries}
        if entries and old_in_file and len(fresh_fqns) < len(old_in_file) \
                and not (fresh_fqns - old_in_file):
            try:
                entries = extract_file(root, rel, settle=3.0, ref_projects=ref_ctx)
            except Exception:  # noqa: BLE001 — keep the fast read if the slow one fails
                pass
        stale = [e for e in entries
                 if existing.get(e["fqname"], {}).get("content_hash") != e["content_hash"]
                 or not existing.get(e["fqname"], {}).get("description")
                 # backfill kw facet: key PRESENCE is the "attempted" marker — a
                 # rendered `keywords = []` means the pass ran and yielded none
                 # (don't retry forever); a missing key means never attempted (legacy)
                 or "keywords" not in existing.get(e["fqname"], {})]
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
                if ex.get("content_hash") == sym["content_hash"] and ex.get("description"):
                    sym["description"] = ex["description"]
                    if "keywords" in ex:       # carry BOTH facets — write() replaces the
                        sym["keywords"] = ex["keywords"]   # whole entry, so an omitted
                else:                          # field is a clobber, not a no-op
                    sym["description"] = ""
        else:
            descs: dict[str, Any] = {}
            if stale:
                try:
                    descs = describe_file(self.config.generate, root, rel)
                except Exception as exc:  # noqa: BLE001 — LLM down → structural-only
                    gen_error = str(exc)
            for sym in entries:
                ex = existing.get(sym["fqname"], {})
                if ex.get("content_hash") == sym["content_hash"] and ex.get("description"):
                    sym["description"] = ex["description"]       # cached, unchanged body
                    if "keywords" in ex:                         # attempted (even if [])
                        sym["keywords"] = ex["keywords"]
                    else:                       # keyword-only backfill: NEVER blank the
                        d, kw = match_meta(sym["fqname"], descs)   # good description if
                        if d:                   # the pass failed / missed this symbol
                            sym["keywords"] = kw
                else:
                    desc, kw = match_meta(sym["fqname"], descs)
                    sym["description"] = desc
                    if desc:                    # covered by the pass → its keywords are
                        sym["keywords"] = kw    # authoritative, [] included
            # MOP-UP: symbols the whole-file bulk pass missed (low-yield / partial LLM
            # response) get a focused describe over just their bodies — far higher hit
            # rate on a small set. Best-effort; content_hash gate keeps future runs cheap.
            missed = [e for e in stale
                      if not e.get("description") or "keywords" not in e]
            if missed:
                try:
                    mop = describe_symbols(self.config.generate, missed)
                    for e in missed:
                        desc, kw = match_meta(e["fqname"], mop)
                        if desc:
                            e["description"] = desc
                            e["keywords"] = kw
                except Exception:  # noqa: BLE001 — mop-up is best-effort
                    pass
        # Serialize only the store read-modify-write (NOT the LSP/LLM work above),
        # so a concurrent reindex of another file — watcher vs query vs explicit
        # index — can't interleave writes and corrupt the cross-file call graph
        # (`CodeStore.patch_edges`). Kept off the slow describe path so the loop-thread
        # revalidation never blocks on a worker's LLM call.
        withheld: set[str] = set()
        with self.code.lock(proj):
            store.write_all(entries)
            store.set_source_root(root)                     # for query-time revalidation
            if standalone:
                # These entries are the current shape and the gate above proved the
                # store was not another one, so record it: an unstamped store
                # converges from ordinary use instead of waiting for a full sweep.
                # The sweep stamps once at the end rather than once per file.
                store.record_schema()
            # Drop symbols that vanished from this file (renamed/removed) — but ONLY
            # on evidence that the file actually changed. See `_deletion_allowed`.
            vanished = old_in_file - {e["fqname"] for e in entries}
            if vanished and self._deletion_allowed(existing, entries, rel):
                for fq in vanished:
                    store.delete(fq)
            elif vanished:
                withheld = self._withhold_deletions(store, vanished)
            if patch_edges:
                self.code.patch_edges(store, entries, rel)
        self.services.register_code_root(proj, root)        # live-watch this repo's source
        self.code.bump_epoch(proj)                          # invalidate the resident cache
        if defer and stale:
            # Structure is durable; schedule the description pass. Bodies ride along
            # so the settle uses the focused describe_symbols over only what changed.
            self._describe_q.enqueue(proj, root, rel, {
                e["fqname"]: {"fqname": e["fqname"],   # the describe blob labels by
                              "name": e["name"],      # fqname → results key by it
                              "kind": e.get("kind", ""),
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
        if withheld:
            out["deletions_withheld"] = sorted(withheld)
        if gen_error:
            out["descriptions_error"] = gen_error
        return out

    @staticmethod
    def _deletion_allowed(existing: dict[str, dict], entries: list[dict],
                          rel: str) -> bool:
        """May this reindex DELETE the symbols that disappeared from `rel`?

        Only if the file's bytes changed since it was last indexed. The reasoning is
        arithmetic, not heuristic: identical content cannot have lost a symbol, so a
        shrinking extract over an unchanged `file_hash` is an extraction anomaly (a
        server answering mid-settle, a wedged session) — never an edit. Gating on
        that makes wrongful deletion STRUCTURALLY impossible instead of
        timing-dependent; the settle/confirm machinery then only affects how quickly
        a legitimate removal lands, not whether live symbols survive.

        Unknown either way (an index written before `file_hash` existed, or an
        extract that produced no entries to carry one) → allowed, i.e. the older
        confirm-based behavior. We only ever ADD a reason not to delete."""
        prior = next((e.get("file_hash") for e in existing.values()
                      if e.get("file") == rel and e.get("file_hash")), "")
        fresh = next((e.get("file_hash") for e in entries if e.get("file_hash")), "")
        return not (prior and fresh and prior == fresh)

    @staticmethod
    def _withhold_deletions(store: Any, vanished: set[str]) -> set[str]:
        """Keep the symbols the extract lost and mark them MERGE-DIRTY (blank
        `content_hash`) instead. That is the store's existing "this record can't be
        trusted, rebuild it from the code" marker: `CodeStore.revalidate` and the
        post-pull reconcile both sweep for it, so the file is re-extracted and the
        symbols re-described on the next pass — self-healing, and visible in the
        result as `deletions_withheld` rather than a silent disappearance."""
        marked: set[str] = set()
        for fq in vanished:
            cur = store.read(fq)
            if cur is None or not cur.get("fqname"):
                continue            # gone, or too broken to rewrite — leave it be
            cur["content_hash"] = ""
            store.write(cur)
            marked.add(fq)
        return marked

    async def _describe_and_patch(self, proj: str, root: Path, rel: str,
                                  pending: dict[str, dict]) -> dict[str, Any]:
        """DescribeQueue callback: focused-describe the changed symbols of one settled
        file and patch their descriptions in. RAISES on LLM failure so the queue re-arms
        (backoff-as-retry). Clobber-guarded: re-reads each symbol and skips one whose
        body moved again since it was queued (a newer edit already re-queued it)."""
        from .codeindex import SymbolIndex, describe_symbols, match_meta
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
                # by FQNAME only: the blob labelled each block with it, so an exact
                # hit is expected. Matching on the bare `name` first is what let two
                # same-named methods in one file overwrite each other's description.
                d, kw = match_meta(fq, descs)
                if d:
                    cur["description"] = d
                    cur["keywords"] = kw    # [] included — marks the attempt durable
                    store.write(cur)
                    patched += 1
        if patched:
            self.code.bump_epoch(proj)                  # queries now see fresh descriptions
        return {"described": patched, "file": rel}

    async def _index_project_code(self, proj: str, root: Path, globs: list[str],
                                  budget_s: float | None = None) -> dict[str, Any]:
        """Index every source file under `globs`. Non-code files self-skip (NoServer).

        `budget_s` makes the sweep RESUMABLE: files not reached before the soft deadline
        are DEFERRED (not processed this call) and reported as `remaining` with
        `complete=False`, so a long reindex fits inside a bounded (e.g. MCP) call and the
        caller re-invokes to continue — the content-hash/keyword gate skips finished files."""
        from .codeindex import SymbolIndex
        files = self.services.enumerate_code_files(root, globs)
        loop = asyncio.get_event_loop()
        deadline = (loop.time() + budget_s) if budget_s else None
        deferred = 0
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
                # DEADLINE check after acquiring the slot: a file that queued past the
                # budget is deferred to the next call, not processed now.
                if deadline is not None and loop.time() > deadline:
                    return f, {"deferred": True}, None
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
        # Files the sweep could not READ (undecodable/vanished). Distinct from the
        # ordinary self-skip of a non-code file (no LSP server), which is expected
        # and silent — these are reported so a hole in the index is never invisible.
        skipped: list[dict[str, str]] = []
        with self.code.indexing_lock:
            self.code.sweeps[proj] = {"done": 0, "total": len(files)}
        try:
            for f, r, err in await asyncio.gather(*(_one(f) for f in files)):
                if (r or {}).get("deferred"):
                    deferred += 1
                elif err is not None:
                    errors.append({"file": str(f), "error": err})
                elif (r or {}).get("skipped"):
                    if (r or {}).get("skipped_kind") == "unreadable":
                        skipped.append({"file": str(f), "error": r["skipped"]})
                else:
                    indexed += 1
                    syms += (r or {}).get("symbols", 0)
                    desc += (r or {}).get("described", 0)
        finally:
            with self.code.indexing_lock:
                self.code.sweeps.pop(proj, None)
            if pins:
                await asyncio.to_thread(_POOL.unpin, root)
        out: dict[str, Any] = {"files_indexed": indexed, "files_seen": len(files),
                               "symbols": syms, "described": desc,
                               "complete": deferred == 0, "remaining": deferred}
        if deferred == 0:
            # Stamp only a sweep that reached every file. A run cut short by the
            # budget leaves part of the project at the old shape, and claiming
            # otherwise is what would let an incremental write in on top of it.
            SymbolIndex(self.paths.project_dir(proj)).record_schema()
        if errors:
            out["errors"] = errors
        if skipped:
            out["skipped"] = skipped
            print(f"[crib] code index {proj}: {len(skipped)} unreadable file(s) "
                  f"skipped (first: {skipped[0]['file']})", file=sys.stderr)
        return out
