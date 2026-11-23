"""Which projects this crib instance may WRITE (per-project read-only overrides).

An instance can be told that only some projects are writable: `writable = [...]`
in config.toml, `$CRIB_WRITABLE` (comma-separated, wins over the file), or the
global `--writable` CLI flag. Every other project is READ-ONLY for that
instance: reads are untouched, writes are refused with the reason. Unset means
every project is writable (the default, and every instance before this).

This is how an agent can be handed several projects (an overlay copy and the
projects it needs for context) with writing confined to the copy. It is enforced
by crib, one check at each write funnel: NoteStore's write/delete/move (notes,
declared facets, decisions, plans, learnings), and the entry of every verb that
writes outside it (code indexing, imports, project lifecycle). It composes with
read-only LOCATIONS (a mapped facet): a write must pass both. Background upkeep
of derived caches (reconcile, the watchers, enrichment) is not a project write
and is not gated. Design: "Transient projects, out-of-store homes, and code
overlays", part 5.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from .errors import CribUserError

WRITABLE_ENV = "CRIB_WRITABLE"


class ReadOnlyProject(CribUserError):
    """A write to a project this instance holds read-only."""


class WriteAccess:
    """The writable-project allowlist: None allows every project."""

    def __init__(self, writable: Iterable[str] | None = None) -> None:
        self.writable = frozenset(writable) if writable is not None else None

    def allows(self, project: str | None) -> bool:
        return self.writable is None or (project or "") in self.writable

    def require(self, project: str | None, what: str = "write") -> None:
        """Refuse `what` on `project` unless this instance may write it."""
        if self.writable is None or (project or "") in self.writable:
            return
        named = ", ".join(sorted(self.writable)) or "none"
        raise ReadOnlyProject(
            f"{what} refused: project {project!r} is read-only for this crib "
            f"instance (writable: {named}; set by `writable` in config.toml, "
            f"${WRITABLE_ENV} or --writable). Reads still work."
        )


def writable_from(configured: list[str] | None) -> list[str] | None:
    """The effective allowlist: `$CRIB_WRITABLE` when set (comma-separated; an
    empty value means NO project is writable), else the config's."""
    raw = os.environ.get(WRITABLE_ENV)
    if raw is None:
        return configured
    return [p.strip() for p in raw.split(",") if p.strip()]
