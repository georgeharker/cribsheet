"""How a call's project is decided: per call, from what the call names.

There is no per-connection state. A read names its project (`project=`), or a
path in that repo (`project_path=`), on the call itself; with neither, the
server ASKS the human (elicitation) or refuses — it never answers from a guess
and never remembers an answer for later calls. Writes always had to name their
target. Design: "Retire use_project and implicit anchoring (all eras)", which
replaced the sticky per-connection current project (`project_use`, adoption by
the first path-bearing call).

The protocol era still matters for HOW the server asks: the handshake era asks
with a server-initiated request (`ctx.elicit`); the mcp 2026-07-28 stateless
era asks by returning an `InputRequiredResult` and reading the answer on the
client's retry (crib/server.py `_ask`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
import re


@dataclass(frozen=True)
class ProjectResolution:
    """How a call's project was decided — the project plus the *policy branch* that
    produced it, so a tool can echo a resolution that would otherwise be silent.

      • ``explicit`` — the caller passed ``project=``.
      • ``path``     — decided by a caller-supplied ``project_path``'s ``.crib``.
      • ``seed``     — the fallthrough: a path no ``.crib`` decides (its
        documented meaning is the bare default), or nothing at all.
      • ``elicited`` — the human was ASKED (no selector, a client that can be
        asked) and named the project, for this call only.

    ``explicit``/``path``/``elicited`` are caller-directed; ``seed`` is
    *implicit*, the one that can silently answer with the wrong project, so
    ``implicit`` flags it and the code tools surface it."""

    project: str
    via: str
    # NOTHING pointed here: no selector, no path. The one branch where the
    # answer would be a GUESS, so the server asks or refuses instead.
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
        return self.via == "seed"

    @property
    def worth_echoing(self) -> bool:
        """The caller did not choose this project: something it hasn't been told."""
        return self.implicit

    def echo(self) -> dict[str, Any]:
        out: dict[str, Any] = {"project": self.project, "resolved_via": self.via}
        if self.unanchored:
            out["warning"] = (
                f"UNANCHORED: this call named no project — answered from "
                f"{self.project!r}. Pass project=<name> or project_path=<a path in "
                f"the repo> on every call."
            )
        return out


# ── Protocol era: decides HOW the server asks, nothing else ──────────────────

ERA_HANDSHAKE = "handshake"  # asks with a server-initiated request (ctx.elicit)
ERA_STATELESS = "stateless"  # mcp 2026-07-28: asks by returning InputRequiredResult

# mcp 2 serves both eras off one app, routed per request by this header (see
# crib/server.py `_pin_legacy_era` and crib/client.py's `mode="legacy"` pin).
_PROTOCOL_HEADER = "mcp-protocol-version"
_STATELESS_PROTOCOL = "2026-07-28"

NAME_THE_PROJECT = (
    "pass project=<name> or project_path=<a path in that repo> on every call "
    "(nothing carries over between calls)"
)


def _era_of_version(version: str | None) -> str:
    """Classify one `mcp-protocol-version` header value. ONLY a well-formed
    ISO-date version at-or-after 2026-07-28 is the stateless era; absent,
    malformed, or older values are the handshake era, and every non-HTTP face
    (stdio, in-process CLI, in-memory transport) hits this branch because it
    never carries the header at all."""
    v = (version or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):  # malformed/absent → handshake
        return ERA_HANDSHAKE
    return ERA_STATELESS if v >= _STATELESS_PROTOCOL else ERA_HANDSHAKE


def request_era() -> str:
    """The protocol era of the current inbound request (see `_era_of_version`):
    "stateless" when the client speaks mcp 2026-07-28, else "handshake" (also
    whenever there is no HTTP request: stdio, the in-process CLI, the in-memory
    test transport)."""
    try:
        from fastmcp.server.dependencies import get_http_headers

        return _era_of_version(get_http_headers().get(_PROTOCOL_HEADER))
    except Exception:  # noqa: BLE001 — no HTTP request (stdio/CLI/in-memory)
        return ERA_HANDSHAKE


def resolve_call_project(
    project_arg: str | None,
    cwd: Any,
    seed: Callable[[Any], str],
    default: str | None = None,
) -> ProjectResolution:
    """Pick a read's project from what THIS call names, reporting the branch
    (`ProjectResolution.via`) so a caller can echo how it resolved:

      1. `project_arg` — named outright;
      2. a `cwd` whose `.crib` decides a project;
      3. the fallthrough: a `cwd` no `.crib` decides lands on the bare default
         (its documented meaning; the CLI standing in ~ does this on purpose,
         and an MCP caller gets the UNLINKED advisory), and NO cwd at all is
         `unanchored` — crib knows nothing, so the server asks or refuses.

    Nothing is remembered between calls."""
    if project_arg:
        return ProjectResolution(project_arg, "explicit")
    if cwd is not None:
        picked = seed(cwd)
        # ONLY when the path decided something: with no `.crib` under it the
        # seed falls through to the bare default, which is not what the caller
        # asked for, and must stay visible as such.
        if picked != default:
            return ProjectResolution(picked, "path")
    return ProjectResolution(
        seed(cwd), "seed", unanchored=cwd is None, path_unmatched=cwd is not None
    )
