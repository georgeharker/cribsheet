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
      • ``elicited`` — the human was ASKED (an unanchored read + a client that
        supports elicitation) and named the project; the session adopts it.

    ``path``/``explicit`` are caller-directed; ``session``/``seed`` are *implicit*
    — the two that silently answer with the wrong project when a connection's
    session state is shared or stale (see DESIGN §15). ``implicit`` flags them so
    the code tools surface the resolution only when it's worth surfacing.

    ``session_set`` marks the call that ADOPTED its project as the session's — the
    first path-bearing call of a session. That decides where every later
    selector-less call lands, including writes, so it is always reported even
    though ``path`` is caller-directed and otherwise quiet."""

    project: str
    via: str
    session_set: bool = False
    # NOTHING pointed here: no selector, no path, no session — the resolution is a
    # pure fallthrough to the bare default. Distinct from `implicit` (a sticky
    # session at least records something the caller once chose) because it is the
    # one branch where the answer is a GUESS, and a guess must never be quiet: after
    # a daemon restart every chat lands exactly here, believing it still has the
    # project it adopted before the restart.
    unanchored: bool = False
    # A caller-supplied `project_path` that no `.crib` anchors: resolution fell
    # through to the bare default even though the caller POINTED at a repo. The
    # complement of `unanchored` in the fallthrough (a path was given vs none was),
    # and nearly always a missing `.crib` rather than intent — so it is surfaced on
    # every read, not just the echo tools. On the MCP face an agent's `project_path`
    # is always deliberate; the CLI (which auto-fills cwd) owns this advisory itself.
    path_unmatched: bool = False

    @property
    def unlinked_message(self) -> str:
        return (
            f"UNLINKED PATH: the project_path you gave is not linked to a crib "
            f"project (no `.crib` there or in a parent) — answered from "
            f"{self.project!r}. If you meant a specific project, pass project=<name>, "
            f"or add a `.crib` (`project: <name>`) at the repo root / run "
            f"project_setup."
        )

    @property
    def implicit(self) -> bool:
        return self.via in ("session", "seed")

    @property
    def worth_echoing(self) -> bool:
        """Either the caller did not choose this project, or the call just made it
        the session's — both are things the caller has not been told."""
        return self.implicit or self.session_set

    def echo(self) -> dict[str, Any]:
        out: dict[str, Any] = {"project": self.project, "resolved_via": self.via}
        if self.unanchored:
            out["warning"] = (
                f"UNANCHORED: nothing names a project for this session — answered "
                f"from {self.project!r}. If that is not where you meant to look, "
                f"pass project= or project_path= once to anchor the session "
                f"(sessions reset when the daemon restarts)."
            )
        if self.session_set:
            out["session_project_set"] = self.project
            out["note"] = (
                f"this call also made {self.project!r} the session's "
                f"current project — later calls that name no project "
                f"will use it. `use_project` to change it."
            )
        return out


class SessionState:
    """Per-connection scope. Just the current project for now."""

    def __init__(self) -> None:
        self.current_project: str | None = None


_SESSIONS: "WeakKeyDictionary[Any, SessionState]" = WeakKeyDictionary()
_DEFAULT = SessionState()  # non-request contexts: in-process CLI, tests


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


def resolve_session_project(
    state: SessionState,
    project_arg: str | None,
    cwd: Any,
    seed: Callable[[Any], str],
    default: str | None = None,
) -> ProjectResolution:
    """Pick the project for a call (DESIGN §15 precedence), reporting the branch
    (`ProjectResolution.via`) so a caller can echo how it resolved:

      1. an EXPLICIT selector — `project_arg`, or a `cwd` whose `.crib` decides a
         project. Both name a target outright, so neither loses to stickiness, and
         `project_path` is if anything the more specific of the two ("this exact
         repo"): it must not be the one that yields.
      2. the session's current project
      3. seed from cwd/.crib, or the bare `default`

    ADOPTION happens once. The FIRST path-bearing call of a session adopts its
    project as the session's, so the common pattern — name the repo once, then call
    without it — keeps working. After that the session is set and rule 1 outranks
    it, so a later call naming a different repo answers for that repo and changes
    nothing: one cross-project read can never re-home the writes that follow it.

    A call that adopts says so (`session_set`), because it decides where every
    later selector-less call lands. A bare `default` never adopts — nothing sticks
    that the caller did not point at, so a stray early call cannot lock the session
    somewhere it has to be argued back out of.
    """
    if project_arg:
        return ProjectResolution(project_arg, "explicit")
    if cwd is not None:
        picked = seed(cwd)
        # ONLY when the path decided something. With no `.crib` under it the seed
        # falls through to the bare `default`, which is not what the caller asked
        # for: returning `path` there would mute the wrong-project echo built for
        # exactly that case (a lookup answering from `default` while the agent
        # believes it named a repo).
        if picked != default:
            first = state.current_project is None
            if first:
                state.current_project = picked
            return ProjectResolution(picked, "path", session_set=first)
    if state.current_project is not None:
        return ProjectResolution(state.current_project, "session")
    # The fallthrough. Two different situations end here and they are NOT the same:
    #   • a cwd was given but no `.crib` decides — landing on `default` is that
    #     path's DOCUMENTED meaning (the CLI standing in ~ does this deliberately);
    #   • NO cwd at all (only MCP can get here: a selector-less call on a fresh
    #     session — which is every chat right after a daemon restart). Then crib
    #     knows NOTHING; its own startup directory is meaningless to every chat,
    #     so any answer is a guess. `unanchored` marks it, and the server layer
    #     ASKS (elicitation) or REFUSES actionably rather than answering.
    return ProjectResolution(
        seed(cwd), "seed", unanchored=cwd is None, path_unmatched=cwd is not None
    )
