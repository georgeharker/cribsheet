"""The generation ledger: remember what an LLM call already failed on, so crib
never pays for the same failing input twice in a row.

Background generation (the startup describe/enrichment backlogs) used to retry
everything still missing on EVERY daemon start. When a provider kept failing,
the same inputs were paid for at every start and nothing was kept
(2026-11-09: ~16,000 code symbols). The ledger records each failed attempt
against its INPUT (a content hash) and its PROVIDER (adapter, model,
endpoint), and backs off:

    1 h, 4 h, 16 h, ~2.7 d, then every 7 d

A changed input (the code or section was edited) or a changed provider (the
config now points somewhere else) retries at once. That is how a fixed config
or a fixed bug clears the backlog. A success forgets the row.

Only BACKGROUND work consults the ledger. An explicit request (project_index,
`crib elaborate`) always runs, and its outcome is still recorded.

Storage: on the DuckDB backends the rows live in the store's own database, so
they persist across restarts and go through the Quack server in shared mode.
Other backends keep them in memory for the life of the process.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

BACKOFF_BASE_S = 3600.0  # first retry after an hour
BACKOFF_FACTOR = 4.0
BACKOFF_CAP_S = 7 * 24 * 3600.0
_ERROR_CHARS = 300  # enough to diagnose, small enough to keep every row


def backoff_s(attempts: int) -> float:
    """Seconds to wait after the `attempts`-th consecutive failure (1-based)."""
    return min(BACKOFF_CAP_S, BACKOFF_BASE_S * BACKOFF_FACTOR ** max(0, attempts - 1))


def provider_id(gen_cfg: Any, purpose: str) -> str:
    """The identity of the provider that would serve `purpose`: a changed model,
    adapter or endpoint means earlier failures say nothing about it."""
    from .generate import resolve_provider

    try:
        p = resolve_provider(gen_cfg, purpose)
    except Exception as e:  # noqa: BLE001 — unresolvable is its own identity
        return f"unresolved:{type(e).__name__}"
    return f"{p.adapter}:{p.model}@{p.endpoint or ''}"


@dataclass
class Entry:
    purpose: str
    target: str
    input_hash: str
    provider: str
    attempts: int
    last_error: str
    last_attempt: float
    next_retry: float


class LedgerStorage(Protocol):
    """Row storage the ledger needs; the DuckDB stores implement it natively."""

    def ledger_rows(self, purpose: str, targets: list[str]) -> dict[str, Entry]: ...
    def ledger_put(self, entries: list[Entry]) -> None: ...
    def ledger_delete(self, purpose: str, targets: list[str]) -> None: ...
    def ledger_counts(self, now: float) -> dict[str, dict[str, int]]: ...


class MemoryLedgerStorage:
    """Process-lifetime rows, for stores without a database (memory, json)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], Entry] = {}
        self._lock = threading.Lock()

    def ledger_rows(self, purpose: str, targets: list[str]) -> dict[str, Entry]:
        with self._lock:
            return {t: e for t in targets if (e := self._rows.get((purpose, t)))}

    def ledger_put(self, entries: list[Entry]) -> None:
        with self._lock:
            for e in entries:
                self._rows[(e.purpose, e.target)] = e

    def ledger_delete(self, purpose: str, targets: list[str]) -> None:
        with self._lock:
            for t in targets:
                self._rows.pop((purpose, t), None)

    def ledger_counts(self, now: float) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        with self._lock:
            for e in self._rows.values():
                c = out.setdefault(e.purpose, {"failing": 0, "backing_off": 0})
                c["failing"] += 1
                c["backing_off"] += e.next_retry > now
        return out


class GenLedger:
    """The policy over a LedgerStorage. Thread-safe as far as its storage is."""

    def __init__(self, storage: LedgerStorage, clock: Any = time.time) -> None:
        self._s = storage
        self._now = clock

    def due(
        self, purpose: str, items: dict[str, str], provider: str
    ) -> tuple[list[str], dict[str, Entry]]:
        """Split `items` ({target: input_hash}) into those worth attempting now and
        those still backing off (target -> its ledger entry). A target whose input
        or provider changed since it failed is due again."""
        rows = self._s.ledger_rows(purpose, list(items))
        now = self._now()
        ready: list[str] = []
        waiting: dict[str, Entry] = {}
        for target, h in items.items():
            e = rows.get(target)
            if (
                e is None
                or e.input_hash != h
                or e.provider != provider
                or now >= e.next_retry
            ):
                ready.append(target)
            else:
                waiting[target] = e
        return ready, waiting

    def failed(
        self, purpose: str, items: dict[str, str], provider: str, error: str
    ) -> None:
        """Record one failed attempt for each target ({target: input_hash})."""
        if not items:
            return
        rows = self._s.ledger_rows(purpose, list(items))
        now = self._now()
        out = []
        for target, h in items.items():
            prev = rows.get(target)
            same = (
                prev is not None and prev.input_hash == h and prev.provider == provider
            )
            attempts = (prev.attempts + 1) if (prev is not None and same) else 1
            out.append(
                Entry(
                    purpose,
                    target,
                    h,
                    provider,
                    attempts,
                    error[:_ERROR_CHARS],
                    now,
                    now + backoff_s(attempts),
                )
            )
        self._s.ledger_put(out)

    def succeeded(self, purpose: str, targets: list[str]) -> None:
        if targets:
            self._s.ledger_delete(purpose, list(targets))

    def counts(self) -> dict[str, dict[str, int]]:
        """{purpose: {failing, backing_off}}, for status. "Backing off" is judged
        by the ledger's own clock, the same one `due` uses."""
        return self._s.ledger_counts(self._now())


def for_store(store: Any) -> GenLedger:
    """The ledger for `store`: its own table when it has one, else memory."""
    has = all(
        callable(getattr(store, m, None))
        for m in ("ledger_rows", "ledger_put", "ledger_delete", "ledger_counts")
    )
    return GenLedger(store if has else MemoryLedgerStorage())
