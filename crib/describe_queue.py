"""Per-file, exponential-backoff scheduler for the LLM description pass.

The code index has two facets: the STRUCTURAL one (symbols + call graph, from the
LSP) and the SEMANTIC one (a one-line description per symbol, from an LLM).
Structural indexing is cheap and wants to be live on every save; the LLM pass is
the expensive part and the least latency-sensitive. This queue decouples them —
the structural pass persists immediately and hands the changed symbols here, and a
per-file timer coalesces edit bursts before spending any LLM call.

Backoff is the whole idea: a file edited repeatedly keeps deferring (delay =
min(base·2^level, cap)), so an active editing session collapses to ONE focused
describe once the file settles. The SAME backoff is the retry policy — a describe
that raises (LLM down / timeout) re-arms one round later rather than hammering.

Changed-symbol bodies ride in memory on the entry, so the settle uses the focused
`describe_symbols` over only what changed (not a whole-file pass). A crash loses the
queue, but the structural pass left BLANK descriptions on disk — a durable
"needs describing" signal the startup backlog scan re-drives — so this state is
purely transient by design (docs/code-symbol-index.md § Deferred describe).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable


class _Entry:
    """One pending file: the changed symbols (fqname → body-carrying dict), how many
    times its window has been re-armed (the backoff exponent), and its live timer."""

    __slots__ = ("root", "pending", "level", "timer")

    def __init__(self, root: Path) -> None:
        self.root = root
        self.pending: dict[str, dict] = {}          # fqname → {name, kind, content_hash, _body}
        self.level = 0
        self.timer: asyncio.TimerHandle | None = None


class DescribeQueue:
    """Schedules deferred, coalesced, backoff-paced describe passes, keyed per file.

    `describe_fn(project, root, relpath, pending)` does the actual LLM call + patched
    write; it MUST raise on failure so the queue can re-arm (backoff-as-retry)."""

    def __init__(self, loop: asyncio.AbstractEventLoop,
                 describe_fn: Callable[[str, Path, str, dict[str, dict]], Awaitable[Any]],
                 base: float = 2.0, cap: float = 240.0) -> None:
        self._loop = loop
        self._describe = describe_fn
        self._base = max(0.1, base)
        self._cap = max(self._base, cap)
        self._q: dict[tuple[str, str], _Entry] = {}

    def enqueue(self, project: str, root: Path, relpath: str,
                symbols: dict[str, dict]) -> None:
        """Add changed symbols for a file and (re)arm its backoff timer. Thread-safe:
        the structural pass runs off the event loop, so hop back onto it."""
        if not symbols:
            return
        self._loop.call_soon_threadsafe(self._arm, project, root, relpath, symbols)

    # --- on the loop thread from here down -----------------------------------
    def _arm(self, project: str, root: Path, relpath: str,
             symbols: dict[str, dict]) -> None:
        key = (project, relpath)
        e = self._q.get(key)
        if e is None:
            e = self._q[key] = _Entry(root)
        e.root = root
        e.pending.update(symbols)                   # newest body per fqname wins
        if e.timer is not None:
            e.timer.cancel()
        delay = min(self._base * (2 ** e.level), self._cap)
        e.level += 1
        e.timer = self._loop.call_later(delay, self._fire, key)

    def _fire(self, key: tuple[str, str]) -> None:
        e = self._q.pop(key, None)
        if e is None or not e.pending:
            return
        project, relpath = key
        self._loop.create_task(self._run(project, e.root, relpath, e.pending))

    async def _run(self, project: str, root: Path, relpath: str,
                   pending: dict[str, dict]) -> None:
        try:
            await self._describe(project, root, relpath, pending)
        except Exception:  # noqa: BLE001 — backoff-as-retry: re-arm one round later
            self._arm(project, root, relpath, pending)

    # --- introspection / teardown --------------------------------------------
    def pending_files(self) -> int:
        return len(self._q)

    def pending_symbols(self) -> int:
        return sum(len(e.pending) for e in self._q.values())

    def stop(self) -> None:
        """Cancel every pending timer and drop the queue (a clean shutdown; any
        unfired describes heal from disk on next start via the backlog scan)."""
        for e in self._q.values():
            if e.timer is not None:
                e.timer.cancel()
        self._q.clear()
