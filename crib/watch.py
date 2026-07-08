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
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable

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
    if name.startswith(".") and name.endswith(".tmp"):
        return True
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

    def _schedule_dir(self, d: str) -> None:
        from watchdog.events import FileSystemEventHandler
        if not Path(d).exists():
            return
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

        self._observer.schedule(_Handler(), str(d), recursive=True)

    def _schedule(self, key: tuple[Any, ...]) -> None:
        sk = "\x00".join(str(x) for x in key)
        if (h := self._pending.pop(sk, None)) is not None:
            h.cancel()
        self._pending[sk] = self._loop.call_later(DEBOUNCE_SEC, self._fire, key)

    def _fire(self, key: tuple[Any, ...]) -> None:
        self._pending.pop("\x00".join(str(x) for x in key), None)
        self._loop.create_task(self._dispatch(*key))

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None


class Watcher(_FSWatcher):
    """Watches `projects_dir`; reloads a note on change — `on_change(project, relpath)`."""

    def __init__(self, projects_dir: Path,
                 on_change: Callable[[str, str], Awaitable[None]],
                 loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(loop)
        self.projects_dir = projects_dir
        self._on_change = on_change

    def _watch_dirs(self) -> list[str]:
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        return [str(self.projects_dir)]

    def _decode(self, raw_path: str, deleted: bool) -> tuple[str, str] | None:
        # index_file drops chunks once it sees the path is gone, so deletes flow too
        return decode(self.projects_dir, raw_path)

    async def _dispatch(self, project: str, relpath: str) -> None:
        await self._on_change(project, relpath)


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
        self._batch: dict[str, dict[str, tuple[str, bool]]] = {}
        self._batch_timers: dict[str, asyncio.TimerHandle] = {}

    def _code_exts(self) -> set[str]:
        if self._exts is None:
            from .codeindex import load_specs
            self._exts = {e for sp in load_specs().values() if isinstance(sp, dict)
                          for e in (sp.get("extensionToLanguage") or {})}
        return self._exts

    def watch_root(self, project: str, root: str | Path) -> None:
        """Register (or re-point) a source root for a project; idempotent."""
        key = str(Path(root).resolve())
        new = key not in self._roots
        self._roots[key] = project
        if new and self._observer is not None:
            self._schedule_dir(key)

    def watches(self, project: str) -> bool:
        """Is this project's source root being watched (so its index refreshes
        eagerly on save, and a per-query source scan is redundant)?"""
        return project in self._roots.values()

    def _watch_dirs(self) -> list[str]:
        return list(self._roots)

    def _decode(self, raw_path: str, deleted: bool) -> tuple[str, str, str, bool] | None:
        p = Path(raw_path)
        if any(part in _CODE_IGNORE_DIRS for part in p.parts):
            return None
        suffix = p.suffix.lower()
        is_doc_ext = suffix in DOC_EXTS
        if suffix not in self._code_exts() and not is_doc_ext:
            # extensionless files route by CONTENT (name/shebang/#compdef marker),
            # the same grammar the sweep enumeration uses — a NEW autoload file
            # must reach the index without waiting for the next full sweep. (A
            # DELETED one can't be sniffed; its entry falls to the lazy
            # revalidation gate, which drops symbols of missing sources.)
            if suffix or not p.is_file():
                return None
            from .codeindex import content_lang
            if content_lang(p) is None:
                return None
        # A delete event for a file that exists is FSEvents/watchdog coalescing
        # noise from a rename-style save — record it as a change, not a delete
        # (the dispatch handler re-verifies against the final state anyway).
        exists = p.exists()
        deleted = deleted and not exists
        rp = p.resolve() if exists else p
        best: tuple[str, str, str, bool] | None = None
        for key, proj in self._roots.items():
            try:
                rel = rp.relative_to(key)
            except ValueError:
                continue
            # A doc-EXTENSION file counts as a doc ONLY if it matches this project's
            # declared `docs:` globs — the same scoping the sweep honors, so which
            # docs get indexed no longer depends on how the change arrived. A `.md`
            # outside the globs is not ours to index (falls through like any other
            # non-indexable file); code files are unaffected.
            is_doc = is_doc_ext and _matches_doc_globs(Path(key), rel)
            if is_doc_ext and not is_doc:
                continue
            if best is None or len(key) > len(best[1]):
                # relpath prefixed so the handler routes doc vs code; the batch key
                # stays unique per file either way.
                tag = f"\x00doc\x00{rel}" if is_doc else str(rel)
                best = (proj, key, tag, deleted)
        return best

    # Coalesce: instead of the base's per-file debounce, accumulate every changed
    # file for a project and (re)arm ONE timer, so a burst becomes a single dispatch.
    def _schedule(self, key: tuple[Any, ...]) -> None:
        project, root, relpath, deleted = key
        self._batch.setdefault(project, {})[relpath] = (root, deleted)  # last event wins
        if (h := self._batch_timers.pop(project, None)) is not None:
            h.cancel()
        self._batch_timers[project] = self._loop.call_later(
            CODE_DEBOUNCE_SEC, self._flush, project)

    def _flush(self, project: str) -> None:
        self._batch_timers.pop(project, None)
        changes = self._batch.pop(project, None)
        if changes:
            self._loop.create_task(self._dispatch(project, changes))

    async def _dispatch(self, *key: Any) -> None:  # (project, changes)
        await self._on_change(*key)

    def stop(self) -> None:
        for h in self._batch_timers.values():
            h.cancel()
        self._batch_timers.clear()
        self._batch.clear()
        super().stop()
