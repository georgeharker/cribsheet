"""Skip a test that needs prompt file events when this host isn't delivering
them (fseventsd saturated, say), with what was measured in the reason, rather
than failing after its own timeout. Uses the daemon's own probe
(crib.watch.Watcher.probe_once); Linux CI (inotify) runs the tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


def require_prompt_file_events(tmp_path: Path, timeout: float = 3.0) -> None:
    pytest.importorskip("watchdog")
    from crib.watch import Watcher

    async def probe() -> tuple[str, float | None]:
        async def on_change(*_a: object) -> None:
            return None

        d = tmp_path / "fsevents-probe"
        d.mkdir(exist_ok=True)
        w = Watcher(d, on_change, asyncio.get_running_loop())
        w.start()
        try:
            await asyncio.sleep(0.2)  # let the observer settle
            state = await w.probe_once(timeout)
            return state, w.health().get("last_latency_s")
        finally:
            w.stop()

    state, latency = asyncio.run(probe())
    if state != "healthy":
        seen = (
            f"last event after {latency}s"
            if latency is not None
            else (f"no event within {timeout:.0f}s")
        )
        pytest.skip(
            f"file events are not delivered promptly on this host ({state}: {seen})"
        )
