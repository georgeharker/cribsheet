"""Crib — the core service. Implements the tool verbs (DESIGN §5).

Both the MCP server and the CLI call into this; tests exercise it directly. All
writes go through `_write_note` so every mutation stashes a version and funnels
through the one hash-gated `index_file`.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from . import __version__, claudemem, notes
from .chunk import CHUNK_SCHEMA_VERSION, section_key, section_line_map
from .claudemem import MemoryBindings
from .codeindexer import CodeIndexer
from .codequery import CodeQuery
from .codestore import CodeStore, _ResidentCode
from .codestore import format_sweep
from .config import Config, CribLink, ProjectConfig, portable_path, resolve_project
from .designs import Designs
from .embed import build_embedder, embed_batch, embed_query_batch
from .errors import CribUserError
from .gitbacking import GitBacking
from .indexer import IndexEngine, IndexResult
from .learnings import Learnings
from .notes import Note, NoteParseError
from .notestore import DESIGN_SPEC, LEARNINGS_SPEC, PLANS_SPEC, NoteStore
from .paths import Paths, ProjectPathResolver, ProjectPaths, check_project_name, confine
from .project_services import ProjectServices
from .refs import Refs
from .sources import SRC_PREFIX, SourceRoots, src_relpath
from .store import Hit, Store
from .util import derived_ulid
from .symbols import fqn_of as _symbol_fqn
from .symbols import key as _symbol_key
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


# The polling interval for bounded JOINS inside a kick/wait sweep — the poll
# contract's own cadence: a join checks the sweep every interval until it is
# done. Lives HERE (app), not server (server imports app; never the reverse);
# server.py re-exports its own copy for the MCP face's historic progress loop.
_PROGRESS_EVERY_S = 2.0


@dataclass
class LookupHit:
    project: str
    relpath: str
    heading: str
    title: str
    snippet: str
    score: float
    line_start: int | None = None  # 1-based span of the section in the file,
    line_end: int | None = None  # resolved against current disk (None if gone)
    # True while this project's vectors are still being rebuilt after a store wipe
    # (embedder profile change): the result set is INCOMPLETE, not authoritative
    index_rebuilding: bool = False
    # True when this hit IS a design decision that a dep change has tainted — the
    # ambient marker, on the same footing as `index_rebuilding`: the moment an
    # agent retrieves a stale decision is the only moment the warning can change
    # what it does with it.
    tainted: bool = False
    # Which pillar store the hit came from — relpaths are store-relative, so the
    # pillar is part of resolving one back to a file.
    store: str = "notes"
    # Per-hit signal annotation, straight off `Hit.signals` ("which route drove
    # this hit"): dense cosine, sparse BM25 term, winning alias label. None when
    # the producer doesn't track routes. Additive output — consumers that don't
    # know it ignore it.
    signals: dict[str, Any] | None = None


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
        # llmkit is optional and we don't own its config schema; the realistic
        # failures are it being absent (ImportError), an unreadable config file
        # (OSError), or a missing/malformed profile (KeyError/ValueError). Any of
        # those means "no usable profile" → fall back to the declared embed. A
        # genuinely unexpected error is left to surface, not hidden.
        with contextlib.suppress(ImportError, OSError, KeyError, ValueError):
            from llmkit.bridge import load

            conf = load(str(Path(config.generate.config).expanduser()))
            spec = conf.select(profile, "embed")
            if spec:
                return replace(config.embed, model=spec)
    return config.embed


def _stored_dim(store: Store) -> int | None:
    """Dimension of the vectors the store already holds (None when empty or the
    backend can't say). Feeds `build_embedder`'s refusal to degrade to the hash
    embedder over real-model vectors — best-effort, so a store that can't be
    probed at construction never blocks startup."""
    try:
        return store.current_dim()
    except Exception:  # noqa: BLE001 — an unreachable store fails later, loudly
        return None


def _min_max(vals: list[float]) -> list[float]:
    """Scale `vals` into [0,1] over their own range (a flat list maps to 0).

    The shared half of both range-matched rerank stages (`Crib._rerank` for notes,
    `Crib._rerank_code` for code): normalize the fusion score AND the cross-encoder
    score over the same head, then SUM — which keeps the fusion's magnitude instead
    of discarding it the way RRF does (measured to regress cross-domain)."""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    return [(v - lo) / rng for v in vals]


class Crib:
    def __init__(self, paths: Paths, config: Config, store: Store) -> None:
        self.paths = paths
        self.config = config
        self.store = store
        embed_cfg = _resolve_embed_config(config)
        # Persist what's embedded (crib.embedcache): alias vectors and code
        # descriptions are re-embedded at every start otherwise. Vectors only;
        # any search index is built in memory.
        from . import embedcache

        # Keyed on what `build_embedder` ACTUALLY returned (its `model_id`), not on
        # `embed_cfg.model`: a missing backend falls back to the hash embedder, and
        # the config key would file those vectors under the real model's name.
        self.embedder = embedcache.wrap(
            build_embedder(embed_cfg, stored_dim=_stored_dim(store)),
            store,
        )
        self.index = IndexEngine(
            store,
            self.embedder,
            config.chunk.window_words,
            config.chunk.overlap_words,
            keyword_terms=self._keyword_terms,
            summary_terms=self._summary_terms,
        )
        # DESIGN §14: network git is CLI-side (the terminal owns auth) — the
        # daemon only ever snapshots/reads history locally.
        self.git = GitBacking(paths.data_dir, allow_network=False)
        self.versions = VersionRing(paths.versions_dir, config.versions_keep)
        # Where each project's DATA tier lives — the global store, or a repo
        # subdir it was adopted into (docs/plans/repo-local-storage). Cached per
        # project because every note read/write hits it; `project_adopt`/
        # `project_release` invalidate. The INDEX tiers never move (see
        # `Paths.project_dir`), so only notes + their ring resolve through here.
        self.project_paths = ProjectPathResolver(paths, config)
        # Pillar stores: one parameterized note-file store implementation (path
        # resolution + the write path: stash→save→index), instantiated per pillar
        # over the SAME shared backends (vector store / IndexEngine / ring /
        # resolver) — the pillars differ only by their StoreSpec. Crib keeps
        # delegators for the notes pillar (below); the facet layers own theirs.
        self.notestore = NoteStore(
            paths, store, self.index, self.versions, self.project_paths
        )
        self.designstore = NoteStore(
            paths, store, self.index, self.versions, self.project_paths, DESIGN_SPEC
        )
        self.planstore = NoteStore(
            paths, store, self.index, self.versions, self.project_paths, PLANS_SPEC
        )
        self.learningstore = NoteStore(
            paths, store, self.index, self.versions, self.project_paths, LEARNINGS_SPEC
        )
        # Routing table for relpath-first entry points (the watcher, migration).
        self.pillars: dict[str, NoteStore] = {
            "notes": self.notestore,
            "design": self.designstore,
            "plans": self.planstore,
            "learnings": self.learningstore,
        }
        self.memory_bindings = MemoryBindings(paths.data_dir / "memory-bindings.json")
        self._reranker: Any = None  # lazy cross-encoder, warm for the daemon
        self._watcher: Watcher | None = None
        self._code_watcher: CodeWatcher | None = None
        self._describe_q: Any = None  # DescribeQueue, started by the daemon
        self._mirror: Any = None  # MemoryMirror, started by the daemon
        # auto keyword_index refresh (daemon): per-note settle timers, plus strong
        # refs for ALL fire-and-forget daemon tasks (backlogs, refreshes) — an
        # unreferenced asyncio task is GC-collectable mid-flight
        self._kw_timers: dict[tuple[str, str], asyncio.TimerHandle] = {}
        self._bg_tasks: set[asyncio.Task] = set()
        # projects left in an in-flight reconcile sweep (None = not reconciling);
        # the `status` progress signal for the backgrounded startup reconcile
        self._reconcile_remaining: int | None = None
        self._reconcile_reason: str | None = None  # why (e.g. an embedder change)
        # Projects whose chunks a store wipe took and whose re-embed sweep hasn't
        # reached them yet — retrieval against one is THIN, so its hits say so
        # (`index_rebuilding`). Cleared per project as the recovery sweep finishes it.
        self._resweep_pending: set[str] = set()
        # projects whose in-repo store isn't on this machine and that startup work
        # has already warned about (one line each, not one per sweep/event)
        self._warned_unavailable: set[str] = set()
        # the daemon's loop, set once it exists — the only place a background
        # recovery sweep can outlive the call that scheduled it
        self._daemon_loop: asyncio.AbstractEventLoop | None = None
        self._on_close: Callable[[], None] | None = None
        # The code subsystem's shared per-project state (resident cache, freshness
        # epoch, write locks, in-flight/sweep tracking) — owned by CodeStore; the
        # methods below reach through `self.code` (codestore.py).
        self.code = CodeStore(paths, config)
        # Cross-project refs (.crib refs:) — resolution + edge attribution. Needs two
        # Crib-owned callables it can't own: a ref's resident cache (carries the
        # pipeline revalidate hook) and nested-.crib boundary detection (shared with
        # code enumeration). Crib keeps delegators (below) for its many callers.
        self.refs = Refs(paths, self._resident_code, self._nested_project_roots)
        # The project-layer surface the indexing pipeline depends on — narrow deps
        # (refs + code + config for resolution/ref-context, plus two injected callables
        # for enumeration + watcher registration). No back-reference to Crib.
        self.services = ProjectServices(
            self.refs,
            self.code,
            paths,
            config,
            self._enumerate_code_files,
            self._register_code_root,
        )
        # The code-index pipeline (extract → describe → persist), over CodeStore +
        # ProjectServices. Crib keeps delegators (below) so the watcher, the resident
        # revalidate hook, and project setup/index call it unchanged.
        self.indexer = CodeIndexer(self.services)
        # What background generation already failed on (crib.genledger): in the
        # store's own database when it has one, so it survives restarts.
        from .genledger import for_store

        self.ledger = for_store(store)
        self.indexer.set_ledger(self.ledger)
        # Parsed derived-index records (crib.recordcache), backed by the same
        # database, so a fresh daemon doesn't re-parse every TOML.
        from . import recordcache

        recordcache.attach(store)
        # In-flight project_index JOBS (proj → {task, crib_at}): the join-don't-
        # restart registry. A re-invoke while a sweep runs awaits the existing task
        # instead of racing a second one — the zero-reset counter bug, fixed at the
        # source. Same-process only (the daemon is the shared process).
        self.code_jobs: dict[str, dict[str, Any]] = {}
        # in-flight RELOCATE jobs (proj → task + live progress): the heavy half
        # (mount rekey + citation rewrite + rehash) runs daemon-owned so a client
        # disconnect (the combiner's 90s cap) can never cancel it mid-embed —
        # learned live on the mcp-combiner relocation;
        self.relocate_jobs: dict[str, dict[str, Any]] = {}
        # Durable symbol learnings, over refs + their own pillar store.
        # Crib keeps resolve_project + delegate public wrappers (learning_add/…).
        self.learnings = Learnings(paths, self.refs, self.learningstore)
        # Design decisions + plan items (notes under design/ and plans/) and the
        # dependency graph over them. Same shape: Crib resolves, Designs executes.
        self.designs = Designs(paths, self.designstore, self.planstore, self.notestore)
        # Code-index queries (lookup/xref/dossier/graph) over refs + learnings + the
        # resident cache. Crib keeps resolve_project + delegate public wrappers.
        self.query = CodeQuery(
            self.refs,
            self.learnings,
            self.embedder,
            self._resident_code,
            self._require_code_index,
        )

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
        self._daemon_loop = loop  # a loop that outlives a tool call exists now
        # Watch roots = the global `projects/` tree PLUS each AVAILABLE in-repo
        # store's notes dir. Computed once at start, so a mid-life adoption takes
        # effect on the next daemon start — the same accepted limitation as
        # memory bindings (DESIGN §13).
        self._watcher = Watcher(
            self.paths.projects_dir,
            self._on_fs_change,
            loop,
            self._in_repo_store_roots(),
        )
        self._watcher.start()
        # Code watcher — reindexes source on edit (notes-watcher reloads notes,
        # code-watcher reindexes code). Seed it with every code-indexed project's root.
        self._code_watcher = CodeWatcher(self._on_code_change, loop)
        from .codeindex import SymbolIndex

        for name in self.projects():  # `projects()` returns plain names
            root = SymbolIndex(self.paths.project_dir(name)).source_root()
            if root is not None:
                self._code_watcher.watch_root(name, root)
        self._code_watcher.start()
        # Deferred describe: the code-watcher persists STRUCTURE eagerly and hands the
        # changed symbols to this per-file backoff queue, so an edit burst coalesces to
        # one focused LLM describe once the file settles (docs § Deferred describe).
        from .describe_queue import DescribeQueue

        gen = self.config.generate
        self._describe_q = DescribeQueue(
            loop,
            self.indexer._describe_and_patch,
            base=gen.describe_backoff_base,
            cap=gen.describe_backoff_cap,
        )
        self.indexer.set_describe_queue(self._describe_q)
        # Self-heal: a crash (or clean stop) mid-window leaves symbols with structure
        # but a blank description — re-drive them so nothing stays undescribed. Ditto
        # the notes keyword facet (refresh timers cancelled on stop): elaborate's
        # content-hash gate is its durable signal, so the catch-up is a cheap no-op
        # when nothing was lost.
        self._spawn_bg(loop, self._describe_backlog())
        self._spawn_bg(loop, self._keyword_backlog())

    def _in_repo_store_roots(self) -> dict[str, Path]:
        """`{project: data root}` for every project adopted into a repo whose
        store is actually here — the extra watcher roots. The DATA ROOT, not the
        notes dir, so edits in every pillar sibling (design/, plans/, learnings/)
        are seen. An unavailable one is skipped with one warning rather than left
        to fail per event."""
        roots: dict[str, Path] = {}
        for name in self.projects():
            pp = self.project_paths(name)
            if not pp.in_repo:
                continue
            if self._skip_if_unavailable(pp, "not watching"):
                continue
            roots[name] = pp.data_root
        return roots

    def _skip_if_unavailable(self, pp: ProjectPaths, what: str) -> bool:
        """True (with ONE warning per project per process) when this project's
        in-repo store isn't on this machine. Startup work — the reconcile sweep,
        the watcher roots — must skip such a project, never crash the daemon: a
        repo that isn't cloned here is a normal state, not a fault."""
        if pp.available:
            return False
        if pp.project not in self._warned_unavailable:
            self._warned_unavailable.add(pp.project)
            print(
                f"[crib] {what} {pp.project}: its notes live in a repo at "
                f"{pp.store_token}, which is not on this machine "
                f"(clone it, add a [locations] entry, or `crib project release "
                f"{pp.project}`)",
                file=sys.stderr,
            )
        return True

    def _spawn_bg(self, loop: asyncio.AbstractEventLoop, coro: Any) -> None:
        """Fire-and-forget on the daemon loop, with a strong reference held until done
        (asyncio keeps only weak refs — an unreferenced task can be GC'd mid-flight)."""
        t = loop.create_task(coro)
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)

    async def _on_fs_change(self, project: str, store: str, relpath: str) -> None:
        pillar = self.pillars.get(store, self.notestore)
        await self.index.index_file(
            project, pillar.dir(project), relpath, store=pillar.spec.name
        )

    async def _on_code_change(
        self, project: str, changes: dict[str, tuple[str, bool]]
    ) -> None:
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
            # OUTER RUNNER (filesystem-watch callback): the OS calls us here at a
            # top-level event boundary, so an exception escaping would tear down the
            # watch loop and silently stop all indexing — hence the deliberate broad
            # catch. We do NOT swallow it: report it and let the next event retry.
            try:
                await asyncio.to_thread(self._revalidate, project)
            except Exception as e:  # noqa: BLE001 — outer runner, see above
                print(
                    f"[crib] watch revalidate failed for {project!r}: {e}",
                    file=sys.stderr,
                )
            return
        for relpath, (root, deleted) in changes.items():
            try:
                if relpath.startswith(
                    "\x00doc\x00"
                ):  # in-situ doc (see CodeWatcher._decode)
                    # index_file is async (asyncio locks) — await on THIS loop, don't
                    # thread it (a per-thread asyncio.run would strand stale-loop locks).
                    rel_to_repo = relpath[len("\x00doc\x00") :]
                    src_rel = src_relpath(Path(root).name, rel_to_repo)
                    await self.index.index_file(
                        project,
                        self.notes_dir(project),
                        src_rel,
                        content_path=self.abspath(project, src_rel),
                    )
                elif deleted and not (Path(root) / relpath).exists():
                    await asyncio.to_thread(self._drop_file, project, relpath)
                else:
                    # NB: a `deleted` flag for a file that EXISTS is a spurious
                    # delete — macOS FSEvents coalesces a rename-style save into
                    # flag bundles that watchdog re-expands in arbitrary order,
                    # and the batch's last-event-wins can land on the delete.
                    # Trusting it evicted whole files' symbols; the file's actual
                    # state at dispatch time (post-debounce) is the truth.
                    # DEFER the LLM describe: persist structure now, queue the
                    # description pass with per-file backoff (the live edit path is
                    # exactly where rapid saves would otherwise slam the LLM).
                    await asyncio.to_thread(
                        self._index_code_file_tracked,
                        Path(root),
                        relpath,
                        project,
                        True,
                        None,
                        "defer",
                    )
            except Exception as e:  # noqa: BLE001 — per-file batch boundary, see below
                # OUTER RUNNER (per-file indexing batch): one malformed/unreadable
                # file must not abort the whole change batch, so each file is
                # isolated with a broad catch on purpose — but we report which file
                # failed rather than swallowing it.
                print(f"[crib] index batch: skipped {relpath!r}: {e}", file=sys.stderr)

    def _register_code_root(self, project: str, root: str | Path) -> None:
        """Watch a repo's source root as soon as it's indexed (so a mid-session
        onboard starts live-updating immediately)."""
        if self._code_watcher is not None:
            self._code_watcher.watch_root(project, root)

    async def _describe_backlog(self) -> None:
        """Re-drive symbols a stop/crash left undescribed. The deferred-describe path
        blanks a changed symbol's description on the structural write, so `content_hash
        present + empty description` is the durable "never got described" signal — one
        inline reindex per affected file catches them up.

        Bounded two ways, because each file is an LLM call: the generation ledger
        skips files whose describe already failed for the same content and
        provider until their backoff expires (crib.genledger), and at most
        `[generate] backlog_budget` files are attempted per start (the rest wait
        for the next start). One summary line says what happened."""
        from .codeindex import SymbolIndex
        from .genledger import provider_id

        budget = max(0, int(self.config.generate.backlog_budget))
        provider = provider_id(self.config.generate, "elaborate")
        pending = attempted = backing_off = 0
        for name in self.projects():
            try:
                si = SymbolIndex(self.paths.project_dir(name))
                root = si.source_root()
                if root is None:
                    continue
                hashes: dict[str, str] = {}
                for e in si.all():
                    if (
                        e.get("content_hash")
                        and not e.get("description")
                        and e.get("file")
                    ):
                        # The file's hash is the input; without one, the
                        # symbol's own hash, so an edit still counts as new input.
                        hashes[f"{name}:{e['file']}"] = str(
                            e.get("file_hash") or e.get("content_hash") or ""
                        )
                self.ledger.forget_except("describe", f"{name}:", list(hashes))
                if not hashes:
                    continue
                pending += len(hashes)
                ready, waiting = self.ledger.due("describe", hashes, provider)
                backing_off += len(waiting)
                for target in sorted(ready):
                    if attempted >= budget:
                        break
                    attempted += 1
                    rel = target.split(":", 1)[1]
                    try:  # inline: describe now, no queue — per-file boundary
                        await asyncio.to_thread(
                            self._index_code_file_tracked, root, rel, name, True
                        )
                    except Exception as e:  # noqa: BLE001 — see comment below
                        print(
                            f"[crib] enrichment catch-up: skipped {rel!r}: {e}",
                            file=sys.stderr,
                        )
            except Exception as e:  # noqa: BLE001 — per-project catch-up boundary
                # OUTER RUNNER (startup catch-up over every project): a missing or
                # odd SymbolIndex for one project must not stop the others, so the
                # per-project body is isolated with a deliberate broad catch — and
                # reported, not swallowed.
                print(
                    f"[crib] enrichment catch-up skipped {name!r}: {e}",
                    file=sys.stderr,
                )
        if pending:
            print(
                f"[crib] describe backlog: {pending} file(s) with undescribed symbols; "
                f"attempted {attempted} (budget {budget}), {backing_off} backing off "
                f"after earlier failures, {pending - attempted - backing_off} left "
                "for a later start",
                file=sys.stderr,
            )

    async def _keyword_backlog(self) -> None:
        """Notes counterpart of `_describe_backlog`: catch the keyword_index AND
        summary_index facets up after a stop dropped pending write-path refreshes
        (their settle timers are cancelled on shutdown, and unfired ones simply die).
        Both verbs are content-hash-gated per section, so this is a no-op when nothing
        was lost — but the first run over a corpus that predates the configured labels
        generates for every never-enriched section (the same work the explicit
        `crib elaborate`/`crib summarize` would do). The project-wide pass also runs
        the orphan prune (`SectionIndex.prune`) for each label."""
        if not (
            self.config.retrieve.keyword_labels or self.config.retrieve.summary_labels
        ):
            return
        for name in self.projects():
            if self._skip_if_unavailable(self.project_paths(name), "not enriching"):
                continue
            try:
                # one combined pass, all facets; background: the ledger may skip
                await self.enrich(project=name, background=True)
            except Exception as e:  # noqa: BLE001 — best-effort catch-up
                print(
                    f"[crib] enrichment catch-up failed ({name}): {e}", file=sys.stderr
                )

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
        if self._describe_q is not None:
            self._describe_q.stop()
            self._describe_q = None
        if self._mirror is not None:
            self._mirror.stop()
            self._mirror = None
        for timer in self._kw_timers.values():
            timer.cancel()
        self._kw_timers.clear()
        for task in self._bg_tasks:  # best-effort: unfinished work dies with us
            task.cancel()
        self._bg_tasks.clear()

    def close(self) -> None:
        """Stop watchers, shut warm LSP sessions down, and release the store
        (the DuckDB file, or the shared server's refcount)."""
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
        return self.notestore.dir(project)

    def _keyword_terms(
        self, project: str, section_hash: str, labels: tuple[str, ...]
    ) -> list[str]:
        """Per-section keyword_index terms for the given labels — the BM25 feed
        (§3.1). Read from the section-addressed TOML store; empty when a section
        has no keyword set for a label yet (graceful: BM25 falls back to
        body+heading)."""
        from .section_index import SectionIndex

        return SectionIndex(
            self.project_paths(project).project_dir, "keyword_index"
        ).terms_for(section_hash, list(labels))

    def _summary_terms(
        self, project: str, section_hash: str, labels: tuple[str, ...]
    ) -> list[str]:
        """Per-section summary_index rephrasings for the given labels — the dense
        alias feed (§3). Read from the section-addressed TOML store; empty when a
        section has no summary for a label yet."""
        from .section_index import SectionIndex

        return SectionIndex(
            self.project_paths(project).project_dir, "summary_index"
        ).terms_for(section_hash, list(labels))

    def _source_roots(self, project: str) -> "SourceRoots":
        """Per-project registry of docs indexed in-situ (prefix -> repo root)."""
        return self.notestore.source_roots(project)

    def abspath(self, project: str, relpath: str) -> Path:
        return self.notestore.abspath(project, relpath)

    async def _write_note(self, project: str, relpath: str, note: Note) -> IndexResult:
        res = await self.notestore.write(project, relpath, note)
        self._schedule_keyword_refresh(project, relpath)  # keep the sparse facet fresh
        return res

    _KW_SETTLE_S = 1.0  # per-note debounce before the refresh fires

    def _schedule_keyword_refresh(self, project: str, relpath: str) -> None:
        """AUTO enrichment: after a note write, (re)generate the terms for the active
        `keyword_labels` (BM25 facet) AND `summary_labels` (dense alias facet) — OFF
        the save path, content-hash-gated so unchanged sections skip. Without this the
        body facet silently carries retrieval on freshly-written notes: the keyword arm
        can't see un-elaborated sections, and the alias max-merge can't see
        un-summarized ones. Mirrors how the code path folds keyword gen into its
        mandatory describe pass. DEBOUNCED per note (a write burst — store then append
        then edit — collapses to ONE refresh after `_KW_SETTLE_S`), and the fired task
        is held in `_bg_tasks` (an unreferenced task can be GC'd mid-flight).
        Daemon-only — a one-shot CLI has no persistent loop to drain the task; there
        `crib elaborate`/`crib summarize` stay the explicit verbs."""
        if (
            not (
                self.config.retrieve.keyword_labels
                or self.config.retrieve.summary_labels
            )
            or self._describe_q is None
        ):
            return  # describe queue present ⇒ daemon
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _refresh() -> None:
            try:
                # ONE combined pass covers every configured facet (keyword + summary
                # labels) in one generation per section. `_generate_enrichment`
                # already drops the lexical/summary caches iff it generated anything,
                # so the next query rebuilds — no extra invalidation needed here.
                await self.enrich(relpath=relpath, project=project)
            except Exception as e:  # noqa: BLE001 — best-effort background refresh
                print(f"[crib] auto enrichment refresh failed: {e}", file=sys.stderr)

        key = (project, relpath)

        def _fire() -> None:
            self._kw_timers.pop(key, None)
            t = loop.create_task(_refresh())
            self._bg_tasks.add(t)
            t.add_done_callback(self._bg_tasks.discard)

        old = self._kw_timers.pop(key, None)
        if old is not None:
            old.cancel()  # re-arm: newest write restarts the settle
        self._kw_timers[key] = loop.call_later(self._KW_SETTLE_S, _fire)

    # --- tool verbs --------------------------------------------------------
    # near-duplicate nudge: a stored note whose probe matches an existing note
    # this closely is flagged in the result (the descriptions preach append/edit
    # over duplicating; this is what detects it).
    DEDUPE_WARN_SCORE = 0.85

    def project_is_new(self, proj: str) -> bool:
        """True if `proj` has no notes dir yet (a write would create it)."""
        return not self.project_paths(proj).notes_dir.exists()

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

    def _similar(
        self, proj: str, content: str, exclude: str, store: str = "notes"
    ) -> list[dict[str, Any]]:
        """Near-duplicate hints for a just-stored note (excludes the note itself),
        probing the SAME pillar it was stored into — a near-duplicate note is
        redundancy, a near-duplicate decision forks the graph, and each is a
        same-pillar question.

        Blocking (a query embed + a retrieval pass) — callers on the event loop
        must `to_thread` it."""
        probe = content.strip().splitlines()[0][:200] if content.strip() else ""
        if not probe:
            return []
        try:
            hits = self.lookup(probe, project=proj, k=4, store=store)
        except Exception:  # noqa: BLE001 — a nudge must never fail the store
            return []
        rows = [
            {"relpath": h.relpath, "heading": h.heading, "score": h.score}
            for h in hits
            if h.relpath != exclude and h.score >= self.DEDUPE_WARN_SCORE
        ]
        # FACET STATE AT THE CUE (plan-deferral-review.md P2): a probe hit that is
        # parked or declined is the answer, not redundancy — "was this already
        # planned/parked/declined?" carries the status (and, for plan rows, the
        # trigger/why lives one `plan_read` away). Notes have no status.
        if store in ("design", "plans") and rows:
            graph = self.designs._load_graph(proj)
            by_rel = {
                n.relpath: n
                for n in graph.of_kind("design" if store == "design" else "plan")
            }
            for r in rows:
                n = by_rel.get(r["relpath"])
                if n:
                    r["status"] = n.status
        return rows

    async def store_note(
        self,
        content: str,
        title: str | None = None,
        project: str | None = None,
        tags: list[str] | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        proj = self.resolve_project(project, cwd)
        created = self.project_is_new(proj)
        # Parenthesized deliberately: `a or b if c else d` binds as `(a or b) if c
        # else d`, which threw away an EXPLICIT title whenever the content was
        # blank/whitespace and filed the note as "note".
        title = title or (
            content.strip().splitlines()[0][:60] if content.strip() else "note"
        )
        relpath = self._unique_relpath(proj, _slug(title))
        fm: dict[str, Any] = {"title": title, "source": "manual"}
        if tags:
            fm["tags"] = tags
        note = Note(path=self.abspath(proj, relpath), frontmatter=fm, body=content)
        res = await self._write_note(proj, relpath, note)
        similar = await asyncio.to_thread(self._similar, proj, content, relpath)
        return {
            "project": proj,
            "relpath": relpath,
            "indexed": res.upserted,
            "created": created,
            "similar": similar,
        }

    async def move_note(
        self,
        relpath: str,
        to_project: str | None = None,
        to_relpath: str | None = None,
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Relocate a note across projects and/or rename it, preserving its `id`
        (and thus version-ring history). One-way: write destination, drop source."""
        src_proj = self.resolve_project(project, cwd)
        return await self.notestore.move(
            src_proj, relpath, to_project or src_proj, to_relpath or relpath
        )

    async def append_note(
        self,
        relpath: str,
        content: str,
        heading: str | None = None,
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        proj = self.resolve_project(project, cwd)
        path = self.abspath(proj, relpath)
        note = (
            notes.load(path)
            if path.exists()
            else Note(path=path, frontmatter={"source": "appended"}, body="")
        )
        block = f"\n\n## {heading}\n{content}" if heading else f"\n\n{content}"
        note.body = note.body.rstrip() + block
        res = await self._write_note(proj, relpath, note)
        return {"project": proj, "relpath": relpath, "indexed": res.upserted}

    async def edit_note(
        self,
        relpath: str,
        new_content: str,
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Replace raw file content (frontmatter preserved if present in input)."""
        proj = self.resolve_project(project, cwd)
        fm, body = notes.parse(new_content)
        path = self.abspath(proj, relpath)
        if not fm and path.exists():
            try:
                fm = notes.load(path).frontmatter  # keep existing frontmatter
            except NoteParseError:
                fm = {}  # unparseable header: this edit REPAIRS it (the old
                # bytes go to the version ring on the way through)
        note = Note(path=path, frontmatter=fm, body=body)
        res = await self._write_note(proj, relpath, note)
        return {"project": proj, "relpath": relpath, "indexed": res.upserted}

    async def forget(
        self, relpath: str, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """Delete a note: remove it from disk and drop its chunks from the index.

        The current content is stashed to the version ring first (keyed by note
        id), so a forgotten note's bytes survive and can be recovered. Deletion
        of the index entry happens through the same `index_file` — once the file
        is gone, it sees a missing path and drops all chunks for that relpath."""
        proj = self.resolve_project(project, cwd)
        return await self.notestore.delete(proj, relpath)

    def read_note(
        self, relpath: str, project: str | None = None, cwd: Path | None = None
    ) -> str:
        proj = self.resolve_project(project, cwd)
        return self.notestore.read(proj, relpath)

    def locate(
        self, relpath: str, project: str | None = None, cwd: Path | None = None
    ) -> str:
        proj = self.resolve_project(project, cwd)
        return str(self.abspath(proj, relpath))

    async def reindex(
        self,
        relpath: str | None = None,
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Reindex a note, or fully reconcile a project when relpath is None.

        Full reconcile walks the UNION of on-disk notes and indexed paths, so it
        catches files added/edited while crib was down AND drops orphaned chunks
        for notes deleted off disk. All idempotent via the hash gate (§4).

        A full reindex is also where an embedder change is detected, and that WIPES
        the shared store — every project's chunks, not just this one's. When it
        happens, schedule the full recovery re-embed (below) so the wipe is a
        visible, self-healing event instead of every other project silently
        returning nothing."""
        proj = self.resolve_project(project, cwd)
        if relpath:
            return await self.notestore.reindex(proj, relpath)
        # Self-heal the legacy facet layout FIRST, so this very sweep drops the
        # old-path chunks and embeds the moved files — also what absorbs
        # old-layout files a not-yet-upgraded machine pushed into the sync repo.
        mig = self._migrate_legacy_layout(proj)
        recreated = self._maybe_recreate_for_dim()
        res = await self.notestore.reindex(proj)
        # Fan out over the sibling pillars — same union sweep, each scoped to its
        # own tree and its own chunks. Empty pillars are cheap no-ops.
        for pillar in (self.designstore, self.planstore, self.learningstore):
            r = await pillar.reindex(proj)
            for k in ("files", "changed", "removed"):
                res[k] += r[k]
            res["skipped"] += r["skipped"]
            if r.get("duplicate_ids"):
                res["duplicate_ids"] = res.get("duplicate_ids", []) + r["duplicate_ids"]
        if mig["moved"] or mig["skipped"] or mig["refs_rewritten"]:
            res["migrated"] = mig
        if recreated:
            res["recreated"] = True
            self._recover_from_wipe("embedder profile change")
        return res

    def _maybe_recreate_for_dim(self) -> bool:
        """A full reindex is the one safe place to switch embedder: if the stored
        vectors' dim differs from the current embedder (a profile flip to a bigger
        model), recreate the store so all chunks re-embed at the new dim.
        The store is shared across projects AND pillars, so this wipes EVERY
        project's chunks (and in-situ `sources/…` docs, which the notes sweep
        doesn't walk) — hoisted out of the per-pillar store so the check runs once
        per full reindex, not once per pillar."""
        cur = self.store.current_dim()
        model = getattr(self.embedder, "model_id", None)
        stored = self.store.meta_get("embed_model") if model else None
        if cur is None or (cur == self.embedder.dim and stored in (None, model)):
            # Nothing stored yet, or the same embedder: record who wrote the
            # vectors, so a later same-dimension model change is caught too.
            if model and stored != model:
                self.store.meta_set("embed_model", model)
            return False
        change = (
            f"dim {cur}→{self.embedder.dim}"
            if cur != self.embedder.dim
            else f"model {stored}→{model}"
        )
        print(
            f"crib: embedder {change}; recreating the vector store (full re-embed)",
            file=sys.stderr,
        )
        self.store.recreate()
        if model:
            self.store.meta_set("embed_model", model)
        # The derived retrieval caches (BM25 corpus + summary alias vectors) are
        # built from the store and keyed per project — a wipe invalidates ALL of
        # them at once, not just one project's. Without this a warm daemon kept
        # serving BM25 hits for chunks that no longer exist (and whose ids the
        # dense side can't score).
        self.index.invalidate_all_caches()
        return True

    def _recover_from_wipe(self, reason: str) -> None:
        """The vector store was recreated: mark EVERY project as awaiting a re-embed
        (their hits carry `index_rebuilding` until swept) and drive one full sweep —
        notes plus in-situ `sources/…` docs, which the per-project notes reconcile
        doesn't walk. Only the daemon can carry that in the background; in-process
        (`--no-daemon`, a one-shot CLI) there's no loop that outlives the call, so
        say what's needed instead of stranding a task that would never run."""
        self._resweep_pending |= set(self.projects()) | self._indexed_projects()
        if self._daemon_loop is None:
            print(
                f"[crib] vector store recreated ({reason}); other projects are "
                f"un-embedded until you run `crib project reconcile`",
                file=sys.stderr,
            )
            return
        if self._reconcile_remaining is not None:
            print(
                f"[crib] vector store recreated ({reason}); the in-flight "
                f"reconcile will re-embed what's left",
                file=sys.stderr,
            )
            return
        self.reconcile_in_background(self._daemon_loop, reason=reason)

    # --- chunk-schema migration ------------------------------------------
    # `chunk_id` is derived from the chunking scheme (chunk.py). When that scheme
    # changes, live chunks keep stale ids and the content-hash gate never revisits
    # them. The recorded version says whether a full sweep is owed; the sweep IS
    # the migration (index_file re-embeds under the new ids and deletes the ones
    # no longer produced), so there is no bespoke migration code.
    @property
    def _schema_marker(self) -> Path:
        return self.paths.index_dir / "chunk_schema"

    def stored_chunk_schema(self) -> int:
        try:
            return int(self._schema_marker.read_text().strip())
        except (OSError, ValueError):
            return 0  # never recorded → pre-v2 store, sweep owed

    def _record_chunk_schema(self) -> None:
        try:
            self._schema_marker.write_text(f"{CHUNK_SCHEMA_VERSION}\n")
        except OSError as e:  # marker only — a lost write costs one extra sweep
            print(f"[crib] could not record chunk schema version: {e}", file=sys.stderr)

    async def reconcile_all(self) -> dict[str, Any]:
        """Startup/post-pull sweep across every project — catch up on offline
        note changes, then eagerly rebuild merge-dirtied code files (a pull
        that merged divergent symbol records lands here via the CLI's
        post-pull reconcile)."""
        projects = sorted(set(self.projects()) | self._indexed_projects())
        total: dict[str, Any] = {
            "projects": len(projects),
            "changed": 0,
            "removed": 0,
            "skipped": [],
        }
        self._reconcile_remaining = len(projects)
        try:
            for proj in projects:
                # A project adopted into a repo that isn't cloned here has no
                # notes to sweep — skip it (one warning) instead of failing the
                # whole startup sweep on the first missing store.
                if self._skip_if_unavailable(
                    self.project_paths(proj), "not reconciling"
                ):
                    total.setdefault("unavailable", []).append(proj)
                    self._reconcile_remaining -= 1
                    continue
                r = await self.reindex(project=proj)
                total["changed"] += r["changed"]
                total["removed"] += r["removed"]
                # notes crib couldn't read — carried up so a sweep that skipped
                # something says so instead of quietly under-indexing
                total["skipped"] += [
                    {"project": proj, **s} for s in r.get("skipped", [])
                ]
                if self._registered_root(proj) is not None:
                    # In-situ `sources/…` docs live outside the notes tree, so the
                    # walk above never revisits them — and the wipe-recovery
                    # obligation (`_resweep_pending`) is in-memory only, so a
                    # restart between a store wipe and its resweep would otherwise
                    # lose the docs for good. Always sweep them here: hash-gated,
                    # a no-op whenever they're already current.
                    total["insitu_reindexed"] = total.get(
                        "insitu_reindexed", 0
                    ) + await self._resweep_insitu(proj)
                self._resweep_pending.discard(proj)
                self._reconcile_remaining -= 1
                await asyncio.sleep(0)  # yield: a backlog must not starve clients
            dirty = await self._reindex_dirty_code()
            if dirty:
                total["code_files_rebuilt"] = dirty
            # Every project has now been re-chunked under the current scheme and
            # stale-id chunks dropped, so the marker is honest. Only here — a
            # single-project reindex proves nothing about the others.
            self._record_chunk_schema()
        finally:
            self._reconcile_remaining = None
            self._reconcile_reason = None
        return total

    async def _resweep_insitu(self, project: str) -> int:
        """Re-embed a project's in-situ `sources/…` docs after a store wipe. Doc
        chunks live under the notes namespace but their FILES live in the repo, so
        the notes reconcile never walks them — only this sweep does. Best-effort:
        a project with no registered repo (or a root that's since moved) simply has
        no in-situ docs to recover."""
        try:
            res = await self.index_docs_insitu(project=project)
        except Exception as e:  # noqa: BLE001 — no repo / moved root / unreadable doc
            print(
                f"[crib] in-situ doc re-embed skipped for {project}: {e}",
                file=sys.stderr,
            )
            return 0
        return int(res.get("changed", 0))

    def reconcile_in_background(
        self, loop: asyncio.AbstractEventLoop, reason: str | None = None
    ) -> None:
        """Run the startup reconcile as a background task instead of before the
        transport opens. A cold daemon with an offline backlog would otherwise
        hold the port closed past the client's ready timeout; serving *during*
        the sweep is safe by design — the hash gate makes any overlap a no-op
        (DESIGN §9). Progress shows up in `status` as `reconciling` /
        `reconcile_remaining`, and `reconcile_reason` when the sweep was triggered
        by something other than startup (an embedder change wiping the store)."""
        self._daemon_loop = loop
        self._reconcile_reason = reason
        stored = self.stored_chunk_schema()
        if stored != CHUNK_SCHEMA_VERSION:
            print(
                f"[crib] chunk schema {stored}→{CHUNK_SCHEMA_VERSION}; the startup "
                f"reconcile re-chunks every project (repeated headings get "
                f"distinct ids)",
                file=sys.stderr,
            )
        self._spawn_bg(loop, self._reconcile_background(reason))

    async def _reconcile_background(self, reason: str | None = None) -> None:
        try:
            rec = await self.reconcile_all()
        except Exception as e:  # noqa: BLE001 — a failed sweep must not kill the daemon
            print(
                f"[crib] {reason or 'startup'} reconcile failed: {e}", file=sys.stderr
            )
            return
        if rec["changed"] or rec["removed"]:
            print(
                f"[crib] startup reconcile: {rec['changed']} updated, "
                f"{rec['removed']} chunk(s) removed across "
                f"{rec['projects']} project(s)",
                file=sys.stderr,
            )
        for s in rec.get("skipped", []):
            print(
                f"[crib] skipped {s['project']}/{s['relpath']}: {s['error']}",
                file=sys.stderr,
            )
        checkpoint = getattr(self.store, "checkpoint", None)
        if callable(checkpoint) and (rec["changed"] or rec["removed"]):
            # Fold the sweep's writes from the WAL into the file, so a long-lived
            # daemon's store doesn't sit on an ever-growing log.
            try:
                await asyncio.to_thread(checkpoint)
            except Exception as e:  # noqa: BLE001 — housekeeping only
                print(f"[crib] store checkpoint failed: {e}", file=sys.stderr)

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
                continue  # index synced from another machine — lazy path
            src_root: Path = root  # narrowed rebind (mypy: closure default)
            files = sorted(
                {
                    e["file"]
                    for e in store.all()
                    if e.get("file") and not e.get("content_hash")
                }
            )
            if not files:
                continue

            async def _one(rel: str, proj: str = proj, root: Path = src_root) -> bool:
                async with sem:
                    try:
                        await asyncio.to_thread(
                            self._index_code_file_tracked, root, rel, proj, True
                        )
                        return True
                    except Exception:  # noqa: BLE001 — the lazy gate backstops
                        return False

            done = await asyncio.gather(*(_one(f) for f in files))
            out[proj] = sum(done)
        return out

    def _indexed_projects(self) -> set[str]:
        return self.store.projects()

    @property
    def reranker(self) -> Any:
        """Lazy reranker, built once and kept warm (daemon-resident)."""
        if self._reranker is None:
            from .retrieve import build_reranker

            self._reranker = build_reranker(self.config.retrieve.rerank_model)
        return self._reranker

    def _rerank(self, query: str, hits: list[Hit]) -> list[Hit]:
        """RANGE-MATCHED rerank: min-max the fusion score AND the cross-encoder score over
        the top `rerank_top_n`, sum, re-order. The fusion below is score-based (dense-dominant),
        so the reranker folds in as a bounded term that keeps the fusion's magnitude — the
        rerank method must MATCH the fusion method (measured: RRF-rerank suits rank fusion,
        range-matched suits score fusion). Degrades to input order if the reranker is down."""
        n = self.config.retrieve.rerank_top_n
        head = hits[:n]
        if not head:
            return hits
        try:
            scores = self.reranker.scores(query, [h.document for h in head])
        except Exception as e:  # noqa: BLE001 — reranker optional; degrade to fused order
            print(f"[crib] reranker disabled: {e}", file=sys.stderr)
            return hits
        bn = _min_max([float(h.score) for h in head])  # fusion (dense + gated sparse)
        rn = _min_max(list(scores))
        order = sorted(range(len(head)), key=lambda i: bn[i] + rn[i], reverse=True)
        return [head[i] for i in order] + hits[n:]

    def _retrieve(
        self,
        proj: str,
        query: str,
        vec: list[float],
        topn: int,
        hybrid: bool,
        rerank: bool,
        keyword_labels: tuple[str, ...] = (),
        keyword_weight: float = 1.0,
        summary_labels: tuple[str, ...] = (),
        summary_weight: float = 1.0,
        hints: list[str] | None = None,
        hint_weight: float = 0.15,
        *,
        store: str = "notes",
        labels: list[str] | None = None,
    ) -> list[Hit]:
        """Candidate Hits in DENSE-DOMINANT SCORE order: raw cosine (dense, already
        calibrated) + min-max'd keyword_index BM25, over the union candidate pool. Only
        the uncalibrated sparse side is normalized; dense cosine stays RAW so a confident
        dense match is never diluted — the score-based fusion the range-matched `_rerank`
        is matched to. summary_index aliases are MULTI-VECTOR dense: each alias embedding
        points at its section, and the section's dense score is the MAX over its vectors
        (body ⊕ aliases × `summary_weight` trust scale) — an alias match IS a dense match,
        never a rank bonus. A candidate that came from the sparse side and isn't in the
        dense finalists has its cosine BACK-FILLED (re-embed just that handful) so every
        pooled candidate has a real dense base to blend on. (No coverage gate — prose
        sections already carry the vocabulary; the gate is a code-side answer to terse
        identifiers.)"""
        from .retrieve import tokenize

        kw_blend = 0.5  # fusion weight of the keyword_index term

        # Retrieval is scoped to ONE pillar store: every chunk carries its
        # `store` key (chunk schema v3, which every DuckDB store was filled at).
        where: dict[str, Any] = {"project": proj, "store": store}
        if labels:
            # A tags= lookup filters IN the query, so the dense top-n is drawn
            # from matching chunks only, never thinned after the fact.
            where["labels"] = {"$any": list(labels)}
        # `where` is a vector-store filter dict (an exact-match metadata predicate),
        # not a SQL string; there is no SQL here.
        # pi-lens-ignore: python-sql-injection
        dense = self.store.query(vec, k=topn, where=where)
        cos: dict[str, float] = {
            h.id: h.score for h in dense
        }  # raw cosine per candidate
        dense_by = {h.id: h for h in dense}
        docs: dict = {}
        kw: dict[str, float] = {}
        ids: list[str] = []
        bm25 = None
        if hybrid:
            ids, docs, bm25 = self.index.lexical.get(
                proj, keyword_labels, keyword_weight, store=store
            )
            if ids:
                qt = tokenize(query)
                sparse = bm25.scores(qt)
                if self.config.retrieve.keyword_coverage_gate:
                    from .retrieve import STOPWORDS

                    Q = {t for t in set(qt) if len(t) > 1 and t not in STOPWORDS}
                    sparse = [c * s for c, s in zip(bm25.coverage(Q), sparse)]
                top = sorted(range(len(ids)), key=lambda j: sparse[j], reverse=True)
                kw = {ids[j]: sparse[j] for j in top[:topn] if sparse[j] > 0}

        # Hints: each caller-supplied exact string is its own lexical route over
        # the SAME resident BM25 index — tokenize-only, zero embedding. Agreement
        # (how many routes hit a candidate) is a COUNT signal the score blend
        # cannot express; hint-only candidates ENTER the pool like sparse-only
        # ones (dense back-filled below), because an exact-term match is the
        # whole reason the caller supplied it.
        hint_hits: dict[str, list[str]] = {}
        if hints:
            if bm25 is None:  # hybrid off: the lexical index wasn't fetched
                ids, docs, bm25 = self.index.lexical.get(
                    proj, keyword_labels, keyword_weight, store=store
                )
            if ids and bm25 is not None:
                for hint in hints:
                    hs = bm25.scores(tokenize(hint))
                    htop = sorted(range(len(ids)), key=lambda j: hs[j], reverse=True)
                    for j in htop[:topn]:
                        if hs[j] > 0:
                            hint_hits.setdefault(ids[j], []).append(hint)

        summ_hit = False
        alias_labels: dict[str, str] = {}  # rep cid -> label whose alias won
        if summary_labels:
            for cid, (ac, alias_label) in self.index.summaries.best_cosines(
                proj, summary_labels, vec, store=store
            ).items():
                ac *= summary_weight  # alias-trust scale (1.0 = full dense)
                if ac > cos.get(cid, -1.0):
                    cos[cid] = ac  # max over the section's vectors
                    alias_labels[cid] = alias_label
                    summ_hit = True

        def _signals_for(cid: str) -> dict[str, Any]:
            """Per-hit route map: which signal(s) drove this candidate. Pure
            provenance — riding on the Hit, never consulted by ranking."""
            sig: dict[str, Any] = {"dense": round(cos[cid], 4)}
            if cid in kn:
                sig["sparse"] = round(kn[cid], 4)
            if cid in alias_labels:
                sig["alias"] = alias_labels[cid]
            if hint_hits.get(cid):
                sig["hints"] = hint_hits[cid]
            return sig

        if not kw and not summ_hit and not hint_hits:  # dense only
            out = dense
        else:
            pool = list(dict.fromkeys(list(cos) + list(kw) + list(hint_hits)))
            if not docs:  # need doc text to back-fill cosines
                docs = self.store.get_docs(where)
            if labels:
                # The lexical index and the alias cache span the whole store, so
                # their candidates get the same label filter the query applied.
                wanted = set(labels)

                def _labelled(cid: str) -> bool:
                    h = dense_by.get(cid)
                    meta = h.metadata if h is not None else docs.get(cid, ("", {}))[1]
                    return bool(wanted & set((meta or {}).get("labels") or []))

                pool = [cid for cid in pool if _labelled(cid)]
                kw = {cid: v for cid, v in kw.items() if cid in pool}
            missing = [cid for cid in pool if cid not in cos and cid in docs]
            if missing:
                for cid, dv in zip(
                    missing,
                    embed_batch(self.embedder, [docs[cid][0] for cid in missing]),
                ):
                    cos[cid] = sum(a * b for a, b in zip(vec, dv))  # L2-normalized
            # drop any we couldn't score, and any alias rep whose chunk vanished
            pool = [
                cid for cid in pool if cid in cos and (cid in dense_by or cid in docs)
            ]

            def _mm(d: dict[str, float]) -> dict[str, float]:
                if not d:
                    return {}
                vals = [d.get(cid, 0.0) for cid in pool]
                lo, hi = min(vals), max(vals)
                rng = (hi - lo) or 1.0
                return {cid: (d.get(cid, 0.0) - lo) / rng for cid in pool}

            kn = _mm(kw)  # min-max the sparse side over the pool
            score = {
                cid: (
                    cos[cid]
                    + kw_blend * kn.get(cid, 0.0)
                    # hint agreement bonus: count of independent lexical routes
                    # that hit this candidate, scaled by hint_weight — the one
                    # signal the score blend cannot express on its own
                    + (
                        hint_weight * len(hint_hits[cid]) / len(hints)
                        if hints and hint_hits.get(cid)
                        else 0.0
                    )
                )
                for cid in pool
            }
            order = sorted(pool, key=lambda cid: score[cid], reverse=True)[:topn]
            out = []
            for cid in order:
                s = round(score[cid], 4)
                sig = _signals_for(cid)
                if cid in dense_by:
                    h = dense_by[cid]
                    out.append(Hit(cid, h.document, h.metadata, s, signals=sig))
                else:
                    doc, meta = docs[cid]
                    out.append(Hit(cid, doc, meta, s, signals=sig))
        if rerank:
            out = self._rerank(query, out)
        return out

    def lookup(
        self,
        query: str,
        project: str | None = None,
        k: int = 8,
        tags: list[str] | None = None,
        dedupe: str = "section",
        min_score: float = 0.0,
        cwd: Path | None = None,
        hybrid: bool | None = None,
        rerank: bool | None = None,
        keyword_labels: list[str] | None = None,
        keyword_weight: float | None = None,
        summary_labels: list[str] | None = None,
        summary_weight: float | None = None,
        hints: list[str] | None = None,
        hint_weight: float | None = None,
        *,
        store: str = "notes",
    ) -> list[LookupHit]:
        """Ranked sections matching `query`.

        `dedupe` collapses duplicates: "section" (default) keeps one hit per
        distinct heading — so a note's several relevant sections all surface, and
        only repeated windows of the *same* section merge; "file" keeps one hit
        per note (breadth across notes, hides a note's other sections); "none"
        keeps every chunk. Section is right for retrieval; file suits a
        what-notes-are-relevant overview.
        """
        from .codequery import check_k, check_query

        # Both come straight off a tool call: an empty query embeds to a
        # meaningless vector and returns whatever is nearest to nothing, and a k of
        # 0/-1/10_000 either silently returns nothing or drags the whole corpus
        # through the reranker. Say what's accepted instead.
        check_query(query)
        check_k(k)
        if dedupe not in ("section", "file", "none"):
            raise CribUserError(
                f"unknown dedupe {dedupe!r}: use 'section' (default), 'file' or 'none'"
            )
        proj = self.resolve_project(project, cwd)
        vec = embed_query_batch(self.embedder, [query])[0]
        use_hybrid = self.config.retrieve.hybrid if hybrid is None else hybrid
        use_rerank = self.config.retrieve.rerank if rerank is None else rerank
        kw_labels = tuple(
            self.config.retrieve.keyword_labels
            if keyword_labels is None
            else keyword_labels
        )
        kw_weight = (
            self.config.retrieve.keyword_weight
            if keyword_weight is None
            else keyword_weight
        )
        sum_labels = tuple(
            self.config.retrieve.summary_labels
            if summary_labels is None
            else summary_labels
        )
        sum_weight = (
            self.config.retrieve.summary_weight
            if summary_weight is None
            else summary_weight
        )
        hw = self.config.retrieve.hint_weight if hint_weight is None else hint_weight
        # Hybrid pulls a wider candidate pool so BM25 can promote terms dense ranked low.
        topn = max(k * 3, 30) if use_hybrid else (k if dedupe == "none" else k * 3)
        raw = self._retrieve(
            proj,
            query,
            vec,
            topn,
            use_hybrid,
            use_rerank,
            kw_labels,
            kw_weight,
            sum_labels,
            sum_weight,
            hints=hints,
            hint_weight=hint_weight if hint_weight is not None else hw,
            store=store,
            labels=tags,
        )
        # a store wipe this project hasn't been re-swept after: what comes back is
        # incomplete, so every hit says so rather than thin results reading as "all
        # there is" (cleared per project by the recovery sweep)
        rebuilding = proj in self._resweep_pending
        # Tainted design notes, loaded LAZILY and at most once per lookup — a
        # frontmatter-only graph scan, and only when a hit actually lands in the
        # design store, so the ambient marker costs nothing on the common query.
        stale_designs: set[str] | None = None
        hits, seen = [], set()
        line_maps: dict[tuple[str, str], dict[str, tuple[int, int]]] = {}
        for h in raw:
            if h.score <= min_score:  # drop orthogonal / irrelevant matches
                continue
            # tags= was applied in the store query (`labels` = the chunk's tags
            # plus its frontmatter `type`, so tags=["design"] scopes to that facet).
            rp = h.metadata.get("relpath", "")
            pillar = str(h.metadata.get("store") or "notes")
            heading = h.metadata.get("heading_path", "")
            key = (pillar, rp) if dedupe == "file" else (pillar, rp, heading)
            if dedupe != "none" and key in seen:
                continue
            seen.add(key)
            if (pillar, rp) not in line_maps:  # read each file once, current on disk
                try:
                    src = self.pillars.get(pillar, self.notestore).abspath(proj, rp)
                    line_maps[(pillar, rp)] = section_line_map(src.read_text())
                except (OSError, ValueError):
                    line_maps[(pillar, rp)] = {}
            # A repeated heading path's 2nd+ section has its own span under
            # `heading#n`; the bare breadcrumb is occurrence 1 (chunk.section_key).
            span = line_maps[(pillar, rp)].get(
                section_key(heading, int(h.metadata.get("occurrence", 1) or 1))
            )
            if pillar == "design":
                if stale_designs is None:
                    stale_designs = self.designs.tainted_designs(proj)
                stale = rp in stale_designs
            else:
                stale = False
            hits.append(
                LookupHit(
                    project=proj,
                    relpath=rp,
                    heading=heading,
                    title=h.metadata.get("title", ""),
                    snippet=h.document[:280],
                    score=round(h.score, 4),
                    line_start=span[0] if span else None,
                    line_end=span[1] if span else None,
                    index_rebuilding=rebuilding,
                    tainted=stale,
                    store=pillar,
                    signals=h.signals,
                )
            )
            if len(hits) >= k:
                break
        return hits

    def apropos(
        self,
        query: str,
        project: str | None = None,
        k: int = 8,
        tags: list[str] | None = None,
        cwd: Path | None = None,
        hints: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """`lookup` (same section-level dedupe) but each hit carries the FULL
        matching section markdown — sliced from the file by its line span — rather
        than a 280-char snippet, for rendering the matches for a human to read."""
        proj = self.resolve_project(project, cwd)
        out: list[dict[str, Any]] = []
        for h in self.lookup(
            query, project, k, tags, dedupe="section", cwd=cwd, hints=hints
        ):
            section = h.snippet
            if h.line_start and h.line_end:
                # a moved/unreadable/short file just keeps the snippet fallback
                with contextlib.suppress(OSError, ValueError):
                    src = self.pillars.get(h.store, self.notestore).abspath(
                        proj, h.relpath
                    )
                    lines = src.read_text().splitlines()
                    section = "\n".join(lines[h.line_start - 1 : h.line_end])
            out.append({**vars(h), "section": section})
        return out

    # --- generation: distill + elaborate (knowledge-capture §2/§4, §3.1) ---
    async def distill(
        self, relpath: str, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """Revise a note in place via the LLM: compress, dedupe, normalize; keep
        facts/decisions, drop deliberation, preserve code verbatim. Thrash-guarded
        (no write if the body is unchanged), marked `source: distilled`, written
        through the version ring so a bad revision is a cheap rollback."""
        proj = self.resolve_project(project, cwd)
        path = self.abspath(proj, relpath)
        if not path.exists():
            raise CribUserError(f"no such note: {relpath} in project {proj!r}")
        note = notes.load(path)
        prompt = self.project_config(proj).distill_prompt or DEFAULT_DISTILL_PROMPT
        from .generate import agenerate

        new_body = (
            await agenerate(
                self.config.generate,
                prompt,
                note.body,
                purpose="distill",
                timeout=self.config.generate.timeout,
            )
        ).strip()
        if not new_body or new_body == note.body.strip():
            return {"project": proj, "relpath": relpath, "changed": False}
        note.frontmatter["source"] = "distilled"
        note.body = new_body
        res = await self._write_note(proj, relpath, note)
        return {
            "project": proj,
            "relpath": relpath,
            "changed": True,
            "indexed": res.upserted,
        }

    async def elaborate(
        self,
        label: str,
        relpath: str | None = None,
        project: str | None = None,
        cwd: Path | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """keyword_index: generate search terms per section for BM25 (§3.1).
        `crib elaborate <label>`; activate via `[retrieve].keyword_labels`."""
        from .section_index import KEYWORD_PROMPTS, resolve_prompt

        prompt = resolve_prompt(label, self.config.elaborate, KEYWORD_PROMPTS)
        return await self._generate_index(
            "keyword_index",
            "elaborate",
            label,
            prompt,
            relpath,
            project,
            cwd,
            overwrite,
        )

    async def summarize(
        self,
        label: str,
        relpath: str | None = None,
        project: str | None = None,
        cwd: Path | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """summary_index: generate LLM rephrasings per section, embedded as dense
        alias vectors (§3). `crib summarize <label>`; activate via
        `[retrieve].summary_labels`."""
        from .section_index import SUMMARY_PROMPTS, resolve_prompt

        prompt = resolve_prompt(label, self.config.summarize, SUMMARY_PROMPTS)
        return await self._generate_index(
            "summary_index",
            "summarize",
            label,
            prompt,
            relpath,
            project,
            cwd,
            overwrite,
        )

    # --- code symbol index (docs/code-symbol-index.md) --------------------
    async def code_index(
        self,
        path: str,
        project: str | None = None,
        cwd: Path | None = None,
        patch_edges: bool = True,
    ) -> dict[str, Any]:
        """Delegate to the CodeIndexer pipeline (crib/codeindexer.py)."""
        return await self.indexer.code_index(path, project, cwd, patch_edges)

    def _index_code_file_tracked(
        self,
        root: Path,
        rel: str,
        proj: str,
        patch_edges: bool,
        prior: list[dict] | None = None,
        describe_mode: str = "inline",
    ) -> dict[str, Any]:
        """Delegate to the CodeIndexer pipeline (kept on Crib so the watcher and the
        resident-cache revalidate hook call it unchanged)."""
        return self.indexer._index_code_file_tracked(
            root, rel, proj, patch_edges, prior, describe_mode
        )

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
        return self.code.resident(
            proj, revalidate=self._revalidate, watched=self._code_watched(proj)
        )

    def _reload_code(
        self, proj: str, tok: Any, prev: _ResidentCode | None
    ) -> _ResidentCode:
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
        """Delegate to CodeStore, injecting the pipeline's per-file indexer."""
        self.code.revalidate(proj, self._index_code_file_tracked)

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
            hint = (
                f" If you meant a DIFFERENT, already-indexed project ({names}), name "
                f"it: pass project=<name> or project_path=<a path in that repo> (or "
                f"use_project <name> to switch your current project)."
            )
        else:
            hint = ""
        raise CribUserError(
            f"project {proj!r} isn't code-indexed yet. If this is the repo you're working "
            f"in, INDEX IT NOW then retry: run project_index (project_path=<this repo dir>) "
            f"— it indexes the source so lookup/dossier/xref work; do NOT grep or read "
            f"files instead.{hint}"
        )

    # ── Whole-project lifecycle: setup / index / forget / status ──────────────
    # Shared engine behind `crib project <verb>` (superset) and the code/notes
    # facets. Everything defers to `_ensure_crib`, the sensible-default .crib
    # creator, so onboarding an unfamiliar repo is one call (docs §…).
    def _code_ignore(self) -> frozenset[str]:
        return frozenset(
            {
                ".git",
                "node_modules",
                ".venv",
                "venv",
                "__pycache__",
                ".mypy_cache",
                ".pytest_cache",
                ".ruff_cache",
                "dist",
                "build",
                "target",
                ".tox",
                ".idea",
                "site-packages",
                ".cache",
                ".claude",
                ".DS_Store",
            }
        )

    def _detect_code_globs(self, root: Path) -> list[str]:
        """Auto-detect source globs: which LSP-supported extensions actually occur
        under `root` (junk dirs pruned) → `**/*.<ext>` per present type."""
        from .codeindex import load_specs

        exts = {
            e
            for spec in load_specs().values()
            if isinstance(spec, dict)
            for e in (spec.get("extensionToLanguage") or {})
        }
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
                dirs[:] = []  # bounded — no need to descend
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
        # An in-repo store is a boundary too, for the same reason: what lives there
        # is this project's NOTES (indexed as notes), never its source — a code glob
        # reaching in would index the same bytes twice under two identities.
        if (link := CribLink.find(root)) is not None and link.store_dir is not None:
            bounds.append(link.store_dir)
        for g in globs:
            for p in root.glob(g):
                if (
                    p.is_file()
                    and not any(part in junk for part in p.relative_to(root).parts)
                    and not any(p.is_relative_to(b) for b in bounds)
                ):
                    seen.add(p)
        # extensionless / unknown-extension files matched by the grammar map
        specs = load_specs()
        served: set[str] = set()
        for sp in specs.values():
            if isinstance(sp, dict) and resolve_command(sp):
                served.update((sp.get("extensionToLanguage") or {}).values())
        known_exts = {
            e
            for sp in specs.values()
            if isinstance(sp, dict)
            for e in (sp.get("extensionToLanguage") or {})
        }
        grammar = load_grammar()
        for dp, dirs, files in os.walk(root):
            dirs[:] = [
                d
                for d in dirs
                if d not in junk
                and not d.startswith(".")
                and not any((Path(dp) / d) == b for b in bounds)
            ]
            for fn in files:
                p = Path(dp) / fn
                if p in seen or p.suffix.lower() in known_exts:
                    continue  # globs already cover known extensions
                lang = content_lang(p, grammar)
                if lang and lang in served:
                    seen.add(p)
        return sorted(seen)

    def _registered_root(self, project: str | None) -> Path | None:
        """Where `project` was last indexed from: the symbol_index's `.source_root`,
        else a sole in-situ doc root. None when unrecorded — or when the recorded
        dir is gone, so a moved checkout can be re-rooted from its new path."""
        if not project:
            return None
        from .codeindex import SymbolIndex

        root = SymbolIndex(self.paths.project_dir(project)).source_root()
        if root is None:
            doc_roots = {Path(r) for r in self._source_roots(project).all().values()}
            if len(doc_roots) == 1:
                root = doc_roots.pop()
        return root.resolve() if root is not None and root.is_dir() else None

    def _ensure_crib(
        self, cwd: Path | None, project: str | None, want_code: bool, want_docs: bool
    ) -> tuple[Any, bool]:
        """Find the repo's `.crib`, or CREATE one with sensible defaults (project =
        repo dir name; auto-detected code `paths:` + doc `docs:` globs indexed
        in-situ). Returns (CribLink, created?). The one primitive both `project
        setup` and the facet setups defer to.

        Root resolution NEVER falls back to the process cwd — in the daemon that's
        just wherever sharedserver spawned it, and a `project_index(project=zdot)`
        once re-rooted zdot into an unrelated checkout that way. An explicit `cwd`
        (the caller's project_path) decides; a bare `project` resolves to the root
        it was last indexed from (`_registered_root`); neither → error. A cwd whose
        `.crib` names a DIFFERENT project than an explicit `project` is treated as
        incidental (the CLI ships the shell cwd with every call), so the project's
        registered root wins; with no registered root to prefer it's an error —
        never index one project's repo into another."""
        base = Path(cwd).resolve() if cwd else None
        link = CribLink.find(base) if base is not None else None
        if project and (link is None or link.project != project):
            # `base` (if any) isn't visibly `project`'s repo — resolve the name to
            # the root it was last indexed from before trusting an incidental cwd.
            reg = self._registered_root(project)
            if reg is not None:
                base, link = reg, CribLink.find(reg)
                if link is not None and link.project != project:
                    raise CribUserError(
                        f"stale registration: {project!r} was last indexed from "
                        f"{reg}, but the .crib there now names {link.project!r}. "
                        "Pass project_path=<the repo dir> explicitly (or "
                        "project_forget the stale project)."
                    )
            elif link is not None:
                raise CribUserError(
                    f"{base} belongs to project {link.project!r} (its .crib says "
                    f"so), not {project!r} — refusing to index one project's repo "
                    f"into another. Pass project_path=<{project}'s repo dir>."
                )
            # else: no .crib and no registered root → first-time onboard of
            # `project` at `base` (the create path below).
        if base is None:
            raise CribUserError(
                f"can't locate a repo for project {project!r}: it has no recorded "
                "source root. Pass project_path=<the repo dir> — a repo-scoped op "
                "never falls back to the server's own cwd."
            )
        if link and link.root:
            return link, False
        # Anchor at the nearest repo marker, else at `base` ITSELF — never escape to
        # base.parent (find_root's no-marker fallback), which would write .crib in the
        # wrong dir and index the parent tree.
        root = base
        for d in (base, *base.parents):
            if (
                (d / ".git").exists()
                or (d / "pyproject.toml").exists()
                or (d / "setup.py").exists()
            ):
                root = d
                break
        name = project or root.name.replace(" ", "-")
        if (
            not project
            and (reg := self._registered_root(name)) is not None
            and reg != root.resolve()
        ):
            # dir-name collision with an existing project rooted elsewhere — a
            # fresh .crib here would silently merge two repos under one project
            raise CribUserError(
                f"a project named {name!r} already exists, rooted at {reg} — "
                f"refusing to also bind {root} to it. Add a .crib here naming a "
                "different project (or project_forget the old one to re-root)."
            )
        lines = [
            "# Auto-created by `crib project setup` — ties this repo to a crib "
            "project.",
            f"project: {name}",
        ]
        # globs are quoted: a bare `- **/*.py` makes YAML read `*` as an alias anchor.
        if want_code and (globs := self._detect_code_globs(root)):
            lines += ["paths:", *[f'  - "{g}"' for g in globs]]
        if want_docs:
            lines += ["docs:", '  - "README.md"', '  - "docs/**/*.md"']
        (root / ".crib").write_text("\n".join(lines) + "\n")
        return CribLink.find(root), True

    async def _index_project_code(
        self,
        proj: str,
        root: Path,
        globs: list[str],
        budget_s: float | None = None,
        crib_at: float | None = None,
    ) -> dict[str, Any]:
        """Delegate to the CodeIndexer pipeline (project setup/index call this)."""
        return await self.indexer._index_project_code(
            proj, root, globs, budget_s, crib_at=crib_at
        )

    async def project_setup(
        self, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """Onboard a repo end-to-end: ensure `.crib` (create with sensible defaults if
        missing), index its declared docs IN-SITU (source is master, never copied),
        AND index all its source code. The superset — `code`+`notes`. Idempotent;
        safe to re-run. (`import` — copy a file into memory — stays a separate,
        explicit verb.)"""
        link, created = self._ensure_crib(cwd, project, want_code=True, want_docs=True)
        proj = project or link.project
        new_project = proj not in self.projects()  # before indexing creates its dirs
        docs = (
            await self.index_docs_insitu(proj, link.root) if link.doc_patterns else {}
        )
        globs = link.paths or self._detect_code_globs(link.root)
        code = await self._index_project_code(proj, link.root, globs)
        return {
            "project": proj,
            "root": str(link.root),
            "crib_created": created,
            "created": new_project,
            "docs_indexed": docs.get("docs", 0),
            **code,
        }

    async def project_index(
        self,
        project: str | None = None,
        cwd: Path | None = None,
        budget_s: float | None = None,
        wait_s: float | None = None,
    ) -> dict[str, Any]:
        """(Re)index the project's SOURCE CODE and in-situ docs from its `.crib`
        (ensuring a `.crib` first). Cheap re-run via the content-hash gate.

        THE POLLING CONTRACT: the sweep runs as a daemon-owned job — a call
        returns a RUNNING HANDLE at once (`running: true` + live done/total)
        and never holds a client request for the sweep's length (a capped proxy
        would cut it; poll `project_status`'s `jobs` — it never errors or
        blocks, and reporting a failed job is its job). A re-invoke JOINS the
        running sweep (never a second one — the zero-reset counter bug, fixed
        at the source); `wait_s=<seconds>` BLOCKS bounded (scripts/terminals;
        the agent-facing faces don't expose it), returning the final result or
        a still-running handle. A finished sweep's re-invoke returns its full
        result. A scope change (`.crib` mtime moved since kick-off) returns a
        `conflict` instead of silently joining a stale-scoped sweep —
        `project_cancel` then re-invoke.

        `budget_s` bounds the WORK: files not reached are deferred
        (`complete=False, remaining=N`) — re-invoke to continue (finished files
        re-skip via the content-hash gate)."""
        link, created = self._ensure_crib(cwd, project, want_code=True, want_docs=False)
        proj = project or link.project
        crib_file = link.root / ".crib"
        crib_at = crib_file.stat().st_mtime if crib_file.exists() else None
        running = self.code_jobs.get(proj)
        if running is not None and not running["task"].done():
            if running["crib_at"] != crib_at:
                sw_total = (self.code.sweeps.get(proj) or {}).get("total")
                return {
                    "project": proj,
                    "joined": False,
                    "conflict": (
                        f"a sweep is already running with an older scope "
                        f"({sw_total} files, .crib mtime "
                        f"{running['crib_at']}); project_cancel it, or wait "
                        f"for it to finish"
                    ),
                }
            # JOIN the running sweep: bounded, or an immediate handle — a poll
            # is never an error, and a bounded join still never starts sweep 2.
            if wait_s is not None:
                started = time.monotonic()
                while (
                    not running["task"].done() and time.monotonic() - started < wait_s
                ):
                    await asyncio.wait({running["task"]}, timeout=_PROGRESS_EVERY_S)
                if running["task"].done():
                    return {**(await running["task"]), "joined": True}
                return {
                    "project": proj,
                    "joined": True,
                    "running": True,
                    "done": (self.code.sweeps.get(proj) or {}).get("done"),
                    "total": (self.code.sweeps.get(proj) or {}).get("total"),
                    "resume_hint": (
                        "the sweep keeps running daemon-side — poll "
                        "project_status, call project_wait to re-attach, or "
                        "re-invoke project_index to join"
                    ),
                }
            return {
                "project": proj,
                "joined": True,
                "running": True,
                "done": (self.code.sweeps.get(proj) or {}).get("done"),
                "total": (self.code.sweeps.get(proj) or {}).get("total"),
                "resume_hint": (
                    "already running — POLL project_status (never blocks, never "
                    "errors); re-invoke to finish-poll; project_wait to block"
                ),
            }
        task = asyncio.create_task(
            self._project_index_now(proj, link, created, budget_s)
        )
        # RETAINED (not popped on finish): the poll needs the completed row
        # (complete: true + the final counts) so 'finished OK' is distinguishable
        # from 'never started'; the next sweep for this project replaces it.
        self.code_jobs[proj] = {"task": task, "crib_at": crib_at}
        if wait_s is None:
            # the handle-at-once half of the contract: never hold the request
            sw = self.code.sweeps.get(proj) or {}
            return {
                "project": proj,
                "started": True,
                "running": True,
                "done": sw.get("done"),
                "total": sw.get("total"),
                "resume_hint": (
                    "indexing runs daemon-owned — POLL project_status (never "
                    "blocks, never errors); re-invoke to join, project_wait to "
                    "block for it"
                ),
            }
        started = time.monotonic()
        while not task.done() and time.monotonic() - started < wait_s:
            await asyncio.wait({task}, timeout=_PROGRESS_EVERY_S)
        return await task

    async def _project_index_now(
        self,
        proj: str,
        link: CribLink,
        created: bool,
        budget_s: float | None,
    ) -> dict[str, Any]:
        """The unguarded sweep body — the caller (project_index) owns
        resolution, join/cancel, and passes the link + created flag in
        (re-ensuring here would report crib_created=False on the very run
        that created the `.crib`)."""
        root = link.root
        if root is None:  # pragma: no cover — ensure always resolves a root
            raise CribUserError(
                f"project {proj!r} has no source root; pass project_path="
            )
        new_project = proj not in self.projects()  # before indexing creates its dirs
        docs = await self.index_docs_insitu(proj, root) if link.doc_patterns else {}
        globs = link.paths or self._detect_code_globs(root)
        crib_file = root / ".crib"
        crib_at = crib_file.stat().st_mtime if crib_file.exists() else None
        code = await self._index_project_code(
            proj, root, globs, budget_s, crib_at=crib_at
        )
        # No learning re-attach here. The sweep converts ENTRIES as a side effect
        # (every write normalizes identity), and the learnings join reads bindings,
        # so notes stay correct WITHOUT being rewritten — rebinding note files under
        # a git-synced store is tidiness, kept behind the explicit `learning_migrate`.
        return {
            "project": proj,
            "root": str(link.root),
            "crib_created": created,
            "created": new_project,
            "docs_indexed": docs.get("docs", 0),
            **code,
        }

    async def project_wait(
        self,
        project: str | None = None,
        cwd: Path | None = None,
        wait_s: float = 30.0,
        ctx: Any = None,
    ) -> dict[str, Any]:
        """Bounded-block on a running index job — the JOIN primitive for clients
        whose tool timeout is shorter than a sweep. Idle → `{running: false}`;
        finished within `wait_s` → the final result; still going → the live
        partial state (call again to keep waiting). While it waits it STREAMS
        PROGRESS (report_progress on the caller's context when one exists —
        `42/317 files (13%) · eta 1:50`), so a blocking join is still
        observable; `ctx` is the fastmcp Context when called over MCP."""
        proj = project or self._proj_from_cwd(cwd)
        if proj is None:
            raise CribUserError("project_wait: pass project= or run inside the repo")
        job = self.code_jobs.get(proj)
        if job is None or job["task"].done():
            return {"project": proj, "running": False}
        started = time.monotonic()
        while True:
            remaining = max(wait_s, 0.05) - (time.monotonic() - started)
            done, _pending = await asyncio.wait(
                {job["task"]}, timeout=min(_PROGRESS_EVERY_S, max(remaining, 0.001))
            )
            if done:
                return {
                    "project": proj,
                    "running": False,
                    "complete": True,
                    **job["task"].result(),
                }
            if ctx is not None:
                sw = self.code.sweeps.get(proj) or {}
                if sw.get("total"):
                    try:
                        await ctx.report_progress(
                            progress=sw.get("done") or 0,
                            total=sw["total"],
                            # pi-lens-ignore: python-empty-except
                            message=f"{proj}: {format_sweep(sw)}",
                        )
                    except Exception:  # noqa: BLE001 — progress is best-effort
                        pass
            if time.monotonic() - started >= max(wait_s, 0.05):
                sw = self.code.sweeps.get(proj) or {}
                return {
                    "project": proj,
                    "running": True,
                    "complete": False,
                    "done": sw.get("done"),
                    "total": sw.get("total"),
                    "resume_hint": (
                        "still running — POLL project_status (never blocks); "
                        "call project_wait again to keep waiting"
                    ),
                }

    async def project_cancel(
        self, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """Cancel a running index sweep. Completed files remain (hash-gated);
        undescribed files retry on the next project_index. Returns
        `{cancelled: false}` when nothing is running."""
        proj = project or self._proj_from_cwd(cwd)
        if proj is None:
            raise CribUserError("project_cancel: pass project= or run inside the repo")
        job = self.code_jobs.get(proj)
        if job is None or job["task"].done():
            return {"project": proj, "cancelled": False, "running": False}
        sw = self.code.sweeps.get(proj) or {}
        job["task"].cancel()
        return {
            "project": proj,
            "cancelled": True,
            "done_at_cancel": sw.get("done"),
            "total": sw.get("total"),
            "note": (
                "completed files remain (hash-gated); re-run project index to resume"
            ),
        }

    def _proj_from_cwd(self, cwd: Path | None) -> str | None:
        """Project name from the cwd's nearest `.crib`, or None — the read-only
        resolution for wait/cancel (never creates a `.crib`)."""
        if cwd is None:
            return None
        link = CribLink.find(Path(cwd))
        return link.project if link else None

    # ── in-repo storage: adopt / release (docs/plans/repo-local-storage) ───────
    # A project's data tier is EITHER global OR in a repo, never both, so these
    # two verbs MOVE files rather than merge them. `chunk_id` is
    # sha1(project + relpath + …) over PROJECT-relative relpaths, so a move
    # changes no chunk id and no content hash: the reconcile each verb ends with
    # is a hash-gated no-op that VERIFIES the move rather than reindexing it.

    def _note_ring_ids(self, project: str, notes_root: Path) -> list[str]:
        """The version-ring keys for every note under `notes_root`.

        The ring is keyed by note id ALONE (ids survive renames), so migrating a
        project's history means knowing which ids are its own — derived here the
        same way `_stash_existing` derives them on the way in, including the
        derived-ulid fallback for a note with no `id:`."""
        ids: list[str] = []
        for p in sorted(notes_root.rglob("*.md")):
            rel = str(p.relative_to(notes_root))
            try:
                raw = p.read_text()
            except OSError:
                continue
            note_id = notes.scan_id(raw) or derived_ulid(project, rel)
            ids.append(note_id)
        return ids

    @staticmethod
    def _move_tree(src: Path, dst: Path) -> int:
        """Move every file under `src` to the same relpath under `dst`, returning
        the count. Per-file (not one `shutil.move` of the dir) so a partially
        populated destination is still a merge of trees rather than a refusal —
        the "no merge" rule is enforced by the CALLERS, which refuse before any
        file moves."""

        import shutil

        moved = 0
        if not src.is_dir():
            return 0
        for p in sorted(src.rglob("*")):
            if not p.is_file():
                continue
            target = dst / p.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(target))
            moved += 1
        return moved

    def _move_ring(self, ids: list[str], src: Path, dst: Path) -> int:
        """Move the named notes' ring dirs between rings, returning the count."""
        moved = 0
        for note_id in ids:
            try:
                s = confine(src, note_id)
                d = confine(dst, note_id)
            except ValueError:  # an id that isn't a safe path segment
                continue
            if not s.is_dir():
                continue
            moved += self._move_tree(s, d)
            with contextlib.suppress(OSError):
                s.rmdir()
        return moved

    _STORE_GITIGNORE = (
        "# crib in-repo store — the notes ARE the repo's, the "
        "version ring is not\n.versions/\n"
    )

    @staticmethod
    def _warn_store_glob_overlap(link: CribLink, store: Path) -> list[str]:
        """Say which of the repo's `.crib` globs now reach INTO the store.

        Harmless — every enumeration point excludes the store (`CribLink.in_store`),
        which is exactly why it is worth saying: a `docs: ["**/*.md"]` that reads as
        "index everything, including these notes" does NOT, and adoption is the
        moment that became true. Silently-ignored config is the kind that gets
        rewritten to "fix" a problem it never had."""
        if link.root is None:  # a found link always has a root; be explicit for None
            return []
        hits = [
            p
            for p in (*link.doc_patterns, *link.paths)
            if any(f.is_relative_to(store) for f in link.root.glob(p))
        ]
        if hits:
            print(
                f"[crib] note: {link.root / '.crib'} globs ({', '.join(hits)}) "
                f"reach into {store} — files there are indexed as {link.project}'s "
                "notes and are excluded from those globs, so nothing is indexed "
                "twice; narrow the globs if you'd rather they said so.",
                file=sys.stderr,
            )
        return hits

    async def project_adopt(
        self, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """Move a project's notes (and their version ring) INTO the repo, at the
        dir its `.crib` declares as `store:`.

        The repo's git then owns those notes — `crib memory sync` and friends
        refuse for an adopted project, because committing them twice is how two
        histories of one file appear. The stub `projects/<name>/.cribproject`
        records the store as a portable `$LOCATION` token so the daemon can find
        it with no cwd near the repo; the derived index tiers do NOT move.
        Idempotent: adopting an already-adopted project at the same store is a
        no-op."""
        link = CribLink.find(cwd or Path.cwd())
        if link is None or link.root is None:
            raise CribUserError(
                "no `.crib` at or above this directory — an adopt needs the repo "
                "that will carry the notes (add a `.crib` with `project:` and "
                "`store:`, or run `crib project setup` first)"
            )
        proj = check_project_name(project or link.project)
        if project and link.project != proj:
            raise CribUserError(
                f"the `.crib` at {link.root} names project {link.project!r}, not "
                f"{proj!r} — adopt is about THIS repo's project"
            )
        store = link.store_dir
        if store is None:
            raise CribUserError(
                f"{link.root / '.crib'} declares no `store:` — add e.g. "
                "`store: .crib/store` (repo-root-relative) to say where in this "
                "repo the notes should live"
            )
        pp = self.project_paths(proj)
        if pp.in_repo:
            if pp.store_root == store:
                return {
                    "project": proj,
                    "store": str(store),
                    "changed": False,
                    "notes_moved": 0,
                    "versions_moved": 0,
                    "store_root": pp.store_token,
                    "message": f"{proj} already lives in {store}",
                }
            raise CribUserError(
                f"{proj} is already adopted into {pp.store_root} — `crib project "
                f"release {proj}` first, then adopt here (a project's notes live "
                "in exactly one place)"
            )
        if store.exists() and not store.is_dir():
            # `.crib` is itself a FILE at the repo root, so `store: .crib/store`
            # (an inviting-looking spelling) can never be created — say that here
            # rather than surfacing a bare NotADirectoryError from mkdir.
            raise CribUserError(
                f"`store: {link.store}` is not a directory ({store} exists as a "
                "file) — point it at a directory path, e.g. `store: .crib-store`"
            )
        from .watch import PILLAR_SEGMENTS

        for seg in PILLAR_SEGMENTS:
            dest = store / seg
            if any(dest.rglob("*.md")) if dest.is_dir() else False:
                raise CribUserError(
                    f"{dest} already holds notes — refusing to merge two note "
                    "trees. Move them aside (or pick a different `store:`) and "
                    "re-run"
                )
        ring_ids = [
            nid
            for seg in PILLAR_SEGMENTS
            if (src := pp.pillar_dir(seg)).is_dir()
            for nid in self._note_ring_ids(proj, src)
        ]
        store.mkdir(parents=True, exist_ok=True)
        notes_moved = 0
        for seg in PILLAR_SEGMENTS:  # every pillar sibling travels together
            src = pp.pillar_dir(seg)
            notes_moved += self._move_tree(src, store / seg)
            with contextlib.suppress(OSError):
                src.rmdir()  # only if the move emptied it
        versions_moved = self._move_ring(
            ring_ids, self.paths.versions_dir, store / ".versions"
        )
        # The ring is DERIVED history, not authored notes: the repo carries the
        # notes, crib carries their undo stack.
        (store / ".gitignore").write_text(self._STORE_GITIGNORE)
        # Now that the notes are in the repo, the repo's own globs can see them —
        # they don't (the store is excluded everywhere), but say so at the moment
        # it became true rather than leaving it to be discovered.
        self._warn_store_glob_overlap(link, store)
        pcfg = ProjectConfig.load(pp.config_file, proj)
        pcfg.store_root = portable_path(store, self.config.locations)
        pcfg.save(pp.config_file)
        self.project_paths.invalidate(proj)
        rec = await self.reindex(project=proj)
        return {
            "project": proj,
            "store": str(store),
            "changed": True,
            "store_root": pcfg.store_root,
            "notes_moved": notes_moved,
            "versions_moved": versions_moved,
            "reconciled": rec,
        }

    async def project_release(
        self, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """The inverse of `project_adopt`: move an in-repo project's notes (and
        ring) back into the global store and clear the stub's `store_root`. Use it
        when the repo is going away, or when the notes should stop travelling with
        it. Idempotent: releasing an already-global project is a no-op."""
        proj = self.resolve_project(project, cwd)
        pp = self.project_paths(proj)
        if not pp.in_repo:
            return {
                "project": proj,
                "changed": False,
                "notes_moved": 0,
                "versions_moved": 0,
                "message": f"{proj} already lives in the global store",
            }
        pp.require()  # can't move notes that aren't on this machine
        store = pp.store_root
        assert store is not None  # in_repo ⇒ store_root set
        from .watch import PILLAR_SEGMENTS

        gdir = self.paths.project_dir(proj)
        for seg in PILLAR_SEGMENTS:
            dest = gdir / seg
            if any(dest.rglob("*.md")) if dest.is_dir() else False:
                raise CribUserError(
                    f"{dest} already holds notes — refusing to merge two note "
                    "trees. Move them aside and re-run"
                )
        ring_ids = [
            nid
            for seg in PILLAR_SEGMENTS
            if (src := pp.pillar_dir(seg)).is_dir()
            for nid in self._note_ring_ids(proj, src)
        ]
        notes_moved = 0
        for seg in PILLAR_SEGMENTS:
            notes_moved += self._move_tree(pp.pillar_dir(seg), gdir / seg)
        versions_moved = self._move_ring(
            ring_ids, pp.versions_dir, self.paths.versions_dir
        )
        gi = store / ".gitignore"
        if gi.is_file() and gi.read_text() == self._STORE_GITIGNORE:
            gi.unlink()  # ours, and now describing nothing
        for d in (
            *(pp.pillar_dir(seg) for seg in PILLAR_SEGMENTS),
            pp.versions_dir,
            store,
        ):
            with contextlib.suppress(OSError):
                d.rmdir()  # each only if the move emptied it
        pcfg = ProjectConfig.load(pp.config_file, proj)
        pcfg.store_root = None
        pcfg.save(pp.config_file)
        self.project_paths.invalidate(proj)
        rec = await self.reindex(project=proj)
        return {
            "project": proj,
            "changed": True,
            "store": str(store),
            "notes_moved": notes_moved,
            "versions_moved": versions_moved,
            "reconciled": rec,
        }

    # ── project lifecycle: create / delete / rename (docs/surface.md) ─────────
    # Birth is a decision moment now: create is the RESIDENCY DOORWAY (declare
    # the repo store at birth instead of adopting retroactively) and records a
    # birth manifest, so a namespace's origin stays auditable once stores cross
    # machines. Write policy unchanged: writes still create implicitly — create
    # is an optional richer doorway, never a gate.

    @staticmethod
    def _stamp_manifest(pcfg: ProjectConfig) -> None:
        """Fill an absent birth manifest — once and only once (a re-create never
        rewrites a project's birth record)."""
        if pcfg.created is None:
            pcfg.created = datetime.date.today().isoformat()
        if pcfg.host is None:
            pcfg.host = claudemem.hostslug()

    async def project_create(
        self,
        project: str | None = None,
        cwd: Path | None = None,
        store: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly create a project: pillar skeleton + birth manifest
        (`created`/`host` in `.cribproject`).

        With `store=<repo-relative path>` (and `project_path=<the repo dir>`) it
        is the RESIDENCY DOORWAY: writes the repo `.crib` (`project:` +
        `store:`) and the machine stub so a project is born in-repo — the
        at-birth form of `project adopt`. It refuses when a non-empty global
        project of that name already exists (moving notes into a repo is
        adopt's job), so it can never be a silent merge.

        Idempotent: an existing project reports `created: false` and changes
        nothing. Writes do NOT require this verb — the implicit creation on
        first write stays (an absent manifest just means "born implicitly")."""
        from .watch import PILLAR_SEGMENTS

        def _skeleton(*roots: Path) -> None:
            for root in roots:
                for seg in PILLAR_SEGMENTS:
                    (root / seg).mkdir(parents=True, exist_ok=True)

        if store is not None:
            # Residency: anchored at the CALLER's repo — never the process cwd
            # (the daemon's spawn dir is not a repo anyone meant). Same rule as
            # every other repo-scoped verb.
            if cwd is None:
                raise CribUserError(
                    "declaring residency needs the repo: pass project_path=<the "
                    "repo dir> (a create never falls back to the server's cwd)"
                )
            base = Path(cwd).resolve()
            name = check_project_name(project) if project else None
            link = CribLink.find(base)
            crib_created = False
            if link is None or link.root is None:
                # The link is part of the birth: anchor at the nearest repo
                # marker, else at `base` itself (the `_ensure_crib` rule —
                # never escape to base.parent).
                root = base
                for d in (base, *base.parents):
                    if (
                        (d / ".git").exists()
                        or (d / "pyproject.toml").exists()
                        or (d / "setup.py").exists()
                    ):
                        root = d
                        break
                if name is None:
                    name = check_project_name(root.name.replace(" ", "-"))
                store_decl = store
            else:
                if name is not None and link.project != name:
                    raise CribUserError(
                        f"{base} belongs to project {link.project!r} (its .crib "
                        f"says so), not {name!r} — create is about THIS repo"
                    )
                name = check_project_name(name or link.project)
                if link.store and store != link.store:
                    raise CribUserError(
                        f"{link.root / '.crib'} already declares `store: "
                        f"{link.store}` — edit the .crib by hand to change it"
                    )
                if link.store is None:
                    raise CribUserError(
                        f"{link.root / '.crib'} declares no `store:` — add e.g. "
                        f"`store: {store}` (repo-root-relative), then re-run"
                    )
                root = link.root
                store_decl = link.store
            # The shared bucket never lives inside ONE repo — residency is a
            # repo's own memory, and `default` would silently adopt it into a
            # repo checkout (and into every clone's .crib).
            if name == self.config.default_project:
                raise CribUserError(
                    f"refusing to declare residency for {name!r} — the default "
                    "project is the shared bucket and stays in the global store"
                )
            # Validate the store string BEFORE anything is written anywhere:
            # it is interpolated into the repo's `.crib` (a YAML file), so a
            # newline or any YAML metacharacter would inject keys (reproduced:
            # `store: "a: b"` wrote a broken .crib). Restrict to the safe
            # spelling — letters/digits dot dash underscore slash — which
            # automatically covers absolute/`..`/control characters.
            if not re.fullmatch(r"[A-Za-z0-9._/-]+", store_decl) or not re.search(
                r"[A-Za-z0-9]", store_decl
            ):
                raise CribUserError(
                    f"invalid store path {store_decl!r}: use repo-relative "
                    "letters/digits/./-/_ / only (no YAML metacharacters — it "
                    "lands in the .crib)"
                )
            if root is None or not root.is_dir():  # pragma: no cover — find() roots
                raise CribUserError("cannot resolve a repo dir to hold the store")
            try:
                store_dir = confine(root, store_decl)
            except (CribUserError, ValueError) as e:
                # confine's message already names the offending value
                raise CribUserError(str(e)) from None
            # adopt's guard, generalized: ANY existing file along the store's
            # relative path (like `store: .crib/store` — the inviting spelling
            # inside the `.crib` FILE itself) means this path can never be a
            # directory; say that by name instead of a bare NotADirectoryError.
            blocker: Path | None = None
            cur = root
            for part in store_dir.relative_to(root).parts:
                cur = cur / part
                if cur.exists() and not cur.is_dir():
                    blocker = cur
                    break
            if blocker is not None:
                raise CribUserError(
                    f"`store: {store_decl}` is not a directory ({blocker} exists "
                    "as a file) — point it at a directory path, e.g. "
                    "`store: .crib-store`"
                )
            # A store that IS the repo ("." collapses to root) can never be a
            # crib store: pillar dirs would be created beside the repo's own
            # content, and a later `project delete --force` would walk the
            # repo's every directory. Same refusal for a store containing the
            # checkout's root markers — it is not crib-owned content.
            if store_dir.resolve() == root.resolve():
                raise CribUserError(
                    f"store {store_decl!r} resolves to the repo root — point it "
                    "at a dedicated directory, e.g. `store: .crib-store`"
                )
            if store_dir.is_dir() and (store_dir / ".git").exists():
                raise CribUserError(
                    f"{store_dir} contains a checkout of its own (has .git) — "
                    "it cannot be a crib store"
                )
            pp = self.project_paths(name)
            if pp.in_repo:
                if pp.store_root == store_dir:
                    return {
                        "project": name,
                        "created": False,
                        "residency": "repo",
                        "store": str(store_dir),
                        "store_root": pp.store_token,
                        "crib_created": False,
                        "message": f"{name} already lives in {store_dir}",
                    }
                raise CribUserError(
                    f"{name} is already adopted into {pp.store_root} — `crib "
                    f"project release {name}` first, then create here"
                )
            # Non-empty anywhere is a refusal, never a merge: notes already in
            # the repo store → this repo was written for another purpose; notes
            # already in the global store of the same name → ADOPT moves them.
            # Empty pillar dirs are fine (a pre-created skeleton, or the empty
            # tree `project use` mkdirs) — only CONTENT collides.
            for dest in (
                *(store_dir / seg for seg in PILLAR_SEGMENTS),
                *(pp.pillar_dir(seg) for seg in PILLAR_SEGMENTS),
            ):
                if dest.is_dir() and any(dest.rglob("*.md")):
                    hint = (
                        f"\n(the global store of {name!r} already holds notes — "
                        f"run `crib project adopt` to move them into the repo)"
                        if dest.is_relative_to(self.paths.projects_dir)
                        else ""
                    )
                    raise CribUserError(
                        f"{dest} already holds notes — refusing to merge two "
                        "note trees." + hint
                    )
            # ALL refusals passed — only now is anything written. The `.crib`
            # comes last of the checks but first of the writes, so a refused
            # create never leaves a poisoned link in a foreign repo.
            if crib_created is False and (
                link is None or link.root is None or link.store is None
            ):
                (root / ".crib").write_text(
                    "# Created by `crib project create` — ties this repo to a "
                    f"crib project with its store in-repo.\nproject: {name}\n"
                    f"store: {store}\n"
                )
                link2 = CribLink.find(root)
                if link2 is None or link2.root is None:  # pragma: no cover
                    raise CribUserError(f"created .crib at {root} failed to parse")
                link, crib_created = link2, True
                store_dir = link.store_dir
                if store_dir is None:  # pragma: no cover — validated above
                    raise CribUserError("store: failed to resolve")
            store_dir.mkdir(parents=True, exist_ok=True)
            _skeleton(store_dir)
            gi = store_dir / ".gitignore"
            if not gi.exists():  # never overwrite a repo's own ignore file
                gi.write_text(self._STORE_GITIGNORE)
            if self._watcher is not None:  # a mid-session birth stays live-watched
                self._watcher.add_store(name, store_dir)
            for seg in PILLAR_SEGMENTS:  # the (now-empty) global pillar dirs
                with contextlib.suppress(OSError):
                    pp.pillar_dir(seg).rmdir()
            pcfg = ProjectConfig.load(pp.config_file, name)
            pcfg.store_root = portable_path(store_dir, self.config.locations)
            self._stamp_manifest(pcfg)
            pcfg.save(pp.config_file)
            self.project_paths.invalidate(name)
            return {
                "project": name,
                "created": True,
                "residency": "repo",
                "store": str(store_dir),
                "store_root": pcfg.store_root,
                "crib_created": crib_created,
                "message": (
                    f"{name} born in-repo at {store_dir} — its pillar stores "
                    "travel with this repo (commit them); the derived index "
                    "tiers stay machine-local"
                ),
            }
        # Global birth: an explicit name is the point of an explicit create.
        if not project:
            raise CribUserError(
                "project_create: pass project=<name> for a global project "
                "(residency is declared with store=<repo-relative path>)"
            )
        name = check_project_name(project)
        if self.paths.project_dir(name).exists():
            return {
                "project": name,
                "created": False,
                "residency": "global",
                "message": f"{name} already exists (implicit birth on first "
                "write, or a previous create) — nothing changed",
            }
        pdir = self.paths.project_dir(name)
        pdir.mkdir(parents=True, exist_ok=True)
        _skeleton(pdir)
        pcfg = ProjectConfig.load(pdir / ".cribproject", name)
        self._stamp_manifest(pcfg)
        pcfg.save(pdir / ".cribproject")
        return {
            "project": name,
            "created": True,
            "residency": "global",
            "message": (
                f"{name} created with an empty pillar skeleton + birth manifest"
            ),
        }

    def _pillar_note_count(self, pp: ProjectPaths) -> int:
        """*.md count across every pillar of a project (delete/rename reports)."""
        from .watch import PILLAR_SEGMENTS

        return sum(
            sum(1 for _ in pp.pillar_dir(seg).rglob("*.md"))
            if pp.pillar_dir(seg).is_dir()
            else 0
            for seg in PILLAR_SEGMENTS
        )

    def _drop_index_state(
        self, proj: str, *, drop_sources: bool = True, drop_bindings: bool = True
    ) -> tuple[int, list[str]]:
        """Delete/rename shared cleanup: drop EVERY chunk of `proj` (one
        project-keyed pass over all pillars + `sources/` docs), the derived
        retrieval caches, the code resident cache/epoch, and any queued
        describes. FLAGS (rename keeps what travels with the tree):
        - `drop_sources` — the in-situ doc registry (doc-sources.json). DELETE
          drops it (the registry names absolute repo paths; the namespace is
          gone). RENAME must KEEP it (the file moves with the project tree;
          its entries key on repo paths, not the project name) — wiping it
          silently lost every `sources/` doc in the first rename pass.
        - `drop_bindings` — mirror bindings. DELETE drops them. RENAME must
          REBIND them (MemoryBindings.rebind) — dropping unbound the harness
          mirror for good.

        Returns (chunks removed, mirror roots dropped/rebound). The
        symbol_index is NOT touched here — it travels with the project tree
        (delete removes the tree; rename moves it)."""
        chunks = list(self.store.get_meta({"project": proj}))
        if chunks:
            self.store.delete(chunks)
        self.index.invalidate_caches(proj)
        if drop_sources:
            reg = self._source_roots(proj)
            for prefix in list(reg.all()):
                reg.remove(prefix)
        mirrors_dropped = (
            self.memory_bindings.drop_project(proj) if drop_bindings else []
        )
        self.code.cache.pop(proj, None)
        self._bump_code_epoch(proj)
        if self._describe_q is not None:
            self._describe_q.drop_project(proj)
        return len(chunks), mirrors_dropped

    async def project_delete(
        self,
        project: str | None = None,
        cwd: Path | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Fenced removal of a project namespace: its chunks, derived caches,
        code index state, mirror bindings — and the machine-local tree itself,
        MOVED to `.trash/` (one-level undo, the version ring's spirit) rather
        than rm -rf'd.

        THE INVARIANT (user-set 2026-10-31): delete NEVER removes anything
        from a user repo. For an in-repo project, committed notes, the store's
        `.versions` ring, the store `.gitignore` and the repo's `.crib` all
        STAY — crib removes only its own state (index entries, caches, the
        machine-local tree). `force` is accepted for surface compatibility
        and gates nothing.

        FENCES, all loud:
        - `project=` is required — deletion never resolves a session default
          (an accidental `delete` with no selector must not guess).
        - the default project is refused: a shared bucket has no owner to
          consent (use `project_forget` for its index only).
        - a running code sweep or background reconcile is refused (cancel/wait
          first — racing one deletes from under it).

        Both version rings survive: the global ring (keyed by note id) stays,
        and an in-repo ring lives in a store delete never touches. To
        actually remove notes that live in a repo, `project release` them
        first (then delete — or edit the repo yourself)."""
        if not project:
            raise CribUserError(
                "project_delete: pass project=<name> — deletion never resolves "
                "the session default"
            )
        proj = check_project_name(project)
        del cwd  # deliberately unused: the selector above is the whole answer
        plan = self._delete_plan(proj, force)
        if not plan["exists_anywhere"]:
            return {
                "project": proj,
                "deleted": False,
                "message": "nothing to delete — no such project on this machine",
            }
        ring_ids = plan["ring_ids"]
        trash: str | None = None
        # THE INVARIANT: a user repo is never written-removed by delete. For
        # an in-repo project the store (committed notes, their .versions ring,
        # crib's .gitignore, even the .crib itself) stays exactly as it is —
        # delete removes crib's OWN state only (see _delete_plan).
        # The destructive phase holds the project's code lock: a watcher save
        # writing `projects/<name>/symbol_index` mid-move would resurrect a
        # phantom tree under our feet.
        with self.code.lock(proj):
            # Watchers forget FIRST: a delete/move event racing an unschedule
            # is harmless (decode→index_file no-ops), but a kept root re-creates
            # the namespace on the next save. (This stops crib WATCHING the
            # repo — it removes nothing from it.)
            if self._code_watcher is not None:
                self._code_watcher.forget_project(proj)
            if (
                plan["in_repo"]
                and plan["store_available"]
                and self._watcher is not None
            ):
                self._watcher.forget_store(plan["store_root"])
            # The global tree (machine-local stub + derived caches) travels to
            # the trash — it is shared-git/data-root state, undo for free.
            # The user repo's store is deliberately NOT touched here.
            if plan["tree_exists"]:
                tdir = self.paths.trash_dir
                tdir.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                # proj is a VALIDATED name (check_project_name at the gate:
                # one [A-Za-z0-9._-] segment, never a path), so this join
                # cannot traverse — the rule is syntactic, not semantic here.
                # pi-lens-ignore: python-path-traversal
                dst = tdir / f"{proj}-{stamp}"
                # create/delete/create/delete within one second collides on
                # the timestamp — suffix on and keep going, never refuse
                dst = (
                    tdir / f"{proj}-{stamp}-{os.urandom(2).hex()}"
                    if dst.exists()
                    else dst
                )
                self.paths.project_dir(proj).rename(dst)
                trash = str(dst)
        chunks_removed, mirrors_dropped = self._drop_index_state(proj)
        # a second pass is deliberately cheap: chunks a racing sweep re-added
        # mid-delete (hash-gated) go with it too
        self._drop_index_state(proj)
        self.project_paths.invalidate(proj)
        from .session import retarget_sessions

        retarget_sessions(proj, None)
        # the live mirror holds its start-time _watched snapshot — a harness
        # edit would otherwise re-sync into the deleted namespace
        if self._mirror is not None:
            self._mirror.refresh()
        return {
            "project": proj,
            "deleted": True,
            "notes_removed": plan["notes_count"],
            "chunks_removed": chunks_removed,
            # BOTH rings survive now: the global ring stays, and an in-repo
            # ring lives in a store delete never touches
            "ring_entries_kept": len(ring_ids),
            "mirror_bindings_dropped": len(mirrors_dropped),
            "trash": trash,
            # stated for the surface: the invariant means this is ALWAYS false
            "store_deleted": False,
            "store": plan["store_token"],
            "message": (
                f"{proj} deleted ({plan['notes_count']} crib-index entries, "
                f"{chunks_removed} chunks)"
                + (f"; machine-local tree kept at {trash}" if trash else "")
                + (
                    "; notes live in a repo and were NOT touched: they remain "
                    f"at {plan['store_token']} — to also remove them there, "
                    "`crib project release` first (or edit the repo yourself)"
                    if plan["in_repo"]
                    else ""
                )
                + (
                    "; its repo .crib still names it — a write from that "
                    "checkout will recreate the namespace (run `crib project "
                    "release <name>` first to move notes back, or drop the "
                    ".crib `store:` line)"
                    if plan["in_repo"]
                    else ""
                )
            ),
        }

    def _delete_plan(self, proj: str, force: bool) -> dict[str, Any]:
        """The fences + preview for `project_delete` — the one place the fences
        are written, used both by the verb and by the MCP surface's pre-confirm
        check (the client must be told what it is agreeing to BEFORE the
        elicitation, and a fence refusal must not cost a round-trip the user
        confirms into).

        THE INVARIANT (user-set 2026-10-31): delete NEVER removes anything
        from a user repo. Whatever a store's location, only crib-owned state
        is removed — index entries (the vector store), local derived caches, the global
        tree to `.trash/`, mirror bindings, session pointers. The user's repo
        (committed notes, `.crib`, store dir) is untouched, so `force` no
        longer gates anything here either; it is accepted for surface
        compatibility and ignored."""
        from .watch import PILLAR_SEGMENTS

        if proj is None:
            raise CribUserError(
                "project_delete: pass project=<name> — deletion never resolves "
                "the session default"
            )
        if proj == self.config.default_project:
            raise CribUserError(
                f"refusing to delete the default project {proj!r} — the shared "
                "bucket has no owner to consent; use `project_forget` to clear "
                "only its index"
            )
        if proj in self.code.sweeps or proj in self.code_jobs:
            raise CribUserError(
                f"a code-index sweep is running for {proj!r} — project_cancel "
                "or project_wait it first"
            )
        if self._reconcile_remaining is not None:
            # the startup/background reconcile walks every project; a delete
            # racing its per-project reindex leaves the sweep re-adding chunks
            # (and, mid-tree, reading dirs that are moving away)
            raise CribUserError(
                "a background reconcile sweep is running — wait for it to "
                "finish (see `crib status`) and retry"
            )
        pp = self.project_paths(proj)
        gdir = self.paths.project_dir(proj)
        chunks = list(self.store.get_meta({"project": proj}))
        mirrors = [b for b in self.memory_bindings.all() if b.get("project") == proj]
        tree_exists = gdir.exists()
        return {
            "exists_anywhere": tree_exists or pp.in_repo or chunks or mirrors,
            "tree_exists": tree_exists,
            "notes_count": self._pillar_note_count(pp)
            if tree_exists or (pp.in_repo and pp.available)
            else 0,
            "chunks_count": len(chunks),
            "mirrors_count": len(mirrors),
            "ring_ids": [
                nid
                for seg in PILLAR_SEGMENTS
                if (srcd := pp.pillar_dir(seg)).is_dir()
                for nid in self._note_ring_ids(proj, srcd)
            ],
            "in_repo": pp.in_repo,
            "store_available": pp.in_repo and pp.available,
            # the invariant, stated in the data: crib never removes repo content
            "will_remove_store": False,
            "store_root": pp.store_root,
            "store_token": pp.store_token,
        }

    async def project_rename(
        self,
        project: str | None = None,
        to: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Rename a project namespace: `projects/<old>` → `projects/<new>`, with
        the identity churn made explicit rather than discovered.

        What travels: the pillar tree (incl. claude-memory mirrors), the machine
        stub, the symbol_index (its entries key on repo paths, not the project
        name), and the global version ring — note ids live in frontmatter and
        survive untouched. What CHURNS: every chunk id (`chunk_id` hashes the
        project name), so the verb deletes the old chunks and reindexes under
        the new name — a full re-embed of the project, the known cost of a rare
        verb. Note files lacking `id:` (their identity was the derived fallback,
        which embeds the project name) are STAMPED with the pre-rename derived
        id first, so their ring history and cross-references stay resolvable.

        Fences: explicit `project=` (never the session default), a target name
        that doesn't exist, the default project pinned (like delete), and an
        in-repo store that isn't on this machine. Other projects' `.crib`
        `refs:` naming the old name are REPORTED, not edited (they live in
        foreign repos — fix each by hand or re-run its setup)."""
        from .watch import PILLAR_SEGMENTS

        if not project:
            raise CribUserError(
                "project_rename: pass project=<name> — renaming never resolves "
                "the session default"
            )
        if not to:
            raise CribUserError("project_rename: pass to=<new name>")
        old = check_project_name(project)
        del cwd  # deliberately unused, same rule as delete
        new = check_project_name(to)
        if old == new:
            raise CribUserError(f"rename to the same name ({old!r}) does nothing")
        if old == self.config.default_project or new == self.config.default_project:
            raise CribUserError(
                "the default project name is pinned — rename another project, "
                "or change [config] default_project first"
            )
        if self.paths.project_dir(new).exists():
            raise CribUserError(
                f"a project named {new!r} already exists — refusing to merge "
                "two namespaces (delete the empty one first)"
            )
        if old in self.code.sweeps or old in self.code_jobs:
            raise CribUserError(
                f"a code-index sweep is running for {old!r} — project_cancel "
                "or project_wait it first"
            )
        if self._reconcile_remaining is not None:
            raise CribUserError(
                "a background reconcile sweep is running — wait for it to "
                "finish (see `crib status`) and retry"
            )
        pp = self.project_paths(old)
        gdir = self.paths.project_dir(old)
        if not gdir.exists() and not pp.in_repo:
            raise CribUserError(f"no such project: {old!r}")
        if pp.in_repo and not pp.available:
            raise CribUserError(
                f"{old} keeps notes in a repo at {pp.store_token}, which is not "
                "on this machine — clone it (or `project release`) first"
            )
        # Stamp id-less notes with their CURRENT derived id before the move: the
        # derivation embeds the project name, so post-rename they would silently
        # re-derive to different ids — their ring history unreachable. Falsy
        # `id:` values (empty/null in a hand-edited header) are OVERWRITTEN, not
        # kept; a derivation that collides with an id another file already owns
        # is SKIPPED and reported (stamping it would forge a duplicate id — the
        # honest outcome is an un-stamped note whose ring entry orphans, same
        # as pre-rename).
        # One walk: real frontmatter ids own their identity (seen_ids); idless
        # notes are candidates for stamping. (_note_ring_ids DERIVES fallbacks
        # for idless files — seeding seen_ids with those would make every
        # stamp attempt look like a collision.)
        seen_ids: set[str] = set()
        idless: list[tuple[Path, str]] = []  # (file, pillar-relative relpath)
        for seg in PILLAR_SEGMENTS:
            root = pp.pillar_dir(seg)
            if not root.is_dir():
                continue
            for p in sorted(root.rglob("*.md")):
                try:
                    raw = p.read_text()
                except OSError:
                    continue
                if nid := notes.scan_id(raw):
                    # `id: null`/`~` scan as the literal strings "null"/"~" —
                    # the canonical id (fm.get("id") or derived) treats those
                    # as ABSENT, so they are stamp candidates, not owners
                    if nid.lower() in ("null", "~", "none"):
                        idless.append((p, str(p.relative_to(root))))
                    else:
                        seen_ids.add(nid)
                else:
                    idless.append((p, str(p.relative_to(root))))
        stamped = 0
        stamp_collisions = 0
        for p, rel in sorted(idless):
            try:
                note = notes.load(p)
            except NoteParseError:
                continue
            derived = derived_ulid(old, rel)
            if derived in seen_ids:
                stamp_collisions += 1
                continue
            seen_ids.add(derived)
            note.frontmatter["id"] = derived  # overwrites a falsy `id:` too
            notes.save_atomic(note)  # identity stamp, not a revision
            stamped += 1
        ring_ids = [
            nid
            for seg in PILLAR_SEGMENTS
            if (srcd := pp.pillar_dir(seg)).is_dir()
            for nid in self._note_ring_ids(old, srcd)
        ]
        # Drop what churns; KEEP what travels. The doc-sources registry moves
        # with the tree (its entries key on repo paths) — wiping it would
        # silently lose every `sources/` doc; mirror bindings are REBOUND
        # below, not dropped (dropping unbound the harness mirror for good).
        chunks_removed, _ = self._drop_index_state(
            old, drop_sources=False, drop_bindings=False
        )
        mirror_bound = self.memory_bindings.rebind(old, new)
        # The destructive phase holds the project's code lock (a watcher save
        # writing symbol_index mid-move would resurrect a phantom tree).
        with self.code.lock(old):
            # Watchers forget FIRST (a kept root re-creates the namespace).
            if self._code_watcher is not None:
                self._code_watcher.forget_project(old)
            if self._watcher is not None and pp.store_root is not None:
                self._watcher.forget_store(pp.store_root)
            if not gdir.exists():  # pragma: no cover — in_repo requires the stub
                raise CribUserError(
                    f"no project tree for {old!r} on this machine — cannot rename"
                )
            gdir.rename(self.paths.project_dir(new))
        # a racing sweep's re-added chunks go too (cheap post-move pass)
        self._drop_index_state(old, drop_sources=False, drop_bindings=False)
        self.project_paths.invalidate(old)
        self.project_paths.invalidate(new)
        from .session import retarget_sessions

        retarget_sessions(old, new)
        # the live mirror holds its start-time _watched snapshot — a harness
        # edit would otherwise re-sync into the OLD name after a rename
        if self._mirror is not None:
            self._mirror.refresh()
        pcfg = ProjectConfig.load(self.paths.project_dir(new) / ".cribproject", new)
        pcfg.name = new
        pcfg.save(self.paths.project_dir(new) / ".cribproject")
        # NO manifest stamp here: a rename never fakes a birth record — an
        # implicitly-born project stays implicitly-born under its new name.
        # THIS project's own repo `.crib` still says `project: <old>` — every
        # cwd-resolved read in that repo would resolve to the (now empty)
        # old namespace, and implicit creation would resurrect it. Rewrite
        # that one line — it is THIS project's repo, not a foreign one.
        crib_needs_fix: str | None = None
        crib_rewritten: str | None = None
        own_root = CribLink.find(pp.store_root) if pp.store_root is not None else None
        if own_root is None or own_root.root is None:
            from .codeindex import SymbolIndex

            si_root = SymbolIndex(self.paths.project_dir(new)).source_root()
            own_root = CribLink.find(si_root) if si_root is not None else None
        if (
            own_root is not None
            and own_root.root is not None
            and own_root.project == old
        ):
            crib_file = own_root.root / ".crib"
            text = crib_file.read_text()
            lines, rewritten = [], False
            for ln in text.splitlines(keepends=True):
                if ln.strip() == f"project: {old}":
                    lines.append(f"project: {new}\n")
                    rewritten = True
                else:
                    lines.append(ln)
            if rewritten:
                crib_file.write_text("".join(lines))
                crib_rewritten = str(crib_file)
            else:
                # a hand-spelled variant we won't guess at — report it
                crib_needs_fix = str(crib_file)
        # Re-point the live watchers under the new name — INCLUDING the in-repo
        # store's pillar tree (a rename is exactly when it would silently go
        # unwatched until the next daemon start otherwise).
        if self._code_watcher is not None:
            from .codeindex import SymbolIndex

            if (
                root := SymbolIndex(self.paths.project_dir(new)).source_root()
            ) is not None and root.is_dir():
                self._code_watcher.watch_root(new, root)
        if self._watcher is not None and pp.store_root is not None:
            self._watcher.add_store(new, pp.store_root)
        rec = await self.reindex(project=new)
        # In-situ `sources/…` docs are not part of the pillar walk reindex ran;
        # their registry moved intact, so this re-registers/indexes under the
        # new name (best-effort: a repo gone from disk just has no docs).
        insitu_docs = 0
        try:
            insitu_docs = int(
                (await self.index_docs_insitu(project=new)).get("changed", 0)
            )
        except Exception as e:  # noqa: BLE001 — no repo root / no docs
            print(
                f"[crib] rename: in-situ doc reindex skipped for {new}: {e}",
                file=sys.stderr,
            )
        refs_affected = [
            {
                "referrer": p,
                "target": r.get("project"),
                "root": str(r["root"]) if r.get("root") else None,
            }
            for p in self.projects()
            if p != new
            for r in self._project_refs(p)
            if r.get("project") == old
        ]
        return {
            "project": new,
            "from": old,
            "renamed": True,
            "id_less_notes_stamped": stamped,
            "id_less_stamp_collisions": stamp_collisions,
            "chunks_rebuilt": chunks_removed,
            "ring_entries_carried": len(ring_ids),
            "mirror_bindings_moved": mirror_bound,
            "insitu_docs_reindexed": insitu_docs,
            "crib_rewritten": crib_rewritten,
            "crib_needs_fix": crib_needs_fix,
            "refs_affected": refs_affected,
            "reindex": rec,
            "message": (
                f"{old} → {new}: {stamped} id-less note(s) stamped, "
                f"{chunks_removed} chunk(s) re-embedded, "
                f"{len(ring_ids)} ring entry/entries carried"
                + (f"; {insitu_docs} in-situ doc(s) re-indexed" if insitu_docs else "")
                + (
                    f"; own .crib rewritten ({crib_rewritten})"
                    if crib_rewritten
                    else ""
                )
                + (
                    f"; .crib at {crib_needs_fix} still says project: {old} "
                    "(hand-edit line)"
                    if crib_needs_fix
                    else ""
                )
                + (
                    "; cross-project refs name the old name: "
                    + ", ".join(sorted({e["referrer"] for e in refs_affected}))
                    + " — update their repos' .crib"
                    if refs_affected
                    else ""
                )
            ),
        }

    # ── layout migration: facet subdirs → sibling pillar stores ────────────────
    # Pre-split layouts kept facet notes INSIDE the notes tree (`notes/design/`,
    # `notes/plans/`, `notes/code-learnings/`). The migration moves them to the
    # sibling pillar dirs and requalifies facet-note citations. It is idempotent
    # and runs automatically from every full reindex (the sweep IS the migration,
    # like the chunk-schema story) — which is also what heals old-layout files a
    # lagging, not-yet-upgraded machine pushes into the synced data repo.

    # legacy subdir under notes/ → the pillar store that owns it now.
    # read-only constant lookup table, intentionally shared (never mutated).
    # pi-lens-ignore: python-mutable-class-attr
    _LEGACY_FACET_DIRS = {
        "design": "design",
        "plans": "plans",
        "code-learnings": "learnings",
    }

    def _migrate_legacy_layout(self, proj: str) -> dict[str, Any]:
        """Move facet notes out of the notes tree and requalify their citations.
        Pure file ops (no indexing) — the caller's reindex sweep does the rest:
        the notes sweep drops the old-path chunks, each pillar sweep embeds its
        moved files. Cheap no-op when no legacy dir exists."""
        pp = self.project_paths(proj)
        moved: list[str] = []
        skipped: list[dict[str, str]] = []
        nd = pp.notes_dir
        for legacy_sub, store_name in self._LEGACY_FACET_DIRS.items():
            src_dir = nd / legacy_sub
            if not src_dir.is_dir():
                continue
            dst_root = pp.pillar_dir(store_name)
            for src in sorted(src_dir.rglob("*.md")):
                rel = src.relative_to(src_dir)
                dst = dst_root / rel
                if dst.exists():
                    # Never clobber: both layouts hold this name (a divergent
                    # pull). A human picks the survivor; re-running finishes.
                    skipped.append({"from": str(src), "to": str(dst)})
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
                moved.append(f"{store_name}/{rel}")
            # prune emptied legacy dirs (deepest first; each only if empty)
            for d in sorted(src_dir.rglob("*"), reverse=True):
                if d.is_dir():
                    with contextlib.suppress(OSError):
                        d.rmdir()
            with contextlib.suppress(OSError):
                src_dir.rmdir()
        rewritten = self._requalify_facet_refs(proj)
        if moved or skipped or rewritten:
            note = f"; {len(skipped)} collision(s) left in place" if skipped else ""
            print(
                f"[crib] {proj}: migrated {len(moved)} facet note(s) to sibling "
                f"pillar stores, requalified {len(rewritten)} citation(s){note}",
                file=sys.stderr,
            )
        return {"moved": moved, "skipped": skipped, "refs_rewritten": rewritten}

    def _requalify_facet_refs(self, proj: str) -> list[str]:
        """Rewrite `sources[].ref` values that still use the pre-split spelling
        (`design/x.md` → `design:x.md`) in every design/plan note. Frontmatter
        only — bodies are NEVER touched, so no dependent gains taint — and the
        prior bytes are ring-stashed first. Idempotent: a qualified ref is left
        alone."""
        out: list[str] = []
        for pillar in (self.designstore, self.planstore):
            root = pillar.root(proj)
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*.md")):
                try:
                    note = notes.load(path)
                except Exception:  # noqa: BLE001 — a broken note is not this sweep's job
                    continue
                srcs = note.frontmatter.get("sources")
                if not isinstance(srcs, list):
                    continue
                changed = False
                for src in srcs:
                    if not isinstance(src, dict):
                        continue
                    ref = str(src.get("ref") or "")
                    for legacy_sub, store_name in self._LEGACY_FACET_DIRS.items():
                        if ref.startswith(legacy_sub + "/"):
                            src["ref"] = f"{store_name}:{ref[len(legacy_sub) + 1 :]}"
                            changed = True
                if changed:
                    rel = path.name
                    pillar._stash_existing(proj, rel, path, note.id)
                    notes.save_atomic(note)
                    out.append(f"{pillar.spec.name}/{rel}")
        return out

    async def project_relocate(
        self,
        project: str | None = None,
        project_path: str | None = None,
        source: str | None = None,
        keep_prefix: bool = False,
        wait_s: float | None = None,
    ) -> dict[str, Any]:
        """The move-and-rename verb: re-point an already-indexed project at its
        repo's NEW location on this machine — the checkout was renamed and/or
        moved; works AFTER THE FACT (the old path may already be gone, the
        project may be renamed, and a PREVIOUS relocate may have been cut
        halfway — re-running RESUMES what's left, never double-registering).

        The FAST half runs synchronously and idempotently: fences; the code
        index's `.source_root` rewrite (entries key on repo-RELATIVE paths, so
        the same index serves the new checkout); the doc-sources registry's
        dead-root re-points; the repo's OWN `.crib` `project:` line (when it
        names the pre-rename name — fenced by dead-source registry evidence;
        foreign .cribs are refused); the machine stub's portable store_root for
        an adopted project (the STORE moved with the repo); the watchers.

        The HEAVY half — re-homing the `sources/<old-name>/` mount when the DIR
        was renamed: doc chunks rekeyed (re-embedded from the new checkout) and
        every citation in THIS project's notes rewritten to the new spelling
        (ring-stashed first) — runs as a DAEMON-OWNED BACKGROUND JOB: a client
        disconnect (e.g. the combiner's ~90s call cap) can never cancel it
        mid-embed. Ordering is content-first, so an interrupt at ANY point
        leaves a resolvable state: a doc is re-embedded under the NEW spelling
        BEFORE its old chunks drop, and the old mount's registry entry is
        removed LAST — mid-job, BOTH mounts resolve the same files, so citations
        never go source-missing in passing.

        `wait_s=None` (default) returns a RUNNING HANDLE immediately (with
        live progress + a resume hint); `wait_s=<seconds>` JOINS the job,
        bounded like `project_index`'s wait; a re-invoke joins automatically
        while it runs. `keep_prefix=true` = identity-keeping only: no mount
        rename, no rehash (a docs re-hash would double-mount
        `sources/<new-name>/` beside the kept old mount).

        The foreign residue — other projects citing this repo by the old
        spellings — is REPORTED (`foreign_refs`), never edited (rename's
        rule: each registry is per-project and machine-local)."""
        from .config import CribLink, ProjectConfig, portable_path
        from .sources import SRC_PREFIX
        from .watch import PILLAR_SEGMENTS

        if not project or not project_path:
            raise CribUserError(
                "project_relocate: pass project=<name> AND "
                "project_path=<the repo's new dir> — a relocation names both, "
                "never the session default and never a cwd guess"
            )
        old = check_project_name(project)
        new_root = Path(project_path).expanduser().resolve()
        if not new_root.is_dir():
            raise CribUserError(f"no such directory: {new_root}")
        # a relocate JOB of ours may be mid-flight WITH its own trailing code
        # sweep (the rehash runs inside the job) — that must JOIN, not fence:
        # the sweep-conflict refusal exists only when no relocate context exists.
        if old not in self.relocate_jobs and (
            old in self.code.sweeps or old in self.code_jobs
        ):
            raise CribUserError(
                f"a code-index sweep is running for {old!r} (not a relocate's "
                "own) — project_cancel or project_wait it first; a relocate in "
                "flight would have joined instead"
            )
        if self._reconcile_remaining is not None:
            raise CribUserError(
                "a background reconcile sweep is running — wait for it to "
                "finish (see `crib status`) and retry"
            )
        pp = self.project_paths(old)
        gdir = self.paths.project_dir(old)
        if not gdir.exists() and not pp.in_repo:
            raise CribUserError(f"no such project: {old!r}")

        link = CribLink.find(new_root)
        if link is None or link.root is None:
            raise CribUserError(
                f"no .crib found from {new_root} upward — a moved repo carries "
                "its own committed .crib; if this clone lacks one, set it up "
                "(project_setup / a hand-written .crib naming the project) and "
                "retry"
            )
        if link.project not in (old, project):
            # OUR-stale case: the repo still names the PRE-RENAME project — the
            # rename itself declares this rewrite legal, but could not run (the
            # dir had already moved). Evidence required: THIS project's registry
            # holds a (dead OR already-re-pointed) source for that repo. No
            # evidence ⇒ foreign; refuse.
            ro0 = self._source_roots(old)
            stale_evidence = any(
                (not Path(r).is_dir()) or Path(r) == new_root
                for p, r in ro0.all().items()
                if p[len(SRC_PREFIX) :].strip("/") in (link.project, link.root.name)
            )
            if not stale_evidence:
                raise CribUserError(
                    f"{new_root} belongs to project {link.project!r} (its .crib "
                    f"says so), not {old!r} — relocations are about THIS repo"
                )

        # 1. The repo's own .crib, when it still names the pre-rename project —
        #    the exact rewrite project_rename does, which could not run here
        #    because the dir had ALREADY moved when the rename was called.
        crib_rewritten: str | None = None
        if link.project != old:
            crib_file = link.root / ".crib"
            text = crib_file.read_text()
            lines, rewritten = [], False
            for ln in text.splitlines(keepends=True):
                if ln.strip() == f"project: {link.project}":
                    lines.append(f"project: {old}\n")
                    rewritten = True
                else:
                    lines.append(ln)
            if not rewritten:
                raise CribUserError(
                    f"{crib_file} does not name its project on its own line — "
                    "edit it (project: " + old + ") and retry"
                )
            crib_file.write_text("".join(lines))
            crib_rewritten = str(crib_file)
            link = CribLink.find(new_root)  # reload under the corrected name
            if link is None or link.root is None:  # pragma: no cover — just rewrote it
                raise CribUserError(f"created .crib at {new_root} failed to parse")

        # 2. The machine stub's portable store_root, for an in-repo store: the
        #    notes live in the repo (they moved WITH it), but the stub still
        #    pointed at the old machine path.
        store_repointed: str | None = None
        if pp.in_repo and pp.store_root is not None and not pp.store_root.exists():
            if link.store_dir is None:
                raise CribUserError(
                    f"{old} was in-repo at a store that moved with the repo, but "
                    f"{link.root / '.crib'} declares no `store:` — fix the .crib "
                    "and retry"
                )
            for seg in PILLAR_SEGMENTS:  # pragma: no cover — store sanity
                (link.store_dir / seg).mkdir(parents=True, exist_ok=True)
            pcfg = ProjectConfig.load(pp.config_file, old)
            pcfg.store_root = portable_path(link.store_dir, self.config.locations)
            pcfg.save(pp.config_file)
            self.project_paths.invalidate(old)
            if self._watcher is not None:
                self._watcher.forget_store(pp.store_root)
                self._watcher.add_store(old, link.store_dir)
            store_repointed = str(link.store_dir)

        # 3. The doc-sources registry: prefixes whose root is dead AND whose
        #    repo-segment matches the new dir's name or the project name — the
        #    repo was renamed, so the segment can be either. `source=` picks
        #    explicitly when the match is ambiguous. IDEMPOTENT: a re-run sees
        #    no dead roots and re-points nothing.
        ro = self._source_roots(old)
        dead = {
            p: r
            for p, r in ro.all().items()
            if not Path(r).is_dir() and p.startswith(SRC_PREFIX)
        }
        if source:
            seg = source.strip("/")
            cands = {
                p: r for p, r in dead.items() if p[len(SRC_PREFIX) :].strip("/") == seg
            }
            if not cands:
                raise CribUserError(
                    f"no registered source matches {source!r} for {old!r} under "
                    f"{pp.project_dir} (registered: {sorted(ro.all()) or 'none'})"
                )
        else:
            seg_of = lambda p: p[len(SRC_PREFIX) :].strip("/")  # noqa: E731
            cands = {p: r for p, r in dead.items() if seg_of(p) in (new_root.name, old)}
            if not cands and len(dead) == 1:
                cands = dict(dead)  # one dead source: it is unambiguous
            elif not cands and len(dead) > 1:
                raise CribUserError(
                    f"{old} has several dead in-situ sources "
                    f"{sorted(dead)} — pass source=<repo segment> for this "
                    "relocation"
                )
            if not cands:
                cands = {}
        prefixes_repointed: list[str] = []
        for p in cands:
            ro.upsert(p, new_root)  # re-point BEFORE the rekey: mid-job, both
            prefixes_repointed.append(p)  # mounts resolve the same files

        # 4. The code index's source root: entries key on repo-RELATIVE paths,
        #    so this one rewrite re-homes the whole index.
        from .codeindex import SymbolIndex

        symbol_index = SymbolIndex(gdir)
        old_si_root = symbol_index.source_root()
        source_root_repointed = False
        if old_si_root != new_root and (
            old_si_root is None or not old_si_root.exists()
        ):
            symbol_index.set_source_root(new_root)
            self.code.cache.pop(old, None)
            self._bump_code_epoch(old)
            if self._code_watcher is not None:
                self._code_watcher.watch_root(old, new_root)  # re-point
            source_root_repointed = True

        # 5. The HEAVY half as a daemon-owned job. The resume set is detected
        #    from the REGISTRY, not from dead paths: every mount of THIS repo
        #    (root == the new checkout) whose name still spells the old dir
        #    rekeys + rewrites citations + is removed last.
        job = self.relocate_jobs.get(old)
        running = job is not None and not job["task"].done()
        pending = (
            [
                p
                for p, r in ro.all().items()
                if r == str(new_root) and p != f"{SRC_PREFIX}{new_root.name}/"
            ]
            if not keep_prefix and not running
            else []
        )
        task = None
        state: dict[str, Any] | None = job["state"] if job is not None else None
        if not keep_prefix and not running and pending:
            job_state: dict[str, Any] = {
                "done": 0,
                "total": 0,
                "citations": 0,
                "mount": [],
                "chunks_lost_restored": 0,
            }
            task = asyncio.create_task(
                self._relocate_rekey(
                    old,
                    new_root,
                    pending,
                    job_state,
                    fast={
                        "source_root": (
                            {"from": str(old_si_root), "to": str(new_root)}
                            if source_root_repointed
                            else None
                        ),
                        "prefixes_repointed": prefixes_repointed,
                        "store_repointed": store_repointed,
                        "crib_rewritten": crib_rewritten,
                    },
                )
            )
            self.relocate_jobs[old] = {"task": task, "state": job_state}
            state = job_state
        elif running and job is not None:
            task = job["task"]

        if task is not None and not task.done():
            if wait_s is None:
                return {
                    "project": old,
                    "relocated": True,
                    "running": True,
                    "progress": state,
                    "resume_hint": (
                        "the rekey runs daemon-owned (a client disconnect can't "
                        "cancel it) — POLL it, don't block: project_status "
                        "carries the live progress; re-invoking the verb joins "
                        "or finishes it"
                    ),
                }
            started = time.monotonic()
            while not task.done() and time.monotonic() - started < wait_s:
                await asyncio.wait({task}, timeout=2.0)
            if task.done():
                return await task
            return {
                "project": old,
                "relocated": True,
                "running": True,
                "progress": state,
                "resume_hint": (
                    "still rekeying — poll project_status; re-invoking joins"
                ),
            }

        # 6. The FINISHED path (or nothing to do): the heavy result if a job ran
        #    to completion in this call, else the plain report. The rehash is
        #    part of the job; nothing runs here.
        if job is not None and job["task"].done():
            task = job["task"]
            res = await task  # the job's result dict (raises if it failed)
            self.relocate_jobs.pop(old, None)
            return res

        # the honest residue: OTHER projects still spelling this repo the old
        # way — foreign repos/notes are never edited, only reported (rename's
        # rule), so a human fixes each or re-runs its setup.
        foreign_refs = [
            {
                "referrer": p,
                "target": r.get("project"),
                "root": str(r["root"]) if r.get("root") else None,
            }
            for p in self.projects()
            if p != old
            for r in self._project_refs(p)
            if r.get("project") == old
        ]
        if job is not None and job["task"].done():
            self.relocate_jobs.pop(old, None)
        return {
            "project": old,
            "relocated": True,
            "root": str(new_root),
            "source_root": (
                {"from": str(old_si_root), "to": str(new_root)}
                if source_root_repointed
                else None
            ),
            "prefixes_repointed": prefixes_repointed,
            # uniform shape with the heavy job's result (keep_prefix / nothing-
            # to-rehome paths): no mount work ran, so these are empty
            "mount_renamed": [],
            "chunks_rekeyed": 0,
            "chunks_lost_restored": 0,
            "citations_rewritten": 0,
            "reindex": None,
            "foreign_refs": foreign_refs,
            "store_repointed": store_repointed,
            "crib_rewritten": crib_rewritten,
            "message": (
                f"relocated {old!r}'s repo to {new_root} — identity kept "
                "(no re-embed); nothing to re-home"
                + ("; .crib line rewritten" if crib_rewritten else "")
                + ("; in-repo store re-pointed" if store_repointed else "")
                + (
                    f"; {len(foreign_refs)} foreign ref(s) reported (not edited)"
                    if foreign_refs
                    else ""
                )
            ),
        }

    async def _relocate_rekey(
        self,
        proj: str,
        new_root: Path,
        old_prefixes: list[str],
        state: dict[str, Any],
        fast: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The heavy relocate half, daemon-owned: re-home each stale mount of
        THIS repo. CONTENT-FIRST and resumable at every step — a doc is
        re-embedded under the NEW spelling BEFORE its old chunks drop; the old
        mount's registry entry is removed LAST; citations are rewritten after
        (an already-rewritten ref no longer matches, so re-running finishes
        rather than double-writing); the prose + code + docs rehash closes, which
        also RESTORES doc chunks a rename's skipped in-situ reindex lost (the
        after-the-fact case)."""
        from .sources import SRC_PREFIX

        new_prefix = f"{SRC_PREFIX}{new_root.name}/"
        ro = self._source_roots(proj)
        mount_renamed: list[dict[str, str]] = []
        chunks_rekeyed = citations_rewritten = chunks_lost_restored = 0
        state["total"] = state["total"] or 0
        for old_prefix in old_prefixes:
            nd = self.notes_dir(proj)
            rels = sorted(self._indexed_relpaths(proj, old_prefix))
            suffixes = {
                rp[len(old_prefix) :]: new_root / rp[len(old_prefix) :] for rp in rels
            }
            state["total"] += len(suffixes)
            for suffix, target in sorted(suffixes.items()):
                new_rel = f"{new_prefix}{suffix}"
                if target.is_file():
                    # CONTENT FIRST: the new-spelling chunks exist (hash gate
                    # skips when already there) before anything is dropped
                    res = await self.index.index_file(
                        proj, nd, new_rel, content_path=target
                    )
                    if res.changed:
                        chunks_rekeyed += 1
                        state["done"] += 1
                    else:
                        state["done"] += 1  # already rekeyed (a resume)
                await self.index.forget(proj, old_prefix + suffix)
            # any DOC the index knows under the old spelling with NO surviving
            # file under the new (a rename's in-situ loss) is re-indexed from
            # the target tree in the rehash below
            chunks_lost_restored += len(
                {
                    f"{new_prefix}{suffix}"
                    for suffix in suffixes
                    if not suffixes[suffix].is_file()
                }
            )
            # ...and the citations, in every pillar note THIS project owns
            for pillar in (self.notestore, self.designstore, self.planstore):
                root = pillar.root(proj)
                if not root.is_dir():
                    continue
                for path in sorted(root.glob("**/*.md")):
                    try:
                        note = notes.load(path)
                    except Exception:  # noqa: BLE001 — not citable, skipped
                        continue
                    srcs = note.frontmatter.get("sources")
                    if not isinstance(srcs, list):
                        continue
                    changed = False
                    for src in srcs:
                        if not isinstance(src, dict):
                            continue
                        ref = str(src.get("ref") or "")
                        if ref == old_prefix.rstrip("/") or ref.startswith(old_prefix):
                            src["ref"] = (
                                new_prefix.rstrip("/")
                                if ref == old_prefix.rstrip("/")
                                else new_prefix + ref[len(old_prefix) :]
                            )
                            changed = True
                    if changed:
                        rel = str(path.relative_to(root))
                        pillar._stash_existing(proj, rel, path, note.id)
                        notes.save_atomic(note)
                        citations_rewritten += 1
                        state["citations"] += 1
            state["finished_at"] = time.time()  # the poller's receipt
            # REMOVE LAST — after the new-spelling chunks exist and the notes
            # cite them: an interrupt above leaves the old mount present and
            # resolvable (both mounts resolve the same files mid-job)
            ro.remove(old_prefix)
            ro.upsert(new_prefix, new_root)
            state["mount"].append(
                {"from": old_prefix, "to": new_prefix, "docs": str(len(suffixes))}
            )
            mount_renamed.append(
                {"from": old_prefix, "to": new_prefix, "docs": str(len(suffixes))}
            )
        # CLOSE: the cheap confirmation rehash (code + docs, content-hash gate)
        # and the prose reindex — which re-adds any in-situ docs whose chunks a
        # rename's skipped reindex dropped (content comes from the new path).
        link = CribLink.find(new_root)
        rehash: dict[str, Any] | None = None
        if link is not None and (link.paths or link.doc_patterns):
            self.index.invalidate_caches(proj)
            rehash = await self.reindex(project=proj)  # prose first (cheap too)
            rehash["in_situ_and_code"] = await self.project_index(
                project=proj, cwd=new_root
            )
        fast = fast or {}
        return {
            "project": proj,
            "relocated": True,
            "root": str(new_root),
            "source_root": fast.get("source_root"),
            "prefixes_repointed": fast.get("prefixes_repointed") or [],
            "store_repointed": fast.get("store_repointed"),
            "crib_rewritten": fast.get("crib_rewritten"),
            "mount_renamed": mount_renamed,
            "chunks_rekeyed": chunks_rekeyed,
            "chunks_lost_restored": chunks_lost_restored,
            "citations_rewritten": citations_rewritten,
            "reindex": rehash,
            "message": (
                f"relocated {proj!r}'s repo to {new_root}"
                + (
                    "; mount renamed "
                    + "; ".join(
                        f"{m['from']} -> {m['to']} ({m['docs']} docs)"
                        for m in mount_renamed
                    )
                    if mount_renamed
                    else " (no stale mount)"
                )
                + f"; {chunks_rekeyed} doc chunk(s) rekeyed, "
                + f"{citations_rewritten} citation(s) rewritten"
                + (
                    f"; {chunks_lost_restored} lost doc(s) re-indexed"
                    if chunks_lost_restored
                    else ""
                )
            ),
        }

    async def learning_migrate(
        self, project: str | None = None, cwd: Path | None = None, apply: bool = False
    ) -> dict[str, Any]:
        """Rebind a project's learning notes to the current symbol identity.

        OPTIONAL TIDINESS, not a correctness step: the learnings join resolves a
        note bound to any prior spelling, so an unconverted note keeps working
        forever. What this buys is canonical filenames and `symbol_ref` frontmatter
        — worth doing once, not worth automating against a git-synced store.

        Per record, no ordering constraint against the entry conversion, re-running
        is the resume. DRY RUN unless `apply`."""
        proj = self.resolve_project(project, cwd)
        return await self.learnings.convert_notes(proj, apply=apply)

    async def code_convert(
        self, project: str | None = None, cwd: Path | None = None, apply: bool = False
    ) -> dict[str, Any]:
        """Convert a project's symbol store to the current shape, in place — no LSP,
        no LLM, every expensive facet byte-identical or the entry is left alone.

        Each record is independently classifiable (`schema` + whether its filename
        is derived from its own key), so the work-list is derived from the data,
        a crash leaves the ordinary input to the next run, and re-running IS the
        resume. A record that cannot be converted (no `file`/`name` to derive a
        reference from) is reported for a reindex rather than guessed at.

        A full `project_index` also converts — every write normalizes identity —
        but at LSP+LLM price. This is the cheap path. DRY RUN unless `apply`."""
        from .codeindex import SYMBOL_SCHEMA_VERSION, SymbolIndex
        from .symconvert import convert_entry, convertible, preserved

        proj = self.resolve_project(project, cwd)
        store = SymbolIndex(self.paths.project_dir(proj))
        rows = store.records()
        # COLLISION GUARD. Distinct records whose cleaned identities derive ONE key
        # cannot both be converted — the second write would silently erase the first
        # (found live: two Lua block-locals told apart only by synthetic containers,
        # `for in` vs `do end`, which the tail cleaning strips). Convert the first by
        # filename, LEAVE the rest as they are, and say so — same rule as two notes
        # claiming one symbol. A reindex is what genuinely resolves them.
        claims: dict[str, tuple[str, tuple]] = {}
        collisions: list[dict[str, Any]] = []
        done = converted = 0
        skipped: list[str] = []
        violations: list[dict[str, Any]] = []

        def _parts(e: dict) -> tuple:
            return (
                str(e.get("file") or ""),
                tuple(str(c) for c in (e.get("container") or ())),
                str(e.get("name") or ""),
            )

        with self.code.lock(proj):
            for path, e in rows:
                k = _symbol_key(e)
                holder, hparts = claims.setdefault(k, (path.name, _parts(e)))
                if holder != path.name:
                    if hparts == _parts(e):
                        # the SAME identity twice is a crash leftover (write-new
                        # happened, unlink-old did not) — ordinary work, swept here
                        if apply:
                            path.unlink(missing_ok=True)
                        converted += 1
                        continue
                    # DISTINCT identities deriving one key: converting the second
                    # would silently erase the first. Leave it, say so, every run.
                    collisions.append(
                        {"record": path.name, "key": k, "held_by": holder}
                    )
                    continue
                if int(
                    e.get("schema") or 0
                ) >= SYMBOL_SCHEMA_VERSION and path.name == store._relname(
                    _symbol_key(e)
                ):
                    done += 1
                    continue
                if not convertible(e):
                    skipped.append(path.name)
                    continue
                new = convert_entry(e, SYMBOL_SCHEMA_VERSION)
                bad = preserved(e, new)
                if bad:  # never write an entry conversion mangled
                    violations.append({"record": path.name, "fields": bad})
                    continue
                if apply:
                    wrote = store.write(new)
                    if path != wrote:  # a name no binding derives (pre-hash slug)
                        path.unlink(missing_ok=True)
                converted += 1
            if apply and not skipped and not violations and not collisions:
                store.record_schema()  # a completion claim: this pass saw every record
        if apply:
            self.code.bump_epoch(proj)
        return {
            "project": proj,
            "applied": apply,
            "total": len(rows),
            "already_done": done,
            "converted": converted,
            "needs_reindex": skipped,
            "violations": violations,
            "collisions": collisions,
        }

    async def project_migrate(
        self, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """Run the legacy-layout migration for one project on demand, then
        reconcile. The same routine every full reindex runs automatically —
        this verb exists so the move can be driven (and its report read)
        explicitly. Idempotent; collisions are skipped and reported, never
        merged."""
        proj = self.resolve_project(project, cwd)
        self.project_paths(proj).require()
        mig = self._migrate_legacy_layout(proj)
        rec = await self.reindex(project=proj)
        return {
            "project": proj,
            **mig,
            "reconciled": rec,
            "changed": bool(mig["moved"] or mig["refs_rewritten"]),
        }

    def ops_status(self) -> dict[str, Any]:
        """The ops view: what the engine is doing RIGHT NOW — live sweeps (with
        scope + failure counts via format_sweep), per-project in-flight files,
        and generation slot occupancy (in-flight / waiting / rate-limited /
        cooldown). The one call that answers "what is running and why is
        nothing moving" — the queue made visible. `generation_ledger` counts the
        targets whose background generation failed and is backing off
        (crib.genledger): a provider that keeps failing shows up here."""
        from .generate import generation_stats

        try:
            ledger = self.ledger.counts()
        except Exception as e:  # noqa: BLE001 — a status view must still answer
            ledger = {"error": str(e)}
        return {
            "sweeps": {p: dict(v) for p, v in self.code.sweeps.items()},
            "indexing": {p: list(v) for p, v in self.code.indexing.items()},
            "generation": generation_stats(),
            "generation_ledger": ledger,
        }

    def _project_refs(self, proj: str) -> list[dict[str, Any]]:
        """Delegate to Refs (crib/refs.py) — a project's `.crib` refs: targets."""
        return self.refs.project_refs(proj)

    def _job_rows(self, proj: str | None = None) -> list[dict[str, Any]]:
        """Every long-running project job that a POLL can see — the relocate
        rekey/citation phase, the code-index sweeps, the background reconcile.
        Calling this while ANY of them run is the point of it: it never errors,
        never blocks; it reports running/complete/failed + live counts, and the
        finished counts a job already banked (citations rewritten, mount named,
        chunks rekeyed)."""
        rows: list[dict[str, Any]] = []
        for p, job in sorted(self.relocate_jobs.items()):
            if proj is not None and p != proj:
                continue
            state = dict(job.get("state") or {})
            t = job["task"]
            done, error = t.done(), None
            if done:
                try:
                    exc = t.exception()  # never re-raised — reported, polled
                    if exc is not None:
                        error = f"{type(exc).__name__}: {exc}"
                except Exception as e:  # noqa: BLE001 — the poll never raises
                    error = f"{type(e).__name__}: {e}"
            rows.append(
                {
                    "kind": "relocate",
                    "project": p,
                    "phase": "rekey+rehash",
                    "running": not done,
                    "complete": done and error is None,
                    "error": error,
                    "done": state.get("done", 0),
                    "total": state.get("total", 0),
                    "citations_rewritten": state.get("citations", 0),
                    "mount_renamed": state.get("mount") or [],
                    **(
                        {"finished_at": state.get("finished_at")}
                        if state.get("finished_at")
                        else {}
                    ),
                }
            )
        for p, meta in sorted(self.code_jobs.items()):
            if proj is not None and p != proj:
                continue
            sw = self.code.sweeps.get(p) or {}
            done = meta["task"].done()
            rows.append(
                {
                    "kind": "code_index",
                    "project": p,
                    "phase": "extract+describe+embed",
                    "running": not done,
                    # done but cancelled means STOPPED, never "finished OK"
                    "complete": done and not meta["task"].cancelled(),
                    **({"cancelled": True} if meta["task"].cancelled() else {}),
                    "done": sw.get("done"),
                    "total": sw.get("total"),
                    **({"line": sw.get("line")} if sw.get("line") else {}),
                }
            )
        for p in sorted(self.code.sweeps):
            if p in self.code_jobs or (proj is not None and p != proj):
                continue
            sw = self.code.sweeps[p] or {}
            rows.append(
                {
                    "kind": "code_index",
                    "project": p,
                    "phase": "sweep (no live call; project_index re-invokes to join)",
                    "running": sw.get("running", True),
                    "complete": False,
                    "done": sw.get("done"),
                    "total": sw.get("total"),
                }
            )
        if self._reconcile_remaining is not None:
            rows.append(
                {
                    "kind": "reconcile",
                    "project": "ALL",
                    "phase": "re-embed sweep",
                    "running": True,
                    "complete": False,
                    "remaining": self._reconcile_remaining,
                }
            )
        return rows

    def project_status(
        self, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """Is this project code-indexed? symbol/file counts, kind breakdown, the
        `.crib` source paths — for orienting before setup/index. Also the POLL
        surface for long-running jobs (see `jobs`)."""
        from collections import Counter

        from .codeindex import SymbolIndex

        proj = self.resolve_project(project, cwd)
        si = SymbolIndex(self.paths.project_dir(proj))
        entries = si.all()
        link = CribLink.find(Path(cwd)) if cwd else None
        srcs = self._source_roots(proj).all()
        doc_count = self.store.count({"project": proj}, relpath_prefix=SRC_PREFIX)
        refs = [
            {**r, "root": str(r["root"]) if r["root"] else None}
            for r in self._project_refs(proj)
        ]
        return {
            "project": proj,
            "indexed": si.is_populated(),
            "symbols": len(entries),
            "files": len({e.get("file") for e in entries}),
            "kinds": dict(Counter(e.get("kind", "?") for e in entries)),
            "described": sum(1 for e in entries if e.get("description")),
            "with_keywords": sum(1 for e in entries if e.get("keywords")),
            "paths": (link.paths if link else []),
            "refs": refs,
            # live reindex progress ({done,total}) while a sweep runs — the poll signal
            # for a budgeted project_index that returned complete=False
            "indexing": self.code.sweeps.get(proj),
            # the non-blocking poll section: every long-running job for this
            # project (relocate rekey, code sweep, reconcile) — running/
            # complete/failed + the counts already banked. NEVER errors, never
            # blocks; this is what the model polls instead of waiting.
            "jobs": self._job_rows(proj),
            "doc_sources": srcs,
            "doc_chunks": doc_count,
            "crib": (str(link.root / ".crib") if link and link.root else None),
        }

    def status(self) -> dict[str, Any]:
        """One-call health summary (the `status` CLI verb / MCP tool): every
        project's inventory (notes, in-situ doc chunks, code symbols, learnings),
        any stale design decisions (`design_tainted`), git-sync state, the warm LSP
        sessions (which servers are attached, alive/busy/idle), and any in-flight
        indexing. Counts are cheap file counts (1 toml = 1 symbol), never full
        parses."""
        from .codeindex import _POOL

        projects = []
        for name in self.projects():
            pp = self.project_paths(name)
            nd = pp.notes_dir
            ld = pp.pillar_dir("learnings")
            sd = pp.project_dir / "symbol_index"
            doc_chunks = self.store.count({"project": name}, relpath_prefix=SRC_PREFIX)
            dd = pp.pillar_dir("design")
            pd = pp.pillar_dir("plans")
            row = {
                "project": name,
                "notes": sum(1 for _ in nd.rglob("*.md")) if nd.exists() else 0,
                "designs": sum(1 for _ in dd.glob("*.md")) if dd.exists() else 0,
                "plans": sum(1 for _ in pd.glob("*.md")) if pd.exists() else 0,
                "learnings": sum(1 for _ in ld.glob("*.md")) if ld.exists() else 0,
                "symbols": sum(1 for _ in sd.glob("*.toml")) if sd.exists() else 0,
                "doc_chunks": doc_chunks,
            }
            # Stale decisions, on the same footing as `reconciling`/
            # `index_rebuilding`: an ambient count nobody has to think to ask for.
            # A frontmatter-only graph scan, and reported only when non-zero, so a
            # project with nothing stale stays silent.
            stale = len(self.designs.tainted_designs(name))
            if stale:
                row["design_tainted"] = stale
            if pp.in_repo:
                # notes counted above are 0 when the store isn't here — say WHY,
                # rather than reading as an empty project
                row["store_root"] = pp.store_token
                if not pp.available:
                    row["unavailable"] = True
            projects.append(row)
        with self.code.indexing_lock:
            indexing = {p: list(v) for p, v in self.code.indexing.items() if v}
            sweeps = {p: dict(v) for p, v in self.code.sweeps.items()}
        remaining = self._reconcile_remaining
        out: dict[str, Any] = {
            "projects": projects,
            "git": self.git.state(),
            "lsp_sessions": _POOL.stats(),
            "indexing": indexing,
            # sweep progress: {proj: {done, total}} while a project index runs,
            # ABSENT when finished — poll this to wait on a background index
            "sweeps": sweeps,
            # the note reconcile started at daemon boot runs in the background;
            # its per-project countdown is here while it's in flight
            "reconciling": remaining is not None,
            "store": type(self.store).__name__,
            "embed_model": self.config.embed.model,
            # The DAEMON's own version — the code actually serving lookups. The
            # eval harness records this in every dump's protocol block: its own
            # `crib_import_version` is the harness interpreter's install, which
            # on the daemon path is NOT the executing code (it has differed:
            # harness 0.15.2 vs daemon 0.14.0 from a stale global venv).
            "crib_version": __version__,
            # pending until a full sweep has re-chunked under the current scheme;
            # a daemonless user clears it with `crib reindex` per project
            "chunk_schema": CHUNK_SCHEMA_VERSION,
            "chunk_schema_pending": self.stored_chunk_schema() != CHUNK_SCHEMA_VERSION,
        }
        if remaining is not None:
            out["reconcile_remaining"] = remaining
            if self._reconcile_reason:
                # why this sweep is running when it isn't the startup one — an
                # embedder change wiped the store and everything is re-embedding
                out["reconcile_reason"] = self._reconcile_reason
        if self._resweep_pending:
            # projects still un-embedded after a wipe: their lookups are THIN and
            # say so per hit (`index_rebuilding`)
            out["index_rebuilding"] = sorted(self._resweep_pending)
        stats = getattr(self.store, "stats", None)
        if callable(stats):
            # rows per table, versions, file/WAL size, and (shared) reconnects
            try:
                out["store_stats"] = stats()
            except Exception as e:  # noqa: BLE001 — status must still answer
                out["store_stats"] = {"error": str(e)}
        hits = getattr(self.embedder, "hits", None)
        if hits is not None:
            out["embed_cache"] = {"hits": hits,
                                  "misses": getattr(self.embedder, "misses", 0)}
        return out

    def project_forget(
        self,
        project: str | None = None,
        cwd: Path | None = None,
        with_learnings: bool = False,
    ) -> dict[str, Any]:
        """Clear the project's code index (the symbol_index). KEEPS attached learnings,
        notes and `.crib` by default — learnings are durable human source-of-truth;
        pass with_learnings=True to drop those too."""

        from .codeindex import SymbolIndex

        proj = self.resolve_project(project, cwd)
        si_root = SymbolIndex(self.project_paths(proj).project_dir).root
        removed = len(list(si_root.glob("*.toml"))) if si_root.exists() else 0
        if si_root.exists():
            shutil.rmtree(si_root)
        self.code.cache.pop(
            proj, None
        )  # drop resident cache (trust-mode won't see rmtree)
        self._bump_code_epoch(proj)
        # In-situ docs are index-only (source is master) — drop their chunks + the
        # source registry too; re-runnable via `project index`.
        reg = self._source_roots(proj)
        doc_ids = [
            i
            for i, m in self.store.get_meta({"project": proj}).items()
            if m.get("relpath", "").startswith(SRC_PREFIX)
        ]
        if doc_ids:
            self.store.delete(doc_ids)
            self.index.invalidate_caches(proj)
        for prefix in list(reg.all()):
            reg.remove(prefix)
        learnings = 0
        if with_learnings:
            ldir = self.learningstore.root(proj)
            if ldir.exists():
                learnings = len(list(ldir.glob("*.md")))
                shutil.rmtree(ldir)
        return {
            "project": proj,
            "symbols_removed": removed,
            "doc_chunks_removed": len(doc_ids),
            "learnings_removed": learnings,
        }

    def code_xref(
        self,
        symbol: str,
        project: str | None = None,
        path: str = "",
        scope: str = "",
        lang: str = "",
        cwd: Path | None = None,
    ) -> list[dict[str, Any]]:
        """A symbol's callers/callees/references from the persisted symbol_index."""
        return self.query.xref(
            self.resolve_project(project, cwd),
            symbol,
            path=path,
            scope=scope,
            lang=lang,
        )

    def code_dossier(
        self,
        symbol: str,
        project: str | None = None,
        cwd: Path | None = None,
        edge_cap: int = 20,
        path: str = "",
        scope: str = "",
        lang: str = "",
    ) -> dict[str, Any]:
        """Everything about one symbol (+ neighbour descriptions + any learning)."""
        return self.query.dossier(
            self.resolve_project(project, cwd), symbol, edge_cap, path, scope, lang
        )

    # ── Durable learnings: delegate to Learnings (crib/learnings.py) ───────────
    # Public code_* wrappers resolve_project then delegate; the internal helpers
    # (_attach_learnings / _learning_relpath / _learning_fqns / _rehome_candidates,
    # called by the code query methods) delegate directly.
    async def learning_add(
        self,
        symbol: str,
        text: str,
        path: str = "",
        scope: str = "",
        lang: str = "",
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Attach a durable learning to a code symbol (append a dated entry)."""
        return await self.learnings.append(
            self.resolve_project(project, cwd),
            symbol,
            text,
            path=path,
            scope=scope,
            lang=lang,
        )

    async def learning_edit(
        self,
        symbol: str,
        new_content: str,
        path: str = "",
        scope: str = "",
        lang: str = "",
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Rewrite a symbol's learning body wholesale."""
        return await self.learnings.edit(
            self.resolve_project(project, cwd),
            symbol,
            new_content,
            path=path,
            scope=scope,
            lang=lang,
        )

    async def learning_forget(
        self,
        symbol: str,
        path: str = "",
        scope: str = "",
        lang: str = "",
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Remove a symbol's learning (recoverable; works on orphans)."""
        return await self.learnings.forget(
            self.resolve_project(project, cwd),
            symbol,
            path=path,
            scope=scope,
            lang=lang,
        )

    async def learning_reaffirm(
        self,
        symbol: str,
        path: str = "",
        scope: str = "",
        lang: str = "",
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Clear a learning's stale flag without rewriting it."""
        return await self.learnings.reaffirm(
            self.resolve_project(project, cwd),
            symbol,
            path=path,
            scope=scope,
            lang=lang,
        )

    def learning_report(
        self,
        project: str | None = None,
        cwd: Path | None = None,
        orphans_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Health report for attached learnings (ok/moved/orphan)."""
        return self.learnings.report(self.resolve_project(project, cwd), orphans_only)

    async def learning_rehome(
        self,
        old_fqn: str,
        new_fqn: str | None = None,
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Re-point an orphaned learning (no target = ranked suggestions)."""
        return await self.learnings.rehome(
            self.resolve_project(project, cwd), old_fqn, new_fqn
        )

    def learning_read(
        self,
        symbol: str,
        path: str = "",
        scope: str = "",
        lang: str = "",
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Read a symbol's learning note (frontmatter + body)."""
        return self.learnings.read(
            self.resolve_project(project, cwd),
            symbol,
            path=path,
            scope=scope,
            lang=lang,
        )

    # ── Design decisions & plan items: delegate to Designs (crib/designs.py) ───
    # Same shape as the learnings block: resolve the project, then delegate to a
    # core that takes it explicitly. The FACET is the interface — reads, writes and
    # retrieval all have their own verbs here, so a caller never has to know the
    # decisions are notes in a directory (they are; that's backend).
    async def design_add(
        self,
        title: str,
        content: str,
        deps: list[str] | None = None,
        project: str | None = None,
        sources: list[Any] | None = None,
        proposed: bool = False,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Record a design decision (deps validated + `checked` seeded, `sources`
        captured at their current section hashes), with the same near-duplicate
        probe `note_store` runs.

        A near-duplicate DECISION is worse than a near-duplicate note: it forks
        the graph, so two half-authoritative decisions each collect their own
        dependents and neither taints the other's."""
        proj = self.resolve_project(project, cwd)
        out = await self.designs.design_add(
            proj, title, content, deps, sources, proposed
        )
        similar = await asyncio.to_thread(
            self._similar, proj, content, out["relpath"], "design"
        )
        return {**out, "similar": similar}

    def design_read(
        self, ref: str, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """A decision's dossier: body, status, annotated edges, taint + chains."""
        return self.designs.design_read(self.resolve_project(project, cwd), ref)

    async def design_edit(
        self,
        ref: str,
        new_content: str,
        project: str | None = None,
        sources: list[Any] | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Rewrite a decision through the facet; result names what it tainted.
        `sources`, when given, replaces its citations."""
        return await self.designs.design_edit(
            self.resolve_project(project, cwd), ref, new_content, sources
        )

    async def design_append(
        self,
        ref: str,
        content: str,
        project: str | None = None,
        cwd: Path | None = None,
        sources: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Extend a decision through the facet; result names what it tainted.
        `sources` ADDS citations (post-hoc doc wiring), never replaces."""
        return await self.designs.design_append(
            self.resolve_project(project, cwd), ref, content, sources
        )

    def design_list(
        self, tainted: bool = False, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """Every decision as a flat table (`tainted` filters to the stale ones)."""
        return self.designs.design_list(self.resolve_project(project, cwd), tainted)

    def design_export(
        self, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """The whole decision graph as ONE consolidated markdown doc
        (dep-topological, ties by rank/title/id — deterministic across runs)."""
        return self.designs.export_facet(self.resolve_project(project, cwd), "design")

    def plan_export(
        self, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """The whole plan as ONE consolidated markdown doc (same ordering
        contract as the design export)."""
        return self.designs.export_facet(self.resolve_project(project, cwd), "plan")

    def design_lookup(
        self,
        query: str,
        project: str | None = None,
        k: int = 8,
        cwd: Path | None = None,
        hints: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieval scoped to the DESIGN facet, each hit annotated with the facet
        state that decides whether to trust it (`status`, `tainted`, edge counts).

        Scoped by the pillar store, not a post-filter: the whole candidate pool
        is decisions, so a busy notes corpus can never crowd them out."""
        return self.designs.annotate_hits(
            self.resolve_project(project, cwd),
            [
                vars(h)
                for h in self.lookup(
                    query, project, k, cwd=cwd, store="design", hints=hints
                )
            ],
            kind="design",
        )

    def plan_lookup(
        self,
        query: str,
        project: str | None = None,
        k: int = 8,
        cwd: Path | None = None,
        hints: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieval scoped to the PLAN facet, hits annotated as `design_lookup`
        annotates its own."""
        return self.designs.annotate_hits(
            self.resolve_project(project, cwd),
            [
                vars(h)
                for h in self.lookup(
                    query, project, k, cwd=cwd, store="plans", hints=hints
                )
            ],
            kind="plan",
        )

    async def design_dep_add(
        self,
        ref: str,
        dep_ref: str,
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Declare that a decision builds on another (cycle-checked)."""
        return await self.designs.design_dep_add(
            self.resolve_project(project, cwd), ref, dep_ref
        )

    async def design_dep_remove(
        self,
        ref: str,
        dep_ref: str,
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Drop a dependency edge between two decisions."""
        return await self.designs.design_dep_remove(
            self.resolve_project(project, cwd), ref, dep_ref
        )

    async def design_forget(
        self,
        ref: str,
        force: bool = False,
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Delete a decision; blocks on dependents unless forced."""
        return await self.designs.design_forget(
            self.resolve_project(project, cwd), ref, force
        )

    def design_check(
        self,
        ref: str | None = None,
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Which decisions are stale with respect to what they build on."""
        return self.designs.design_check(self.resolve_project(project, cwd), ref)

    async def design_reaffirm(
        self, ref: str, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """Re-record a decision's dep hashes after re-reading it (the cheap,
        normal outcome of a taint — not error recovery)."""
        return await self.designs.design_reaffirm(
            self.resolve_project(project, cwd), ref
        )

    async def plan_reaffirm(
        self, ref: str, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """Re-record a plan item's dep hashes after re-reading what moved — the
        plan-side twin of design_reaffirm (clears a benign taint without the
        dep_remove/dep_add dance, and without pretending the work moved)."""
        return await self.designs.plan_reaffirm(self.resolve_project(project, cwd), ref)

    async def design_promote(
        self, ref: str, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """Promote an extracted decision: proposed → active, hashes seeded fresh."""
        return await self.designs.design_promote(
            self.resolve_project(project, cwd), ref
        )

    def design_import(
        self, relpath: str, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """A doc's sections + hashes + existing citations + the extraction
        procedure — the read that precedes an LLM-driven design import."""
        return self.designs.design_import(self.resolve_project(project, cwd), relpath)

    def plan_import(
        self, relpath: str, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """The same for the plan facet (actionable work rather than decisions)."""
        return self.designs.plan_import(self.resolve_project(project, cwd), relpath)

    def design_tree(
        self,
        ref: str | None = None,
        direction: str = "deps",
        depth: int = 6,
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """The dependency tree around a decision, taint-flagged."""
        return self.designs.design_tree(
            self.resolve_project(project, cwd), ref, direction, depth
        )

    def design_graph(
        self, project: str | None = None, cwd: Path | None = None, sources: bool = False
    ) -> dict[str, Any]:
        """The decision map as {nodes, edges} — the symbol graph's consumer
        contract, for decisions. `tainted` is this graph's ※."""
        return self.designs.facet_graph(
            self.resolve_project(project, cwd), "design", sources
        )

    def plan_graph(
        self, project: str | None = None, cwd: Path | None = None, sources: bool = False
    ) -> dict[str, Any]:
        """The plan as {nodes, edges}, INCLUDING the design nodes items rest on —
        those edges gate, so a plan graph without them would misdraw the plan as
        self-contained."""
        return self.designs.facet_graph(
            self.resolve_project(project, cwd), "plan", sources
        )

    async def design_supersede(
        self,
        ref: str,
        by_ref: str | None = None,
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Soft-delete a decision and taint its dependents."""
        return await self.designs.design_supersede(
            self.resolve_project(project, cwd), ref, by_ref
        )

    async def plan_add(
        self,
        title: str | None = None,
        content: str = "",
        deps: list[str] | None = None,
        after: str | None = None,
        before: str | None = None,
        items: list[dict[str, Any]] | None = None,
        project: str | None = None,
        sources: list[Any] | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Add one plan item, or a whole batch (`items`), ranked at the end unless
        after/before place them."""
        return await self.designs.plan_add(
            self.resolve_project(project, cwd),
            title,
            content,
            deps,
            after,
            before,
            items,
            sources,
        )

    async def plan_status(
        self, ref: str, status: str, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """Set a plan item's status (todo/in-progress/parked/done/verified)."""
        return await self.designs.plan_status(
            self.resolve_project(project, cwd), ref, status
        )

    async def plan_park(
        self,
        ref: str,
        project: str | None = None,
        triggers: list[str] | None = None,
        why: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Park an item — not now, with a stated reason; optionally watching refs
        whose change SURFACES it (never auto-wakes it)."""
        return await self.designs.plan_park(
            self.resolve_project(project, cwd), ref, triggers, why
        )

    async def plan_decline(
        self,
        ref: str,
        why: str | None = None,
        project: str | None = None,
        triggers: list[str] | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Decline an item — never scheduled, remembered with the why-not (a
        decline without the why-not is refused: the rationale IS the payload)."""
        return await self.designs.plan_decline(
            self.resolve_project(project, cwd), ref, why, triggers
        )

    async def plan_review(
        self, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """The review EVIDENCE PACK: the consolidated export + per-item evidence
        (age, deps, overlap probes vs open AND done/declined items) + the review
        contract. Runs no model, writes nothing — the session LLM judges, then
        `plan_review_apply` applies the batch."""
        return await self.designs.plan_review(
            self.resolve_project(project, cwd),
            probe=lambda p, text, exclude: self._similar(p, text, exclude, "plans"),
        )

    async def plan_review_apply(
        self,
        project: str | None = None,
        batch: list[dict] | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Apply a judged review batch, edge-aware, stamped `reviewed`."""
        return await self.designs.plan_review_apply(
            self.resolve_project(project, cwd), batch or []
        )

    def plan_read(
        self, ref: str, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """A plan item's dossier: body, status, annotated edges, derived blocking
        + taint — the plan-side design_read."""
        return self.designs.plan_read(self.resolve_project(project, cwd), ref)

    async def plan_edit(
        self,
        ref: str,
        new_content: str,
        project: str | None = None,
        sources: list[Any] | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Rewrite a plan item's body through the facet; result names what it
        tainted. `sources`, when given, replaces its citations."""
        return await self.designs.plan_edit(
            self.resolve_project(project, cwd), ref, new_content, sources
        )

    async def plan_append(
        self,
        ref: str,
        content: str,
        project: str | None = None,
        cwd: Path | None = None,
        sources: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Extend a plan item's body through the facet; result names what it
        tainted. `sources` ADDS citations, never replaces."""
        return await self.designs.plan_append(
            self.resolve_project(project, cwd), ref, content, sources
        )

    async def plan_dep_add(
        self,
        ref: str,
        dep_ref: str,
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Declare that a plan item must follow another (cycle-checked)."""
        return await self.designs.plan_dep_add(
            self.resolve_project(project, cwd), ref, dep_ref
        )

    async def plan_dep_remove(
        self,
        ref: str,
        dep_ref: str,
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Drop a must-precede edge between plan items."""
        return await self.designs.plan_dep_remove(
            self.resolve_project(project, cwd), ref, dep_ref
        )

    async def plan_forget(
        self,
        ref: str,
        force: bool = False,
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Delete a plan item; blocks on dependents unless forced."""
        return await self.designs.plan_forget(
            self.resolve_project(project, cwd), ref, force
        )

    async def plan_move(
        self,
        ref: str,
        after: str | None = None,
        before: str | None = None,
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Re-rank a plan item (deps untouched)."""
        return await self.designs.plan_move(
            self.resolve_project(project, cwd), ref, after, before
        )

    def plan_list(
        self, all: bool = False, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """The plan in topological, rank-tie-broken order."""
        return self.designs.plan_list(self.resolve_project(project, cwd), all)

    def plan_next(
        self, k: int = 5, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """The actionable plan items right now (todo, deps satisfied)."""
        return self.designs.plan_next(self.resolve_project(project, cwd), k)

    def code_lookup(
        self,
        query: str,
        project: str | None = None,
        k: int = 8,
        cwd: Path | None = None,
    ) -> list[dict[str, Any]]:
        """Find a code symbol by concept OR name. Coverage-gated expanded-BM25 ⊕ raw-dense
        blend (in `_lookup_one`), then an optional cross-encoder rerank folded in as a
        RANGE-MATCHED term (`[retrieve].rerank`). Fans out to refs."""
        from .codequery import _RERANK_N, check_k, check_query

        # validated HERE too: the widened pool below (`max(k, _RERANK_N)`) would
        # otherwise launder a k of 0/-1 into a legal 20 before the core saw it
        check_query(query)
        check_k(k)
        proj = self.resolve_project(project, cwd)
        hits = self.query.lookup(proj, query, max(k, _RERANK_N))
        if self.config.retrieve.rerank and len(hits) > 1:
            hits = self._rerank_code(query, hits)
        hits = hits[:k]
        for r, h in enumerate(hits, 1):
            h["rank"] = r
            h.pop("_score", None)
        return hits

    def _rerank_code(
        self, query: str, hits: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Range-matched rerank: min-max the blend score AND the cross-encoder score over
        the head, sum, re-order. Keeps the blend's magnitude (unlike RRF, which discards it
        and let a noisier reranker reorder freely — measured to regress cross-domain)."""
        from .codequery import _RERANK_N

        head = hits[:_RERANK_N]
        try:
            # the NAME, not the reference: a cross-encoder scores words against a
            # query, and `crib.app.Crib.foo` carries them where a path ref does not
            rr = self.reranker.scores(
                query, [f"{_symbol_fqn(h)}: {h.get('description', '')}" for h in head]
            )
        except Exception as e:  # noqa: BLE001 — reranker optional; degrade to blend order
            print(f"[crib] code reranker disabled: {e}", file=sys.stderr)
            return hits
        bn = _min_max([float(h.get("_score", 0.0)) for h in head])
        rn = _min_max(list(rr))
        order = sorted(range(len(head)), key=lambda j: bn[j] + rn[j], reverse=True)
        return [head[j] for j in order] + list(hits[_RERANK_N:])

    def code_graph(
        self,
        symbol: str | None = None,
        direction: str = "callees",
        depth: int = 6,
        project: str | None = None,
        cwd: Path | None = None,
        shape: str | None = None,
        group_by: str | None = None,
        group_depth: int = 0,
        path: str = "",
        scope: str = "",
        lang: str = "",
        under: str = "",
        exclude: str = "",
    ) -> dict[str, Any]:
        """Call graph around a symbol — pstree tree, or (`shape="edges"`) the
        deduplicated subgraph; omit `symbol` for the whole project (`under=`/`exclude=`
        then scope the export by path prefix; boundary-crossing edges keep lean
        `truncated` frontier nodes). Crosses into refs."""
        return self.query.graph(
            self.resolve_project(project, cwd),
            symbol,
            direction,
            depth,
            shape,
            group_by,
            group_depth,
            path,
            scope,
            lang,
            under,
            exclude,
        )

    async def enrich(
        self,
        relpath: str | None = None,
        project: str | None = None,
        cwd: Path | None = None,
        overwrite: bool = False,
        background: bool = False,
    ) -> dict[str, Any]:
        """ONE enrichment pass over every configured facet — the keyword labels (BM25
        side) AND the summary labels (dense alias side) together, one bulk generation
        per note batch emitting all facets' terms. The notes mirror of the code path's
        single describe emitting {description, keywords}: half the LLM calls of running
        `elaborate` + `summarize` back to back. This is what the write-path refresh and
        the startup backlog run; the per-label verbs stay for explicit/eval use."""
        from .section_index import (
            KEYWORD_PROMPTS,
            SUMMARY_PROMPTS,
            SectionIndex,
            resolve_prompt,
        )

        proj = self.resolve_project(project, cwd)
        facets: list[dict[str, Any]] = []
        for table, builtins, root, labels in (
            (
                self.config.elaborate,
                KEYWORD_PROMPTS,
                "keyword_index",
                self.config.retrieve.keyword_labels,
            ),
            (
                self.config.summarize,
                SUMMARY_PROMPTS,
                "summary_index",
                self.config.retrieve.summary_labels,
            ),
        ):
            for lb in labels:
                prompt = resolve_prompt(lb, table, builtins)
                if prompt is None:
                    raise CribUserError(f"unknown enrichment label {lb!r}")
                facets.append(
                    {
                        "root": root,
                        "label": lb,
                        "prompt": prompt,
                        "store": SectionIndex(self.paths.project_dir(proj), root),
                    }
                )
        if not facets:
            return {"project": proj, "labels": [], "written": {}, "total": 0}
        return await self._generate_enrichment(
            proj, "elaborate", facets, relpath, overwrite, background=background
        )

    async def _generate_index(
        self,
        root_name: str,
        purpose: str,
        label: str,
        prompt: str | None,
        relpath: str | None,
        project: str | None,
        cwd: Path | None,
        overwrite: bool,
    ) -> dict[str, Any]:
        """Single-facet shim over `_generate_enrichment` (the `elaborate`/`summarize`
        verbs) — legacy flat result shape preserved."""
        proj = self.resolve_project(project, cwd)
        from .section_index import SectionIndex

        if prompt is None:
            raise CribUserError(
                f"unknown {purpose} label {label!r}: no builtin and no "
                f"[{purpose}.{label}].prompt in config"
            )
        facet = {
            "root": root_name,
            "label": label,
            "prompt": prompt,
            "store": SectionIndex(self.paths.project_dir(proj), root_name),
        }
        out = await self._generate_enrichment(
            proj, purpose, [facet], relpath, overwrite
        )
        return {
            "project": proj,
            "label": label,
            "written": out["written"].get(label, 0),
            "skipped": out["skipped"],
            "errors": out["errors"],
            "total": out["total"],
            "pruned": out["pruned"].get(label, 0),
            "bulk_docs": out["bulk_docs"],
        }

    async def _generate_enrichment(
        self,
        proj: str,
        purpose: str,
        facets: list[dict[str, Any]],
        relpath: str | None,
        overwrite: bool,
        background: bool = False,
    ) -> dict[str, Any]:
        """Shared MULTI-FACET section-level generation: one bulk call per note batch
        emits terms for EVERY facet (a facet = one label of keyword_index or
        summary_index, with its own prompt and store), with the full section as
        context (windows share one result), persisted section-addressed so it
        survives re-windowing. Per-facet hash gates stay independent — a section is
        a target if ANY facet lacks its entry, and only the missing facets are
        written, so a half-generated state heals without churning complete facets.
        The per-section mop-up sweeps the bulk-missed tail per facet (plain-text
        `parse_terms`, the proven path). Bounded-concurrent, per-call timeout,
        error-isolated; off the write path."""
        from .generate import (
            GenerationError,
            agenerate,
            agenerate_structured,
            resolve_provider,
        )
        from .section_index import parse_terms

        # provenance only (the resolved model name for the record); the real
        # generation below surfaces its own errors, so a resolution failure here
        # just falls back to the configured name.
        try:
            _p = resolve_provider(self.config.generate, purpose)
            model = _p.model or _p.adapter or ""
        # FP: the rule matches the `or` in the except BODY, not the exception
        # spec (which is a plain tuple of types, not a boolean expression).
        # pi-lens-ignore: no-boolean-in-except
        except (GenerationError, KeyError, ValueError, AttributeError):
            model = self.config.generate.model or self.config.generate.adapter

        # Group chunks into their sections (section_hash), reconstructing the full
        # section text once per note from the file (windows would double-count
        # overlap). One generation per section.
        section_line_maps: dict[str, dict[str, tuple[int, int]]] = {}

        def _section_text(rp: str, heading: str, fallback: str) -> str:
            if rp not in section_line_maps:
                try:
                    section_line_maps[rp] = section_line_map(
                        self.abspath(proj, rp).read_text()
                    )
                except OSError:
                    section_line_maps[rp] = {}
            span = section_line_maps[rp].get(heading)
            if span:
                with contextlib.suppress(OSError):
                    lines = self.abspath(proj, rp).read_text().splitlines()
                    return "\n".join(lines[span[0] - 1 : span[1]])
            return fallback

        pass_start = time.time()  # prune guard: spare entries written mid-pass
        seen_sections: set[str] = set()
        # (section_hash, relpath, heading, body, missing-facets)
        targets: list[tuple[str, str, str, str, list[dict]]] = []
        skipped = 0
        for _cid, (doc, meta) in self.store.get_docs({"project": proj}).items():
            if relpath and meta.get("relpath") != relpath:
                continue
            sh = meta.get("section_hash") or meta.get("content_hash")
            if not sh or sh in seen_sections:
                continue
            seen_sections.add(sh)
            missing = [
                f for f in facets if overwrite or not f["store"].has(f["label"], sh)
            ]
            if not missing:
                skipped += 1
                continue
            rp = meta.get("relpath", "")
            heading = meta.get("heading_path", "")
            key = section_key(heading, int(meta.get("occurrence", 1) or 1))
            targets.append((sh, rp, heading, _section_text(rp, key, doc), missing))

        gen = self.config.generate
        # The generation ledger (crib.genledger): BACKGROUND passes (the startup
        # backlog) skip sections whose generation already failed for the same
        # content and provider until their backoff expires; explicit requests
        # always run. Outcomes are recorded either way (below).
        from .genledger import provider_id

        provider = provider_id(gen, purpose)
        backing_off = 0
        if background and targets:
            ready, waiting = self.ledger.due(
                "enrich", {f"{proj}:{t[0]}": t[0] for t in targets}, provider
            )
            keep = set(ready)
            backing_off = len(waiting)
            targets = [t for t in targets if f"{proj}:{t[0]}" in keep]
        total = len(targets)
        sem = asyncio.Semaphore(max(1, gen.concurrency))
        written: dict[str, int] = {f["label"]: 0 for f in facets}
        counters = {"errors": 0, "done": 0, "bulk_docs": 0}
        done: dict[str, set[str]] = {f["label"]: set() for f in facets}  # label → shs
        tag = f"{purpose} {'+'.join(f['label'] for f in facets)}"
        mode = "bulk+mop-up" if gen.bulk else "per-section"
        print(
            f"[{tag}] {total} sections to generate ({mode}; skipped {skipped} "
            f"cached; concurrency {gen.concurrency}, timeout {gen.timeout:g}s)",
            file=sys.stderr,
            flush=True,
        )

        def _record(
            facet: dict, sh: str, rp: str, heading: str, terms: list[str]
        ) -> None:
            lb = facet["label"]
            if terms and sh not in done[lb]:
                facet["store"].write(
                    lb, sh, terms, relpath=rp, heading=heading, model=model
                )
                written[lb] += 1
                done[lb].add(sh)

        async def _one_facet(
            facet: dict, sh: str, rp: str, heading: str, body: str
        ) -> None:
            # Per-section, per-facet: bounded-concurrent, timeout-capped, error-
            # isolated — one hung/failed section is skipped, never sinks the pass.
            # Serves both the legacy path (bulk off) and the bulk-missed mop-up;
            # stays plain-text + parse_terms (the proven path) since it only sweeps
            # the small tail.
            user = f"{heading}\n\n{body}" if heading else body
            try:
                async with sem:
                    text = await agenerate(
                        gen, facet["prompt"], user, purpose=purpose, timeout=gen.timeout
                    )
                terms = parse_terms(text)
            except Exception as e:  # noqa: BLE001 — timeout or generation failure
                counters["errors"] += 1
                counters["done"] += 1
                print(
                    f"[{tag}] {counters['done']} ERR  {rp} :: "
                    f"{heading[-44:]} ({facet['label']}) — {type(e).__name__}: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                return
            _record(facet, sh, rp, heading, terms)
            counters["done"] += 1
            print(
                f"[{tag}] {counters['done']} ok   {rp} :: "
                f"{heading[-44:]} ({facet['label']}: {len(terms)} terms)",
                file=sys.stderr,
                flush=True,
            )

        # Phase 1 — whole-doc bulk authoring (structured, one call per note/batch):
        # the model sees a note's sections together, so it can pick section-
        # *distinctive* terms (blind per-section authoring made them generic — see
        # docs/retrieval-and-adoption.md §5.5). Content-addressing makes it safe: a
        # skipped/malformed section simply isn't written and is swept by Phase 2, so
        # strict model conformance is an efficiency, not a correctness, property.
        if gen.bulk and targets:
            from collections import defaultdict

            # ONE structured call per batch emits EVERY facet: an object field per
            # label, each with its own instruction — the {description, keywords}
            # single-pass trick from the code path, generalized.
            bulk_schema = {
                "type": "object",
                "properties": {
                    "sections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "heading": {"type": "string"},
                                **{
                                    f["label"]: {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    }
                                    for f in facets
                                },
                            },
                            "required": ["heading"],
                        },
                    }
                },
                "required": ["sections"],
            }
            instr = "\n\n".join(
                f"For the `{f['label']}` field: {f['prompt']}" for f in facets
            )
            bulk_system = (
                f"{instr}\n\nYou are given a DOCUMENT of several sections, each "
                "introduced by a line `<<< SECTION: <heading> >>>`. Apply EACH "
                "field's instruction above to EACH section independently and return "
                "one result per section, using that section's exact <heading> "
                "string. Cover every section and every field."
            )

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
            batches = [
                items[i : i + step]
                for items in by_doc.values()
                for i in range(0, len(items), step)
            ]

            async def _bulk(batch: list[tuple]) -> None:
                rp = batch[0][1]
                by_head = {t[2]: t for t in batch}
                user = "\n\n".join(f"<<< SECTION: {t[2]} >>>\n{t[3]}" for t in batch)
                try:
                    async with sem:
                        data = await agenerate_structured(
                            gen,
                            bulk_system,
                            user,
                            bulk_schema,
                            purpose=purpose,
                            schema_name="emit_terms",
                            schema_description="Per-section search terms for the document.",
                            timeout=gen.timeout,
                        )
                except Exception as e:  # noqa: BLE001 — bulk fails → mop-up covers it
                    print(
                        f"[{tag}] bulk ERR {rp} ({len(batch)} sec) — "
                        f"{type(e).__name__}: {e} → mop-up",
                        file=sys.stderr,
                        flush=True,
                    )
                    return
                items = data.get("sections") if isinstance(data, dict) else None
                for it in items if isinstance(items, list) else []:
                    if not isinstance(it, dict):
                        continue
                    tgt = _match(str(it.get("heading") or ""), by_head)
                    if not tgt:
                        continue
                    for facet in tgt[4]:  # write ONLY the missing facets
                        raw = it.get(facet["label"])
                        terms = [
                            str(x).strip()
                            for x in (raw if isinstance(raw, list) else [])
                            if str(x).strip()
                        ]
                        if terms:
                            _record(facet, tgt[0], tgt[1], tgt[2], terms)
                counters["bulk_docs"] += 1
                got = sum(
                    1 for t in batch if all(t[0] in done[f["label"]] for f in t[4])
                )
                print(
                    f"[{tag}] bulk {rp}: {got}/{len(batch)} sections complete",
                    file=sys.stderr,
                    flush=True,
                )

            await asyncio.gather(*(_bulk(b) for b in batches))

        # Phase 2 — per-section, per-facet mop-up for anything Phase 1 didn't cover
        # (or everything when bulk is off). Idempotent: `done` gates re-writes.
        mop = [(f, t) for t in targets for f in t[4] if t[0] not in done[f["label"]]]
        if mop:
            if gen.bulk and counters["bulk_docs"]:
                print(
                    f"[{tag}] mop-up: {len(mop)} facet-sections uncovered",
                    file=sys.stderr,
                    flush=True,
                )
            await asyncio.gather(
                *(_one_facet(f, t[0], t[1], t[2], t[3]) for f, t in mop)
            )

        if targets:
            ok = [
                f"{proj}:{t[0]}"
                for t in targets
                if all(t[0] in done[f["label"]] for f in t[4])
            ]
            okset = set(ok)
            bad = {
                f"{proj}:{t[0]}": t[0] for t in targets if f"{proj}:{t[0]}" not in okset
            }
            try:
                self.ledger.succeeded("enrich", ok)
                self.ledger.failed(
                    "enrich",
                    bad,
                    provider,
                    f"facets left ungenerated ({counters['errors']} error(s) in the pass)",
                )
            except Exception as e:  # noqa: BLE001 — bookkeeping must never fail a pass
                print(f"[crib] generation ledger: {e}", file=sys.stderr)
            self.index.invalidate_caches(
                proj
            )  # feeds BM25 (keywords) + aliases (summaries)
        # GC orphaned entries (edits re-key their section, note deletes drop it) — only
        # a PROJECT-WIDE pass knows the full live set; a relpath-scoped one must not
        # prune (it saw one note's sections). Runs on every startup via the backlog.
        pruned = (
            {
                f["label"]: f["store"].prune(
                    f["label"], seen_sections, before=pass_start
                )
                for f in facets
            }
            if relpath is None
            else {f["label"]: 0 for f in facets}
        )
        return {
            "project": proj,
            "labels": [f["label"] for f in facets],
            "written": written,
            "skipped": skipped,
            "errors": counters["errors"],
            "total": total,
            "pruned": pruned,
            "bulk_docs": counters["bulk_docs"],
            "backing_off": backing_off,
        }

    # --- in-situ docs (default: source is master, never copied) ------------
    async def index_docs_insitu(
        self, project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
        """Index a repo's `.crib`-declared docs IN-SITU — the source tree is the
        master; crib holds only the index, never a copy. Each doc is a
        source-anchored note keyed `sources/<repo>/<rel>`; `read`/`locate` return
        the repo file, so an LLM that edits it edits the master. Re-runnable
        (hash-gated), and it prunes docs deleted from the source."""
        # No process-cwd fallback: in the daemon that's the spawn dir, not a repo.
        base = Path(cwd) if cwd else self._registered_root(project)
        if base is None:
            raise CribUserError(
                "no repo dir to scan: pass project_path=<the repo dir> (or name an "
                "already-indexed project)"
            )
        link = CribLink.find(base)
        if link is None or link.root is None:
            raise CribUserError(f"no .crib found from {base} upward")
        proj = project or link.project
        repo = link.root.name
        prefix = f"{SRC_PREFIX}{repo}/"
        self._source_roots(proj).upsert(prefix, link.root)
        self._register_code_root(
            proj, link.root
        )  # watch source-tree edits (docs + code)

        nd = self.notes_dir(proj)
        seen: set[str] = set()
        indexed: list[str] = []
        skipped: list[dict[str, str]] = []
        for pattern in link.doc_patterns:
            for src in sorted(link.root.glob(pattern)):
                # The in-repo store is NOT part of the repo's docs: its files are
                # this project's notes and are already indexed as such, so a glob
                # reaching in (`docs: ["**/*.md"]` reaches everywhere) would index
                # each one a second time under a `sources/…` key (CribLink.in_store).
                if not src.is_file() or link.in_store(src):
                    continue
                rel = src.relative_to(link.root)
                relpath = src_relpath(repo, rel.as_posix())
                seen.add(relpath)
                # A repo's docs are NOT crib-shaped notes — one with a `---` header
                # that isn't YAML (or a non-UTF8 byte) must skip, not kill the sweep.
                try:
                    res = await self.index.index_file(
                        proj, nd, relpath, content_path=src
                    )
                except (NoteParseError, UnicodeDecodeError, OSError) as e:
                    skipped.append({"relpath": relpath, "error": str(e)})
                    continue
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
        return {
            "project": proj,
            "root": str(link.root),
            "prefix": prefix,
            "docs": len(seen),
            "changed": len(indexed),
            "removed": removed,
            "skipped": skipped,
        }

    def _indexed_relpaths(self, project: str, prefix: str) -> set[str]:
        """Relpaths currently indexed under `prefix` (one meta scan)."""
        out: set[str] = set()
        for m in self.store.get_meta({"project": project}).values():
            rp = m.get("relpath", "")
            if rp.startswith(prefix):
                out.add(rp)
        return out

    # --- import ------------------------------------------------------------
    async def import_files(
        self, paths: list[str], project: str | None = None, cwd: Path | None = None
    ) -> dict[str, Any]:
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
                    raise CribUserError(
                        f"relative path {p!r} has no anchor: pass absolute paths, "
                        "or project_path=<repo dir> to resolve them against"
                    )
                src = cwd / src
            src = src.resolve()
            if not src.is_file():
                raise CribUserError(f"not a file: {p}")
            relpath = f"imported/{src.name}"
            sfm, sbody = notes.parse(src.read_text())
            tgt = self.abspath(proj, relpath)
            prev = notes.load(tgt) if tgt.exists() else None
            fm = dict(sfm)
            fm.update(
                {
                    "source": "imported",
                    "source_path": portable_path(src, self.config.locations),
                    "imported": (prev.frontmatter.get("imported") if prev else None)
                    or today,
                }
            )
            note_id = (prev.id if prev and prev.id else None) or derived_ulid(relpath)
            fm = {"id": note_id, **fm}
            note = Note(path=tgt, frontmatter=fm, body=sbody)
            await self._write_note(proj, relpath, note)
            imported.append(relpath)
        return {
            "project": proj,
            "imported": len(imported),
            "files": imported,
            "created": created,
        }

    # --- claude harness memory mirror (DESIGN §13) -------------------------
    async def import_claude_memory(
        self,
        project: str | None = None,
        cwd: Path | None = None,
        root: Path | None = None,
    ) -> dict[str, Any]:
        """Mirror Claude Code's harness memory into
        `<project>/notes/claude-memory/<host>/`.

        One-way: the harness owns those files; we copy+index, never write back.
        Host-namespaced so a git-synced data dir merges (not collides) two
        machines' memories. The crib note id is preserved across syncs, so
        history/identity survive; files removed upstream are dropped (reconcile,
        scoped to THIS host). Records a binding for the daemon's live mirror.
        """
        # No process-cwd fallback: in the daemon that's the spawn dir, not a repo.
        start = root or cwd
        if start is None:
            raise CribUserError(
                "no repo dir to anchor the memory lookup: pass project_path=<the "
                "repo dir> (or root=<the harness project root>)"
            )
        src_root = root or claudemem.find_harness_root(start)
        if src_root is None or not claudemem.harness_memory_dir(src_root).is_dir():
            raise CribUserError(
                f"no Claude memory dir found from {start} upward "
                f"(looked under {claudemem.claude_config_dir() / 'projects'})"
            )
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
            mtype = (
                (sfm.get("metadata") or {})
                if isinstance(sfm.get("metadata"), dict)
                else {}
            ).get("type")
            tags = list(
                dict.fromkeys(
                    [
                        *(sfm.get("tags") or []),
                        "claude-memory",
                        *([mtype] if mtype else []),
                    ]
                )
            )
            fm = {
                **sfm,
                "source": "claude_memory",
                "host": claudemem.hostslug(),
                "source_path": str(src),
                "memory_name": sfm.get("name"),
                "synced": today,
                "tags": tags,
            }
            tgt = self.abspath(proj, relpath)
            prev_id = notes.load(tgt).id if tgt.exists() else None
            # Derived id (from the host-namespaced path) is stable across syncs,
            # so identity/history survive without a per-machine random ULID (§14).
            fm = {"id": prev_id or derived_ulid(relpath), **fm}
            note = Note(path=tgt, frontmatter=fm, body=sbody)
            note.path = tgt
            notes.save_atomic(note)  # derived: bypass the version ring
            await self.index.index_file(proj, self.notes_dir(proj), relpath)
            synced.append(relpath)

        removed = await self._reconcile_memory_dir(proj, prefix, seen)
        self.memory_bindings.upsert(src_root, proj)
        return {
            "project": proj,
            "source": str(mem_dir),
            "synced": len(synced),
            "removed": removed,
            "files": synced,
            "created": created,
        }

    async def _reconcile_memory_dir(
        self, proj: str, prefix: str, keep: set[str]
    ) -> int:
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
    def list_versions(
        self, relpath: str, project: str | None = None, cwd: Path | None = None
    ) -> list[dict[str, Any]]:
        proj = self.resolve_project(project, cwd)
        return self.notestore.list_versions(proj, relpath)

    async def restore(
        self,
        relpath: str,
        version: str,
        project: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        proj = self.resolve_project(project, cwd)
        content = self.notestore.version_content(proj, relpath, version)
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

    def use_project(self, project: str) -> dict[str, Any]:
        """Set the session's current project — the one implementation behind both the
        `project_use` MCP tool and the in-process CLI verb (session state is
        process-local here)."""
        from .paths import check_project_name
        from .session import STATELESS_HINT, adopt_into_session

        # Validated BEFORE the eager mkdir — otherwise `../x` would create a
        # namespace outside the projects tree on the way in.
        check_project_name(project)
        created = self.project_is_new(project)
        self.notes_dir(project)  # eager mkdir — no phantom namespace
        # The ONE anchor gate: on the stateless era the pointer would die after
        # this call, so don't write it — say so instead (the CLI, which anchors
        # process-locally, never sees the `anchored: false` shape).
        if not adopt_into_session(project):
            return {
                "current_project": project,
                "created": created,
                "anchored": False,
                "note": STATELESS_HINT,
            }
        return {"current_project": project, "created": created}

    def current_project(self, cwd: Path | None = None) -> dict[str, Any]:
        """Show the session's current project + how it resolved (mirrors the
        project_current MCP tool). A cwd naming a real project is reported as the
        answer, and adopted when the session has none yet."""
        from .session import resolve_session_project, session_state

        res = resolve_session_project(
            session_state(),
            None,
            cwd,
            lambda c: self.resolve_project(None, c),
            default=self.config.default_project,
        )
        return {
            "current_project": res.project,
            "resolved_via": res.via,
            "projects": self.projects(),
        }

    def project_config(self, project: str) -> ProjectConfig:
        return ProjectConfig.load(self.project_paths(project).config_file, project)

    def project_list(self) -> list[dict[str, Any]]:
        """Every project as a row — the `project_list` surface.

        A project whose notes live in a repo carries its `store_root` token, and
        `unavailable: true` when that repo isn't on this machine. Listing it
        anyway is the point: an invisible project reads as "gone" when the truth
        is "clone the repo" (or `crib project release` it)."""
        out: list[dict[str, Any]] = []
        for name in self.projects():
            pp = self.project_paths(name)
            row: dict[str, Any] = {"project": name}
            if pp.in_repo:
                row["store_root"] = pp.store_token
                if not pp.available:
                    row["unavailable"] = True
            out.append(row)
        return out


def _build_store(
    paths: Paths, config: Config
) -> tuple[Store, Callable[[], None] | None]:
    """Return (store, on_close). on_close releases what the store holds: the DuckDB
    file (so another process can open it), or the shared server's refcount.

    Every store is a rebuildable cache: a fresh or empty one is filled by the
    startup reconcile, which re-embeds what is on disk (no LLM)."""
    backend = config.store.backend
    if backend == "json":
        # Removed in 0.22: DuckDB ships in the base install, and the JSON file
        # was rewritten whole on every edit. The store is a cache, so nothing
        # is lost by switching; the startup reconcile fills the new one.
        print(
            '[crib] [store] backend = "json" is no longer supported; using duckdb. '
            "Remove the setting from config.toml.",
            file=sys.stderr,
        )
    elif backend != "duckdb":
        raise CribUserError(
            f"[store] backend = {backend!r} is not a store crib knows (duckdb)"
        )
    if config.duckdb.mode == "shared":
        return _build_shared_duckdb(paths, config)
    if config.duckdb.mode != "embedded":
        raise CribUserError(
            f"[duckdb] mode = {config.duckdb.mode!r} is not a mode crib knows "
            "(embedded | shared)"
        )
    from .store_duckdb import DuckDBStore

    store = DuckDBStore.embedded(paths.duckdb_file)
    return store, store.close


def _build_shared_duckdb(
    paths: Paths, config: Config
) -> tuple[Store, Callable[[], None]]:
    """Refcount the crib DuckDB server via sharedserver, wait for it, connect.

    Reuse-or-start through sharedserver. The bind is checked BEFORE the
    launch so a non-loopback config never opens a listener. The first start
    fetches the Quack extension into crib's own extension dir, so it may take a
    while; later starts load it offline."""
    from . import sharedserver
    from .store_quack import (
        QuackStore,
        check_shared_bind,
        ensure_token,
        quack_uri,
        server_command,
    )

    c = config.duckdb
    check_shared_bind(c.host)
    token = ensure_token(paths.duckdb_token_file)
    ext = paths.duckdb_extension_dir
    command = server_command(
        paths.duckdb_file, c.host, c.port, paths.duckdb_token_file, ext
    )
    uri = quack_uri(c.host, c.port)

    def probe() -> None:
        QuackStore.connect(uri, token, ext).close()

    if not c.launch:
        # Externally supervised: never start (or restart) it; just attach. A
        # restart by the supervisor is survived by the client's reconnect,
        # which waits for the server like the first attach does.
        def wait() -> None:
            try:
                sharedserver.wait_ready(probe, timeout=c.attach_timeout)
            except sharedserver.SharedServerError as e:
                raise CribUserError(
                    f"the crib DuckDB server is not answering at {uri} ({e}). It "
                    "is configured as externally managed ([duckdb] launch = "
                    "false): start it (`python -m crib.store_quack`, e.g. from a "
                    "combiner sharedServers entry), or set launch = true to let "
                    "crib start it"
                ) from e

        wait()
        store = QuackStore.connect(uri, token, ext, relaunch=wait)
        return store, store.close

    held = [0]  # sharedserver refcounts this crib holds; each one is released at close

    def attach() -> None:
        """Reuse-or-start the server and wait until it answers. Also the client's
        relaunch hook: a server that died is started again on the next op."""
        sharedserver.use(c.server_name, command, c.grace_period)
        held[0] += 1
        sharedserver.wait_ready(probe, timeout=180.0)

    def release() -> None:
        while held[0]:
            held[0] -= 1
            sharedserver.unuse(c.server_name)

    try:
        attach()
        store = QuackStore.connect(uri, token, ext, relaunch=attach)
    except Exception:
        release()  # don't leak the refcount on failure
        raise

    def close() -> None:
        store.close()
        release()

    return store, close
