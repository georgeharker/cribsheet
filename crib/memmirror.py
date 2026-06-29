"""Live mirror of Claude Code harness memory into crib projects (DESIGN §13).

The sibling of the notes Watcher (watch.py): where that watches crib's own
notes dir, this watches each bound harness memory dir and re-runs the one-shot
sync (`Crib.import_claude_memory`) when a memory file changes — so writing a
memory via the harness makes it searchable in crib within moments. Bindings come
from the registry (`crib import-memory` opts a repo in); deletions and edits both
funnel through the same idempotent sync, so events are safe to coalesce.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, Callable

from . import claudemem
from .claudemem import MemoryBindings

DEBOUNCE_SEC = 0.5  # a memory write is one file; coalesce its create/modify burst


class MemoryMirror:
    """Watches bound harness memory dirs; re-syncs a binding on change."""

    def __init__(self, bindings: MemoryBindings,
                 sync: Callable[[Path, str], Coroutine[Any, Any, Any]],
                 loop: asyncio.AbstractEventLoop) -> None:
        self._bindings = bindings
        self._sync = sync          # (root, project) -> awaitable, runs the mirror
        self._loop = loop
        self._pending: dict[str, asyncio.TimerHandle] = {}
        self._observer: Any = None
        # munged-memory-dir -> (root, project), built from the registry at start
        self._watched: dict[str, tuple[Path, str]] = {}

    async def catch_up(self) -> None:
        """Sync every binding once at startup (like the notes reconcile)."""
        for b in self._bindings.all():
            try:
                await self._sync(Path(b["root"]), b["project"])
            except Exception:  # noqa: BLE001 — a stale/removed root must not abort the rest
                pass

    def start(self) -> None:
        from watchdog.events import FileSystemEventHandler  # lazy
        from watchdog.observers import Observer

        dirs = self._resolve_dirs()
        if not dirs:
            return
        mirror = self

        class _Handler(FileSystemEventHandler):
            def _emit(self, raw_path: str) -> None:
                binding = mirror._match(raw_path)
                if binding is not None:
                    mirror._loop.call_soon_threadsafe(mirror._schedule, binding)

            def on_created(self, e):  # noqa: ANN001
                if not e.is_directory:
                    self._emit(e.src_path)

            def on_modified(self, e):  # noqa: ANN001
                if not e.is_directory:
                    self._emit(e.src_path)

            def on_moved(self, e):  # noqa: ANN001
                if not e.is_directory:
                    self._emit(e.dest_path)

            def on_deleted(self, e):  # noqa: ANN001 — re-sync drops the chunk
                if not e.is_directory:
                    self._emit(e.src_path)

        self._observer = Observer()
        for d in dirs:
            self._observer.schedule(_Handler(), str(d), recursive=False)
        self._observer.start()

    def _resolve_dirs(self) -> list[Path]:
        self._watched.clear()
        dirs: list[Path] = []
        for b in self._bindings.all():
            root = Path(b["root"])
            mem = claudemem.harness_memory_dir(root)
            if mem.is_dir():
                self._watched[str(mem.resolve())] = (root, b["project"])
                dirs.append(mem)
        return dirs

    def _match(self, raw_path: str) -> tuple[Path, str] | None:
        if not raw_path.endswith(".md") or Path(raw_path).name == "MEMORY.md":
            return None
        parent = str(Path(raw_path).resolve().parent)
        return self._watched.get(parent)

    def _schedule(self, binding: tuple[Path, str]) -> None:
        key = str(binding[0])
        if (h := self._pending.pop(key, None)) is not None:
            h.cancel()
        self._pending[key] = self._loop.call_later(DEBOUNCE_SEC, self._fire, binding)

    def _fire(self, binding: tuple[Path, str]) -> None:
        self._pending.pop(str(binding[0]), None)
        self._loop.create_task(self._sync(binding[0], binding[1]))

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
