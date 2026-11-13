"""A provider that never answers can't wedge crib: every generation call runs
on its own bounded pool under crib's own deadline, shows in `status` while it
runs, and a query path never generates inline."""

from __future__ import annotations

import threading
import time

import pytest

from crib import generate as gen
from crib.config import GenerateConfig
from crib.diag import DIAG


@pytest.fixture()
def hung(monkeypatch):
    release = threading.Event()

    def never_answers(*_a, **_k):
        release.wait(10)
        return "late"

    monkeypatch.setattr(gen, "_generate", never_answers)
    monkeypatch.setattr(gen, "_generate_structured", never_answers)
    monkeypatch.setattr(gen, "deadline_s", lambda cfg: 0.3)
    yield release
    release.set()


def test_a_hung_provider_costs_the_caller_only_the_deadline(hung):
    t0 = time.monotonic()
    with pytest.raises(gen.GenerationError, match="no reply"):
        gen.generate_structured(
            GenerateConfig(), "s", "u", {"type": "object"}, purpose="elaborate"
        )
    assert time.monotonic() - t0 < 2.0


def test_a_stuck_call_is_listed_while_it_hangs(hung):
    with pytest.raises(gen.GenerationError):
        gen.generate(GenerateConfig(), "s", "u", purpose="distill")
    jobs = [j["job"] for j in DIAG.snapshot()["background"]]
    assert "generation (distill)" in jobs  # still visible, still stuck
    hung.set()
    time.sleep(0.2)
    assert "generation (distill)" not in [
        j["job"] for j in DIAG.snapshot()["background"]
    ]


def test_other_threads_are_not_blocked_behind_stuck_generation(hung):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(4) as callers:
        for _ in range(4):
            callers.submit(
                lambda: (
                    pytest.raises(gen.GenerationError).__enter__()
                    or gen.generate(GenerateConfig(), "s", "u")
                )
            )
        # the default pool (where tool calls and store work run) stays free
        import asyncio

        async def other_work():
            return await asyncio.to_thread(lambda: "done")

        assert asyncio.run(other_work()) == "done"


def test_a_query_path_defers_description(tmp_path, monkeypatch):
    from crib.app import Crib
    from crib.config import Config
    from crib.paths import Paths
    from crib.store import InMemoryStore

    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    cfg = Config()
    cfg.generate.mode = "on"
    crib = Crib(Paths.resolve().ensure(), cfg, InMemoryStore())
    seen: dict = {}

    def capture(proj, reindex):
        seen["mode"] = reindex.keywords.get("describe_mode")

    monkeypatch.setattr(crib.code, "revalidate", capture)
    crib._revalidate("p")
    assert seen["mode"] == "defer"
