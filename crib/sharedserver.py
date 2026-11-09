"""Thin wrapper over the `sharedserver` CLI (DESIGN §10.1).

In shared mode crib does not own the store's server process (`chroma run`, or the
crib DuckDB server) — it refcounts it. `use` attaches (starting the server if
needed); `unuse` detaches. The grace
period keeps Chroma warm between crib invocations; dead-client detection reaps
the refcount if a crib process crashes.
"""

from __future__ import annotations

import shutil
import subprocess

from . import proc
import time


class SharedServerError(RuntimeError):
    pass


def available() -> bool:
    return shutil.which("sharedserver") is not None


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """The sharedserver CLI, started without fork() (see `crib.proc`)."""
    return proc.run(args, capture_output=True, text=True)


def use(name: str, command: list[str], grace_period: str | None = None) -> None:
    """Attach to (and start if needed) a managed server, incrementing refcount."""
    if not available():
        raise SharedServerError(
            "sharedserver binary not found on PATH; install it or set "
            "[chroma].mode = 'embedded'/'json'"
        )
    args = ["sharedserver", "use", name]
    if grace_period:
        args += ["--grace-period", grace_period]
    args += ["--", *command]
    r = _run(args)
    if r.returncode != 0:
        raise SharedServerError(f"sharedserver use failed: {r.stderr.strip()}")


def unuse(name: str) -> None:
    if not available():
        return
    _run(["sharedserver", "unuse", name])


def wait_ready(probe, timeout: float = 30.0, interval: float = 0.3) -> None:
    """Poll `probe()` (e.g. chroma heartbeat) until it succeeds or times out."""
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            probe()
            return
        except Exception as e:  # noqa: BLE001 — any connection error means not-ready
            last = e
            time.sleep(interval)
    raise SharedServerError(f"server did not become ready in {timeout}s: {last}")
