"""File watcher (DESIGN §9). Cross-platform via watchdog.

A single observer rooted at `projects/` covers every project — including ones
created mid-session — decoding `(project, relpath)` from each path. Handles
created/modified/moved (editors atomic-rename, so `modified` alone misses
saves), debounces per-path, filters temp/dotfiles, and leans on the hash gate so
duplicate events are harmless no-ops.
"""

from __future__ import annotations

import asyncio
import fnmatch
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable

from .util import spawn

# `*.tmp` covers the dotted temp form too (fnmatch on the basename), so there is
# one pattern list, not a pattern list plus a special case for `.foo.tmp`.
_IGNORE = ["*~", ".*.swp", "*.tmp", "4913", ".#*", "*.orig"]
_IGNORE_DIRS = {".git", ".versions"}
DEBOUNCE_SEC = 0.2
# Code edits arrive in bursts (a formatter rewrites a tree, `git checkout` touches
# hundreds of files), and each reindex is a live LSP call — so the code watcher
# COALESCES per project over a slightly longer window, then hands the whole changed
# set to one dispatch. A batch bigger than the fallback threshold isn't reindexed
# file-by-file at all: it's collapsed to a single project revalidation sweep.
CODE_DEBOUNCE_SEC = 0.5
CODE_BATCH_FALLBACK = 50


def _ignored(path: Path) -> bool:
    if any(part in _IGNORE_DIRS for part in path.parts):
        return True
    name = path.name
    return any(fnmatch.fnmatch(name, pat) for pat in _IGNORE) or path.suffix != ".md"


def decode(projects_dir: Path, raw_path: str) -> tuple[str, str] | None:
    """Map a filesystem path to (project, relpath) under `<project>/notes/…`."""
    p = Path(raw_path)
    if _ignored(p):
        return None
    try:
        rel = p.resolve().relative_to(projects_dir.resolve())
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 3 or parts[1] != "notes":
        return None
    return parts[0], str(Path(*parts[2:]))


def decode_store(stores: dict[str, Path], raw_path: str) -> tuple[str, str] | None:
    """Same, for a project adopted into a repo: its notes dir IS the root, so the
    project name comes from the root that matched rather than from a path segment.

    Longest matching root wins, for the same reason the code watcher prefers it —
    one store nested inside another's repo must not decode to the outer project."""
    p = Path(raw_path)
    if _ignored(p):
        return None
    rp = p.resolve()
    best: tuple[int, str, str] | None = None
    for project, notes_dir in stores.items():
        try:
            rel = rp.relative_to(notes_dir.resolve())
        except ValueError:
            continue
        if best is None or len(str(notes_dir)) > best[0]:
            best = (len(str(notes_dir)), project, str(rel))
    return (best[1], best[2]) if best else None


