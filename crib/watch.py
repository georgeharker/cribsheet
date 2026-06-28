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
from pathlib import Path
from typing import Any, Awaitable, Callable

_IGNORE = ["*~", ".*.swp", "*.tmp", "4913", ".#*", "*.orig"]
_IGNORE_DIRS = {".git", ".versions"}
DEBOUNCE_SEC = 0.2


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


class Watcher:
    """Watches `projects_dir`; calls `on_change(project, relpath)` debounced."""

    def __init__(self, projects_dir: Path,
                 on_change: Callable[[str, str], Awaitable[None]],
                 loop: asyncio.AbstractEventLoop) -> None:
        self.projects_dir = projects_dir
        self._on_change = on_change
        self._loop = loop
        self._pending: dict[str, asyncio.TimerHandle] = {}
        self._observer: Any = None

    def start(self) -> None:
        from watchdog.events import FileSystemEventHandler  # lazy
        from watchdog.observers import Observer

        watcher = self

        class _Handler(FileSystemEventHandler):
            def _emit(self, raw_path: str) -> None:
                decoded = decode(watcher.projects_dir, raw_path)
                if decoded is None:
                    return
                watcher._loop.call_soon_threadsafe(watcher._schedule, decoded)

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
                # index_file drops the chunks once it sees the path is gone
                if not e.is_directory:
                    self._emit(e.src_path)

        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self._observer = Observer()
        self._observer.schedule(_Handler(), str(self.projects_dir), recursive=True)
        self._observer.start()

    def _schedule(self, key: tuple[str, str]) -> None:
        sk = "\x00".join(key)
        if (h := self._pending.pop(sk, None)) is not None:
            h.cancel()
        self._pending[sk] = self._loop.call_later(DEBOUNCE_SEC, self._fire, key)

    def _fire(self, key: tuple[str, str]) -> None:
        self._pending.pop("\x00".join(key), None)
        self._loop.create_task(self._on_change(*key))

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
