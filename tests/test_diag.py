"""Daemon diagnostics (crib.diag) and the file-event probe (crib.watch):
startup milestones, background work in `status`, the daemon log, and the
probe's states against a stub delivery (prompt, late, silent)."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from crib.app import Crib
from crib.config import Config
from crib.diag import Diagnostics, _Tee
from crib.paths import Paths
from crib.store import InMemoryStore
from crib.watch import Watcher

_REAL_WRITE = Path.write_text  # before any test patches it


def test_milestones_and_jobs_are_reported(capsys):
    d = Diagnostics()
    d.milestone("store ready", store="X")
    with d.track("reconcile", "3 projects"):
        snap = d.snapshot()
        assert [j["job"] for j in snap["background"]] == ["reconcile"]
        assert snap["background"][0]["detail"] == "3 projects"
    assert d.snapshot()["background"] == []  # done: gone
    assert d.snapshot()["startup"][0]["step"] == "store ready"
    assert "store ready store=X" in capsys.readouterr().err


def test_the_log_tees_stderr_and_rolls(tmp_path):
    path = tmp_path / "daemon.log"
    path.write_text("x" * 200)  # past the cap
    sink = open(tmp_path / "terminal", "w")
    tee = _Tee(sink, path, max_bytes=100)
    tee.write("[crib] hello\n")
    tee.flush()
    assert (tmp_path / "daemon.log.1").read_text() == "x" * 200  # rolled
    line = path.read_text()
    assert line.endswith("[crib] hello\n") and line[:4].isdigit()  # timestamped
    assert (tmp_path / "terminal").read_text() == "[crib] hello\n"  # untouched


def _watcher(tmp_path, deliver_after: float | None):
    """A Watcher whose probe 'event' arrives after `deliver_after` seconds, or
    never (a stub for the platform's delivery)."""

    async def on_change(*_a):
        return None

    loop = asyncio.get_running_loop()
    w = Watcher(tmp_path, on_change, loop)
    real = _REAL_WRITE

    def write(self, *a, **k):
        n = real(self, *a, **k)
        if self.name == ".crib-watch-probe" and deliver_after is not None:
            loop.call_later(
                deliver_after, w._probe_arrived, time.monotonic() + deliver_after
            )
        return n

    return w, write


def test_probe_states(tmp_path, monkeypatch, capsys):
    async def scenario(after, timeout):
        w, write = _watcher(tmp_path, after)
        monkeypatch.setattr(Path, "write_text", write)
        state = await w.probe_once(timeout)
        return state, w.health()

    state, h = asyncio.run(scenario(0.05, 1.0))
    assert state == "healthy" and h["last_latency_s"] < 1
    state, h = asyncio.run(scenario(None, 0.3))
    assert state == "not delivering"
    from crib import watch

    monkeypatch.setattr(watch, "PROBE_HEALTHY_S", 0.1)
    state, h = asyncio.run(scenario(0.3, 1.0))
    assert state == "delayed" and h["last_latency_s"] >= 0.3
    err = capsys.readouterr().err
    assert "file watching: not delivering" in err and "catch-up sweep" in err


def test_degraded_delivery_runs_the_catch_up(tmp_path, monkeypatch):
    swept: list[float] = []

    async def scenario():
        w, write = _watcher(tmp_path, None)  # silent platform
        monkeypatch.setattr(Path, "write_text", write)
        from crib import watch

        monkeypatch.setattr(watch, "PROBE_TIMEOUT_S", 0.1)

        async def catch_up():
            swept.append(time.monotonic())

        w.start_probe(interval=0.2, catchup_interval=0.0, on_degraded=catch_up)
        await asyncio.sleep(0.6)
        w.stop()

    asyncio.run(scenario())
    assert len(swept) >= 2


def test_status_reports_the_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def work():
            started.set()
            await release.wait()

        crib._spawn_bg(asyncio.get_running_loop(), work(), "a long job")
        await started.wait()
        running = crib.status()["daemon"]
        release.set()
        await asyncio.sleep(0.01)
        return running, crib.status()["daemon"]

    running, after = asyncio.run(scenario())
    assert "a long job" in [j["job"] for j in running["background"]]
    assert "a long job" not in [j["job"] for j in after["background"]]
    assert "uptime_s" in after and "queues" in after
    assert sys.stderr is not None


def test_the_probe_file_is_never_committed(tmp_path):
    """The probe lives in crib's data repo (under the watched projects dir) and
    is rewritten every minute; an existing repo's .gitignore gains it in place."""
    from crib.gitbacking import _GITIGNORE, GitBacking
    from crib.watch import PROBE_NAME

    assert PROBE_NAME in _GITIGNORE.splitlines()
    (tmp_path / ".gitignore").write_text("memory-bindings.json\n.trash/\n")
    g = GitBacking(tmp_path, allow_network=False)
    g._run("init")
    g._ensure_gitignore()
    g._ensure_gitignore()  # idempotent
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines.count(PROBE_NAME) == 1 and lines.count(".trash/") == 1


def test_plain_status_prints_the_background_work(capsys):
    from crib.cli import _emit_daemon

    _emit_daemon({
        "daemon": {"uptime_s": 3725, "log_file": "/x/daemon.log",
                   "background": [{"job": "reconcile", "running_s": 42.0}],
                   "queues": {"describe_files": 3, "describe_symbols": 0},
                   "watchers": {"notes": {"state": "delayed", "last_latency_s": 41.2}}},
        "generation": {"mode": "explicit", "pending_descriptions": {"un-bien": 120}}})
    out = capsys.readouterr().out
    assert "daemon     up 1h02m  log /x/daemon.log" in out
    assert "running    reconcile for 42s" in out
    assert "queued     describe files 3" in out and "describe symbols" not in out
    assert "watching   delayed (41.2s)" in out
    assert "generate   explicit: descriptions waiting in un-bien 120" in out