class _FSWatcher:
    """Shared watchdog plumbing for both watchers: observer lifecycle, a filesystem
    event handler, and per-key debounce. Subclasses provide `_watch_dirs()` (dirs to
    schedule), `_decode(raw_path, deleted)` (path → a key tuple, or None to ignore),
    and the async `_dispatch(*key)` reaction. Notes-watcher reloads notes; code-watcher
    reindexes code — same plumbing, different decode + reaction."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._pending: dict[str, asyncio.TimerHandle] = {}
        self._observer: Any = None
        # dir -> watchdog ObservedWatch, so a superseded root can be unscheduled
        self._watches: dict[str, Any] = {}
        self._missing: set[str] = set()     # dirs warned about once (see _schedule_dir)
        # strong refs to in-flight dispatches (asyncio holds only weak ones, so an
        # unreferenced task can be GC'd mid-flight — a save's reindex silently lost)
        self._tasks: set[asyncio.Task] = set()

    # --- subclass hooks ---
    def _watch_dirs(self) -> list[str]:
        return []

    def _decode(self, raw_path: str, deleted: bool) -> tuple[Any, ...] | None:
        raise NotImplementedError

    async def _dispatch(self, *key: Any) -> None:
        raise NotImplementedError

    # --- shared machinery ---
    def start(self) -> None:
        from watchdog.observers import Observer
        self._observer = Observer()
        for d in self._watch_dirs():
            self._schedule_dir(d)
        self._observer.start()

    def _schedule_dir(self, d: str) -> bool:
        """Watch `d` (idempotent), returning whether it is now watched.

        A directory that doesn't exist yet can't be scheduled — but it used to be
        skipped SILENTLY and never retried, so a project whose checkout was
        missing at registration simply never live-updated for the rest of the
        session with nothing said. Say it once, and let a later `watch_root` (or a
        restart) pick it up."""
        from watchdog.events import FileSystemEventHandler
        if d in self._watches:
            return True
        if not Path(d).exists():
            if d not in self._missing:
                self._missing.add(d)
                print(f"[crib] not watching {d}: no such directory — edits there "
                      f"won't reindex until it exists and the root is re-registered "
                      f"(or crib restarts)", file=sys.stderr)
            return False
        self._missing.discard(d)
        watcher = self

        class _Handler(FileSystemEventHandler):
            def _emit(self, raw_path: str, deleted: bool = False) -> None:
                key = watcher._decode(raw_path, deleted)
                if key is not None:
                    watcher._loop.call_soon_threadsafe(watcher._schedule, key)

            def on_created(self, e):  # noqa: ANN001
                if not e.is_directory:
                    self._emit(e.src_path)

            def on_modified(self, e):  # noqa: ANN001
                if not e.is_directory:
                    self._emit(e.src_path)

            def on_moved(self, e):  # noqa: ANN001
                if not e.is_directory:
                    self._emit(e.dest_path)

            def on_deleted(self, e):  # noqa: ANN001
                if not e.is_directory:
                    self._emit(e.src_path, deleted=True)

        self._watches[d] = self._observer.schedule(_Handler(), str(d), recursive=True)
        return True

    def _unschedule_dir(self, d: str) -> None:
        """Stop watching `d` — for a root that has been superseded."""
        watch = self._watches.pop(d, None)
        if watch is not None and self._observer is not None:
            try:
                self._observer.unschedule(watch)
            except Exception:  # noqa: BLE001 — already gone is the goal state
                pass

    def _schedule(self, key: tuple[Any, ...]) -> None:
        sk = "\x00".join(str(x) for x in key)
        if (h := self._pending.pop(sk, None)) is not None:
            h.cancel()
        self._pending[sk] = self._loop.call_later(DEBOUNCE_SEC, self._fire, key)

    def _fire(self, key: tuple[Any, ...]) -> None:
        self._pending.pop("\x00".join(str(x) for x in key), None)
        spawn(self._loop, self._dispatch(*key), self._tasks,
              f"watch dispatch {key}")

    def stop(self) -> None:
        # Cancel pending debounce timers FIRST (as CodeWatcher.stop does for its
        # batch timers): a timer that fires after shutdown spawns a dispatch onto a
        # loop that is closing, so the reindex either dies in a dead task or raises
        # from call_soon on a closed loop. Nothing useful can come of it — the
        # watcher is gone, and the startup reconcile catches the edit next run.
        for h in self._pending.values():
            h.cancel()
        self._pending.clear()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
        self._watches.clear()


class Watcher(_FSWatcher):
    """Watches `projects_dir`; reloads a note on change — `on_change(project, relpath)`.

    `stores` adds the notes dir of each project adopted into a repo
    (docs/plans/repo-local-storage): those notes are edited in the checkout, so
    the global tree alone would never see them. The ignore rules are the shared
    ones and apply per root — including `.versions/`, which each in-repo store
    keeps inside itself."""

    def __init__(self, projects_dir: Path,
                 on_change: Callable[[str, str], Awaitable[None]],
                 loop: asyncio.AbstractEventLoop,
                 stores: dict[str, Path] | None = None) -> None:
        super().__init__(loop)
        self.projects_dir = projects_dir
        self._on_change = on_change
        self.stores = dict(stores or {})

    def _watch_dirs(self) -> list[str]:
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        # A store dir that doesn't exist yet is reported once by `_schedule_dir`
        # rather than skipped silently.
        return [str(self.projects_dir), *(str(d) for d in self.stores.values())]

    def _decode(self, raw_path: str, deleted: bool) -> tuple[str, str] | None:
        # index_file drops chunks once it sees the path is gone, so deletes flow too
        key = decode(self.projects_dir, raw_path)
        return key if key is not None else decode_store(self.stores, raw_path)

    async def _dispatch(self, project: str, relpath: str) -> None:
        # Guarded like the code watcher's batch dispatch: one note that won't parse
        # (a hand edit, conflict markers) or a file that vanished between event and
        # dispatch must not take the reindex down as an unhandled task exception —
        # `spawn` would only print it, and the next save/reconcile fixes it anyway.
        try:
            await self._on_change(project, relpath)
        except Exception as e:  # noqa: BLE001 — one bad note never kills the watcher
            print(f"[crib] watch reindex failed ({project}/{relpath}): {e}",
                  file=sys.stderr)


_CODE_IGNORE_DIRS = {".git", ".versions", "node_modules", ".venv", "venv",
                     "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
                     "dist", "build", "target", ".tox", ".idea", "site-packages",
                     ".cache", ".claude"}
# Prose docs indexed in-situ alongside code (same source roots, same watcher).
DOC_EXTS = {".md", ".rst", ".txt", ".markdown"}


def _matches_doc_globs(root: Path, rel: Path) -> bool:
    """True when `rel` matches the repo's `.crib` `docs:` globs — the SAME scoping
    the in-situ sweep honors (`index_docs_insitu`), so which prose files count as
    docs no longer depends on whether the change arrived via a save or a sweep.
    (`full_match` mirrors the sweep's `Path.glob`, incl. `**`; needs py3.13+.)"""
    from .config import CribLink
    link = CribLink.find(root)
    if link is None:
        return False
    rp = PurePosixPath(rel.as_posix())
    return any(rp.full_match(pat) for pat in link.doc_patterns)


class CodeWatcher(_FSWatcher):
    """Watches the SOURCE roots of code-indexed projects; reindexes an indexable code
    file on change — `on_change(project, root, relpath, deleted)`. Roots are registered
    as projects get indexed (`watch_root`), so a repo onboarded mid-session is watched
    at once."""

    def __init__(self, on_change: Callable[[str, dict[str, tuple[str, bool]]],
                                           Awaitable[None]],
                 loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(loop)
        self._on_change = on_change
        self._roots: dict[str, str] = {}          # abs root → project
        self._exts: set[str] | None = None
        # per-project coalescing: {project: {relpath: (root, deleted)}} + one timer
        self._batch: dict[str, dict[str, tuple[str, bool, str]]] = {}
        self._batch_timers: dict[str, asyncio.TimerHandle] = {}

    def _code_exts(self) -> set[str]:
        if self._exts is None:
            from .codeindex import load_specs
            self._exts = {e for sp in load_specs().values() if isinstance(sp, dict)
                          for e in (sp.get("extensionToLanguage") or {})}
        return self._exts

    def watch_root(self, project: str, root: str | Path) -> None:
        """Register (or RE-POINT) a source root for a project; idempotent.

        Re-pointing drops the old root: a project whose `.crib` moved (a fresh
        clone elsewhere, a submodule promoted out of tree) used to leave its
        previous root scheduled forever — every save in the ABANDONED checkout
        still decoded to this project and reindexed symbols from a tree the
        project no longer describes, and the stale watch outlived the root's own
        deletion."""
        key = str(Path(root).resolve())
        superseded = [k for k, p in self._roots.items() if p == project and k != key]
        self._roots[key] = project
        for old in superseded:
            del self._roots[old]
            self._unschedule_dir(old)
        if self._observer is not None:
            self._schedule_dir(key)         # idempotent; retries a formerly-missing dir

    def watches(self, project: str) -> bool:
        """Is this project's source root being watched (so its index refreshes
        eagerly on save, and a per-query source scan is redundant)?"""
        return project in self._roots.values()

    def _watch_dirs(self) -> list[str]:
        self._code_exts()       # prime the spec table: it reads config files, and
        return list(self._roots)    # `_decode` must never do that on the event thread

    # Batch payload per file: (root, deleted, kind) where kind is how the path was
    # CLASSIFIED cheaply and what still has to be confirmed with I/O later:
    #   "code"  — a known code extension; nothing left to check
    #   "doc"   — a prose extension; still needs the repo's `docs:` globs applied
    #   "sniff" — extensionless; still needs a content sniff to route by language
    def _decode(self, raw_path: str, deleted: bool) -> tuple[str, str, str, bool, str] | None:
        """Classify an event by PATH ALONE — no filesystem access, no `.crib` parse.

        This runs on the watchdog EVENT THREAD, which must return fast: it drains
        the OS notification queue for every watched repo, and anything slow here
        (a `stat`, a file read to sniff a shebang, a YAML `.crib` load per event)
        applies to EVERY event in a burst — a `git checkout` touching a thousand
        files paid a thousand YAML parses before the debounce had even started
        coalescing them, and a slow drain drops events outright on some backends.
        So the thread does only string work, and everything needing I/O is deferred
        past the debounce boundary into `_resolve_batch` (a worker thread)."""
        p = Path(raw_path)
        if any(part in _CODE_IGNORE_DIRS for part in p.parts):
            return None
        suffix = p.suffix.lower()
        if suffix in DOC_EXTS:
            kind = "doc"
        elif suffix in self._code_exts():
            kind = "code"
        elif suffix:
            return None
        else:
            # extensionless files route by CONTENT (name/shebang/#compdef marker),
            # the same grammar the sweep enumeration uses — a NEW autoload file
            # must reach the index without waiting for the next full sweep. (A
            # DELETED one can't be sniffed; its entry falls to the lazy
            # revalidation gate, which drops symbols of missing sources.)
            kind = "sniff"
        best: tuple[str, str, str, bool, str] | None = None
        for key, proj in self._roots.items():
            try:
                rel = p.relative_to(key)        # pure path arithmetic, no syscall
            except ValueError:
                continue
            if best is None or len(key) > len(best[1]):
                best = (proj, key, str(rel), deleted, kind)
        return best

    # Coalesce: instead of the base's per-file debounce, accumulate every changed
    # file for a project and (re)arm ONE timer, so a burst becomes a single dispatch.
    def _schedule(self, key: tuple[Any, ...]) -> None:
        project, root, relpath, deleted, kind = key
        self._batch.setdefault(project, {})[relpath] = (root, deleted, kind)  # last wins
        if (h := self._batch_timers.pop(project, None)) is not None:
            h.cancel()
        self._batch_timers[project] = self._loop.call_later(
            CODE_DEBOUNCE_SEC, self._flush, project)

    def _flush(self, project: str) -> None:
        self._batch_timers.pop(project, None)
        changes = self._batch.pop(project, None)
        if changes:
            spawn(self._loop, self._dispatch(project, changes), self._tasks,
                  f"code watch dispatch {project}")

    @staticmethod
    def _resolve_batch(changes: dict[str, tuple[str, bool, str]]
                       ) -> dict[str, tuple[str, bool]]:
        """Apply the I/O-bound half of the decode to a whole coalesced batch —
        existence, the in-repo store exclusion, the `docs:` globs, the extensionless
        content sniff — and return the `{relpath: (root, deleted)}` the change
        handler consumes. Runs ONCE per burst on a worker thread, not once per event
        on the watchdog thread, and the `.crib` is parsed once per root instead of
        once per file. (The store exclusion lands here rather than in `_decode` for
        that reason: knowing where the store is means parsing the `.crib`, which the
        event thread must never do.)"""
        from .codeindex import content_lang
        from .config import CribLink
        links: dict[str, Any] = {}              # root -> CribLink (parsed once)

        def _link(root: str) -> Any:
            if root not in links:
                links[root] = CribLink.find(Path(root))
            return links[root]

        out: dict[str, tuple[str, bool]] = {}
        for relpath, (root, deleted, kind) in changes.items():
            path = Path(root) / relpath
            exists = path.exists()
            # A delete event for a file that exists is FSEvents/watchdog coalescing
            # noise from a rename-style save — record it as a change, not a delete
            # (the change handler re-verifies against the final state anyway).
            deleted = deleted and not exists
            # A save inside the repo's own crib store is a NOTE edit — the notes
            # watcher already has that root and reindexes it as a note. Indexing it
            # here as well would give the same bytes a second chunk identity, so the
            # store is skipped the way `.git`/`.versions` are (CribLink.in_store).
            if (link := _link(root)) is not None and link.in_store(path):
                continue
            if kind == "sniff":
                if not exists or not path.is_file() or content_lang(path) is None:
                    continue
            elif kind == "doc":
                # A doc-EXTENSION file counts as a doc ONLY if it matches this
                # project's declared `docs:` globs — the same scoping the sweep
                # honors, so which docs get indexed no longer depends on how the
                # change arrived. A `.md` outside the globs is not ours to index.
                rp = PurePosixPath(Path(relpath).as_posix())
                if link is None or not any(rp.full_match(pat)
                                           for pat in link.doc_patterns):
                    continue
                # relpath prefixed so the handler routes doc vs code
                relpath = f"\x00doc\x00{relpath}"
            out[relpath] = (root, deleted)
        return out

    async def _dispatch(self, project: str,
                        changes: dict[str, tuple[str, bool, str]]) -> None:
        resolved = await asyncio.to_thread(self._resolve_batch, changes)
        if resolved:
            await self._on_change(project, resolved)

    def stop(self) -> None:
        for h in self._batch_timers.values():
            h.cancel()
        self._batch_timers.clear()
        self._batch.clear()
        super().stop()
