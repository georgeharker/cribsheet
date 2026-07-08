"""Per-connection session state (DESIGN §15).

One warm crib daemon serves many connections; each chat session keeps its own
*current project* so calls don't have to re-pass cwd every time. The pattern is
svg-mcp's: a `WeakKeyDictionary` keyed by the MCP `ServerSession` object — MCP
exposes no session-close hook, so we lean on GC: when the connection ends and the
session object is collected, its entry is released automatically.

The state is MCP-only; the in-process CLI/tests have no session and fall back to
a shared default (so `resolve_session_project` degrades to plain cwd seeding).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from weakref import WeakKeyDictionary


@dataclass(frozen=True)
class ProjectResolution:
    """How a call's project was decided — the project plus the *policy branch* that
    produced it, so a tool can echo a resolution that would otherwise be silent.

      • ``explicit`` — the caller passed ``project=`` (a deliberate one-off).
      • ``path``     — seeded from a caller-supplied ``project_path``'s ``.crib``.
      • ``session``  — the sticky per-connection current project (set earlier).
      • ``seed``     — seeded with no path (cwd-less / the bare default).

    ``path``/``explicit`` are caller-directed; ``session``/``seed`` are *implicit*
    — the two that silently answer with the wrong project when a connection's
    session state is shared or stale (see DESIGN §15). ``implicit`` flags them so
    the code tools surface the resolution only when it's worth surfacing."""
    project: str
    via: str

    @property
    def implicit(self) -> bool:
        return self.via in ("session", "seed")

    def echo(self) -> dict[str, str]:
        return {"project": self.project, "resolved_via": self.via}


class SessionState:
    """Per-connection scope. Just the current project for now."""

    def __init__(self) -> None:
        self.current_project: str | None = None


_SESSIONS: "WeakKeyDictionary[Any, SessionState]" = WeakKeyDictionary()
_DEFAULT = SessionState()   # non-request contexts: in-process CLI, tests


def session_state() -> SessionState:
    """The SessionState for the calling MCP connection, created on first use.
    Returns the shared default when there's no active MCP context."""
    try:
        from fastmcp.server.dependencies import get_context
        session = get_context().session
    except Exception:  # noqa: BLE001 — no request context (CLI/tests)
        return _DEFAULT
    st = _SESSIONS.get(session)
    if st is None:
        st = SessionState()
        _SESSIONS[session] = st
    return st


def resolve_session_project(state: SessionState, project_arg: str | None,
                            cwd: Any, seed: Callable[[Any], str],
                            default: str | None = None) -> ProjectResolution:
    """Pick the project for a call (DESIGN §15 precedence), reporting the branch
    (`ProjectResolution.via`) so a caller can echo how it resolved:

      1. explicit `project_arg`  — one-off override; does NOT change the session
      2. the session's current project — sticky once set to a REAL project
      3. seed lazily from cwd/.crib (`seed(cwd)`) and stick it to the session

    The seed sticks — EXCEPT the bare `default`. A stray early call with no cwd
    (e.g. a notes `lookup`) would otherwise seed the session to `default` and lock
    it there forever, so a later call carrying a cwd/.crib could never point the
    code tools at the right project. So while the session is still only on `default`
    and a cwd is now offered, we re-seed to let that cwd UPGRADE it.
    """
    if project_arg:
        return ProjectResolution(project_arg, "explicit")
    if state.current_project is None or (
            state.current_project == default and cwd is not None):
        state.current_project = seed(cwd)
        # a seed with a path in hand is caller-directed (`path`); cwd-less is `seed`
        return ProjectResolution(state.current_project,
                                 "path" if cwd is not None else "seed")
    return ProjectResolution(state.current_project, "session")
