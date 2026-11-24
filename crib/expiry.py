"""Expiry times for transient projects and bundles: parsed once, stored as
ISO-8601 UTC, compared strictly."""

from __future__ import annotations

import datetime

from .errors import CribUserError

_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse(spec: str) -> str:
    """An expiry as an ISO-8601 UTC time: an ISO time is taken as given (UTC if
    it names no zone), a duration (`90s`, `30m`, `2h`, `1d`) counts from now."""
    spec = spec.strip()
    try:
        if spec and spec[-1] in _UNIT and spec[:-1].isdigit():
            at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
                seconds=int(spec[:-1]) * _UNIT[spec[-1]]
            )
        else:
            at = datetime.datetime.fromisoformat(spec)
            if at.tzinfo is None:
                at = at.replace(tzinfo=datetime.UTC)
        return at.astimezone(datetime.UTC).isoformat(timespec="seconds")
    except (ValueError, OverflowError) as e:
        raise CribUserError(
            f"expires: {spec!r} is neither an ISO time nor a duration like "
            "30m / 2h / 1d (within range)"
        ) from e


def when(value: object) -> datetime.datetime | None:
    """A stored expiry back as an aware datetime, or None when it isn't one."""
    if not isinstance(value, str):
        return None
    try:
        at = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=datetime.UTC)


def due(value: object, now: datetime.datetime | None = None) -> bool:
    at = when(value)
    return at is not None and at <= (now or datetime.datetime.now(datetime.UTC))


class ExpiryTable:
    """The scheduled-delete table for a store without one (an in-memory store):
    the same four methods as the DuckDB store's `crib_expiry`, over a dict."""

    def __init__(self) -> None:
        self._at: dict[str, float] = {}

    def expiry_set(self, project: str, at: float | None) -> None:
        if at is None:
            self._at.pop(project, None)
        else:
            self._at[project] = float(at)

    def expiry_replace(self, rows: dict[str, float]) -> None:
        self._at = {p: float(a) for p, a in rows.items()}

    def expiry_next(self) -> tuple[str, float] | None:
        if not self._at:
            return None
        p = min(self._at, key=lambda k: (self._at[k], k))
        return p, self._at[p]

    def expiry_due(self, now: float) -> list[str]:
        return sorted((p for p, a in self._at.items() if a <= now),
                      key=lambda k: (self._at[k], k))


def table_for(store: object) -> object:
    """The store itself when it keeps the expiry table, else an ExpiryTable."""
    methods = ("expiry_set", "expiry_replace", "expiry_next", "expiry_due")
    if all(callable(getattr(store, m, None)) for m in methods):
        return store
    return ExpiryTable()
