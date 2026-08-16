"""The daemon's event loop must keep ticking while crib indexes (robustness 1.1-1.3).

Every write verb funnels through `IndexEngine.index_file`, which embeds — a real
model forward, ~0.9s for a 120-section note. Run on the loop thread that freezes
every other MCP client (and `DaemonClient._wait_ready` then declares the daemon
dead). These tests pin the offload: the work is still *paid* in full, just not on
the loop, and the stores it writes through survive concurrent readers.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from crib.app import Crib
from crib.config import Config
from crib.embed import HashEmbedder
from crib.paths import Paths
from crib.store import InMemoryStore, JsonStore, Record

SLOW = 0.5          # stand-in for a real model forward
# Acceptance bar for the loop gap during a big write. What it discriminates is
# offloaded-vs-not: an embed left on the loop shows up as a gap at SLOW, so any bar
# comfortably under 0.5 catches the regression. It sits well ABOVE the 0.02 tick
# because a 20ms heartbeat routinely slips past 100ms on a machine saturated by the
# rest of the suite (or a CI runner) — jitter that says nothing about the offload.
MAX_GAP = 0.25
TICK = 0.02         # heartbeat period


class SlowEmbedder:
    """HashEmbedder with a deliberate stall, so a test can tell "the loop didn't
    block" from "there was nothing to block on"."""

    def __init__(self, delay: float = SLOW) -> None:
        self._inner = HashEmbedder()
        self.dim = self._inner.dim
        self.delay = delay
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        time.sleep(self.delay)
        return self._inner.embed(texts)

    def embed_query(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)


def _big_note(sections: int = 120) -> str:
    return "\n\n".join(
        f"## Section {i}\nSome prose about topic {i} with enough words to make a "
        f"chunk that is worth embedding, repeated for bulk. " * 2
        for i in range(sections))


@pytest.fixture()
def slow_crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    config = Config()
    config.retrieve.rerank = False      # no model download in a unit test
    crib = Crib(Paths.resolve().ensure(), config, InMemoryStore())
    emb = SlowEmbedder()
    crib.embedder = emb
    crib.index.embedder = emb
    crib.index.summaries._embed = emb
    return crib, emb


async def _measure(coro):
    """Run `coro` against a 20ms heartbeat; return (elapsed, max loop gap)."""
    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(TICK)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    hb = asyncio.create_task(heartbeat())
    await asyncio.sleep(TICK * 3)       # let the heartbeat settle
    gaps.clear()
    t0 = time.perf_counter()
    await coro
    elapsed = time.perf_counter() - t0
    stop.set()
    await hb
    return elapsed, max(gaps)


def test_store_note_does_not_stall_the_loop(slow_crib):
    crib, emb = slow_crib

    async def main():
        return await _measure(
            crib.store_note(_big_note(), title="big", project="p"))

    elapsed, worst = asyncio.run(main())
    assert emb.calls >= 1            # the embed really ran (not hash-gated away)
    assert elapsed >= SLOW           # ...and the write really paid for it
    assert worst < MAX_GAP, f"loop blocked for {worst:.3f}s"


def test_reindex_sweep_does_not_stall_the_loop(slow_crib):
    """The reconcile path (many files, one after another) yields between files."""
    crib, _ = slow_crib
    for i in range(3):
        asyncio.run(crib.store_note(f"note {i} body text", title=f"n{i}",
                                    project="p"))
    (crib.notes_dir("p") / "n0.md").write_text("---\ntitle: n0\n---\nchanged body\n")

    async def main():
        return await _measure(crib.reconcile_all())

    _elapsed, worst = asyncio.run(main())
    assert worst < MAX_GAP, f"loop blocked for {worst:.3f}s"


def test_status_reports_background_reconcile(slow_crib):
    """1.3: the startup sweep runs as a background task, and `status` — served
    while it runs — says so."""
    crib, _ = slow_crib
    asyncio.run(crib.store_note("something to reconcile", title="n", project="p"))
    assert crib.status()["reconciling"] is False

    async def main():
        crib.reconcile_in_background(asyncio.get_running_loop())
        await asyncio.sleep(0)          # let the task start
        seen = crib.status()            # served DURING the sweep
        while crib._bg_tasks:
            await asyncio.sleep(0.01)
        return seen

    seen = asyncio.run(main())
    assert seen["reconciling"] is True
    assert seen["reconcile_remaining"] >= 0
    assert crib.status()["reconciling"] is False


def test_jsonstore_survives_concurrent_writers_and_readers(tmp_path):
    """1.2: indexing writes from worker threads while FastMCP's sync tools read
    from its threadpool — a plain dict rewritten wholesale would raise
    `dictionary changed size during iteration`."""
    store = JsonStore(tmp_path / "store.json")
    vec = [1.0] + [0.0] * 7
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer() -> None:
        try:
            for i in range(300):
                store.upsert([Record(f"c{i}", vec, f"doc {i}",
                                     {"project": "p", "relpath": "n.md"})])
                if i % 3 == 0:
                    store.delete([f"c{i - 1}"])
        except BaseException as e:  # noqa: BLE001 — the point of the test
            errors.append(e)
        finally:
            stop.set()

    t = threading.Thread(target=writer)
    t.start()
    reads = 0
    while not stop.is_set():
        try:
            store.get_meta({"project": "p"})
            store.get_docs({"project": "p"})
            store.query(vec, k=5, where={"project": "p"})
            reads += 1
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
            break
    t.join()
    assert not errors, errors
    assert reads > 0


def test_lexical_cache_survives_invalidation_mid_read(tmp_path):
    """Same race one layer up: a writer thread invalidating the BM25 corpus while
    a reader builds/reads it."""
    from crib.retrieve import LexicalCache

    store = InMemoryStore()
    vec = [1.0] + [0.0] * 7
    store.upsert([Record(f"c{i}", vec, f"doc {i}", {"project": "p"})
                  for i in range(200)])
    cache = LexicalCache(store)
    errors: list[BaseException] = []
    stop = threading.Event()

    def invalidator() -> None:
        try:
            for _ in range(500):
                cache.invalidate("p")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            stop.set()

    t = threading.Thread(target=invalidator)
    t.start()
    while not stop.is_set():
        try:
            ids, _docs, bm25 = cache.get("p")
            assert len(ids) == len(bm25.tf)   # entries are consistent snapshots
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
            break
    t.join()
    assert not errors, errors
