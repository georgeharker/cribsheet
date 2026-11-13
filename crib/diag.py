"""Daemon diagnostics: startup milestones, background work, the daemon log.

One process-wide `DIAG`:

- `milestone(name, **detail)` timestamps a startup step from process start
  and prints it to stderr, so "slow, or empty?" is answerable from evidence;
- `job(name, detail)` / `done(token)` (or `track`) keep the background work
  that is running right now, which `status` lists;
- `install_log(path, max_bytes)` tees stderr to a size-capped file, so a
  daemon started by sharedserver (which keeps no log of its own) leaves one.
"""

from __future__ import annotations

import contextlib
import itertools
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

_T0 = time.monotonic()


class Diagnostics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._milestones: list[dict[str, Any]] = []
        self._jobs: dict[int, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self.log_path: Path | None = None

    def milestone(self, name: str, **detail: Any) -> None:
        t = round(time.monotonic() - _T0, 3)
        with self._lock:
            self._milestones.append({"at_s": t, "step": name, **detail})
        extra = " ".join(f"{k}={v}" for k, v in detail.items())
        print(
            f"[crib] +{t:.2f}s {name}{' ' + extra if extra else ''}",
            file=sys.stderr,
            flush=True,
        )

    def job(self, name: str, detail: str = "") -> int:
        token = next(self._ids)
        with self._lock:
            self._jobs[token] = {
                "job": name,
                "detail": detail,
                "started": time.monotonic(),
            }
        return token

    def update(self, token: int, detail: str) -> None:
        with self._lock:
            if token in self._jobs:
                self._jobs[token]["detail"] = detail

    def done(self, token: int) -> None:
        with self._lock:
            self._jobs.pop(token, None)

    @contextlib.contextmanager
    def track(self, name: str, detail: str = "") -> Iterator[int]:
        token = self.job(name, detail)
        try:
            yield token
        finally:
            self.done(token)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            jobs = [
                {
                    "job": j["job"],
                    "running_s": round(now - j["started"], 1),
                    **({"detail": j["detail"]} if j["detail"] else {}),
                }
                for j in self._jobs.values()
            ]
            steps = list(self._milestones)
        out: dict[str, Any] = {
            "uptime_s": round(now - _T0, 1),
            "startup": steps,
            "background": jobs,
        }
        if self.log_path is not None:
            out["log_file"] = str(self.log_path)
        return out


DIAG = Diagnostics()


class _Tee:
    """stderr, also written to a log file (line-buffered, size-capped: past
    `max_bytes` the file rolls to `<name>.1`, keeping one previous log)."""

    def __init__(self, stream: TextIO, path: Path, max_bytes: int) -> None:
        self._stream = stream
        self._path = path
        self._max = max_bytes
        self._lock = threading.Lock()
        self._file = self._open()

    def _open(self) -> TextIO:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            if self._path.stat().st_size > self._max:
                self._path.replace(self._path.with_name(self._path.name + ".1"))
        return self._path.open("a", buffering=1, encoding="utf-8")

    def write(self, s: str) -> int:
        n = self._stream.write(s)
        with self._lock, contextlib.suppress(OSError, ValueError):
            if s and not s.isspace():
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                s = "".join(
                    f"{stamp} {line}" if line.strip() else line
                    for line in s.splitlines(keepends=True)
                )
            self._file.write(s)
            if self._file.tell() > self._max:
                self._file.close()
                self._file = self._open()
        return n

    def flush(self) -> None:
        self._stream.flush()
        with contextlib.suppress(OSError, ValueError):
            self._file.flush()

    def __getattr__(self, name: str) -> Any:  # isatty, fileno, encoding, ...
        return getattr(self._stream, name)


def install_log(path: Path, max_bytes: int) -> None:
    """Tee this process's stderr to `path` (idempotent)."""
    if isinstance(sys.stderr, _Tee):
        return
    sys.stderr = _Tee(sys.stderr, path, max_bytes)  # type: ignore[assignment]
    DIAG.log_path = path
