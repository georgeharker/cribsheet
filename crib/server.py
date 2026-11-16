"""FastMCP server exposing the crib tool surface (DESIGN §5).

Lazy-imports fastmcp so the package stays importable without it.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import facets as _facets
from . import verbs as _verbs

from .app import Crib
from .errors import CribUserError
from .session import (
    ERA_STATELESS,
    ProjectResolution,
    STATELESS_HINT,
    adopt_into_session,
    request_era,
    resolve_session_project,
    session_state,
)

# How often project_index emits an MCP progress notification while a sweep runs — frequent
# enough to keep the call alive (progress resets the client idle timeout) and feel live.
_PROGRESS_EVERY_S = 2.0

# Context annotates the elicitation param; needed at runtime for injection AND for the
# forward-ref eval of `ctx: Context`. The TYPE_CHECKING arm gives the checker the real
# type (so `ctx: Context | None` is a type, not a variable); the runtime arm keeps the
# package importable without fastmcp by binding the name to Any as a fallback.
if TYPE_CHECKING:
    from fastmcp import Context
else:
    try:
        from fastmcp import Context
    except Exception:  # pragma: no cover — package stays importable without fastmcp
        Context = Any


def _cwd(project_path: str | None) -> Path | None:
    """The CLI (an MCP client) passes its own working directory so the daemon
    identifies which project a call targets (its `.crib` root). Named `project_path` because for an MCP agent it is NOT a shell cwd — it is the repo you mean."""
    return Path(project_path) if project_path else None


# ── Project resolution: three policies over one ProjectResolution ──────────────
# A tool call's project can be decided three ways, differing by op class (DESIGN
# §15). All produce (or hinge on) a `ProjectResolution` carrying *how* it resolved:
#   • _resolve / _project  — READS: explicit > sticky session > seed-from-path.
#       Sticky is the ergonomic default; the `via` lets a read tool ECHO an
#       implicit (session/seed) resolution so a wrong one is visible, not silent.
#   • _source_project      — REPO-SCOPED ops: a given path's .crib decides (never
#       sticky), so indexing /other/repo never lands in the current project.
#   • _write_project(_elicit) — WRITES: must NAME the target (a durable fact belongs
#       to the project it's ABOUT), never inheriting sticky.
def _resolve(
    crib: Crib, project: str | None, project_path: str | None
) -> ProjectResolution:
    """The READ policy as a `ProjectResolution` (project + how it resolved)."""
    return resolve_session_project(
        session_state(),
        project,
        _cwd(project_path),
        lambda c: crib.resolve_project(None, c),
        default=crib.config.default_project,
    )


def _project(crib: Crib, project: str | None, project_path: str | None) -> str:
    """The resolved project name for a read (sticky-session convenience)."""
    return _resolve(crib, project, project_path).project


def _echo_dict(out: Any, res: ProjectResolution) -> Any:
    """Stamp the PROJECT resolution onto a dict result (a non-breaking extra key), so
    an agent that didn't name a project can see which one — and how — answered, and
    so a call that adopted the session's project says it did.

    `resolved_project`, not `resolved`: the symbol verbs already return `resolved`
    for what a symbol name resolved to, and one key cannot mean both."""
    if isinstance(out, dict) and res.worth_echoing:
        out.setdefault("resolved_project", res.echo())
    return out


def _echo_unlinked(out: Any, res: ProjectResolution) -> Any:
    """An explicit `project_path` that no `.crib` anchors resolved to the bare
    default. Surface it on EVERY read tool (not just the `echo=` ones) and
    non-swallowably — the caller pointed at a repo and silently got `default`,
    nearly always a missing `.crib`. This is the MCP-face counterpart of the CLI's
    `-P` advisory: an agent's `project_path` is always deliberate, so it reads the
    advisory off the result and re-calls with `project=`. Dict results carry it as a
    non-breaking key; an empty list becomes a one-item diagnostic (as `_echo_list`)."""
    if isinstance(out, dict):
        out.setdefault("unlinked_project_path", res.unlinked_message)
    elif isinstance(out, list) and not out:
        return [{"unlinked_project_path": res.unlinked_message, "matches": 0}]
    return out


def _echo_list(hits: Any, res: ProjectResolution) -> Any:
    """Surface an IMPLICIT resolution on a LIST result where it would otherwise be
    invisible: an EMPTY result from a sticky/seeded project is indistinguishable
    from 'answered the wrong project', so return one diagnostic marker instead of a
    bare `[]`. Non-empty lists already tag each hit with its owning `project`."""
    if res.implicit and isinstance(hits, list) and not hits:
        return [
            {
                "resolved_project": res.echo(),
                "matches": 0,
                "note": (
                    f"resolved implicitly to {res.project!r} via {res.via}; "
                    "0 matches. If you meant another project pass "
                    "project=<name> or project_path=<a path in that repo>."
                ),
            }
        ]
    return hits


def _source_project(
    crib: Crib, project: str | None, project_path: str | None
) -> str | None:
    """Project selector for REPO-SCOPED ops (project_setup/index/status/forget).

    Also `code_index` — a single-file index is the same op at file granularity.

    These act on a specific repo, so an explicit `project_path` must decide WHICH
    project via that repo's `.crib` — never the sticky session project (a call with
    project_path=/other/repo but no project once indexed the OTHER repo INTO the
    current one). Precedence: explicit `project` wins; else if a `project_path` is
    given, return None so `crib.project_*` reads `link.project` from that repo's
    `.crib`; else fall back to the session's current project."""
    if project:
        return project
    if project_path:
        return None  # let the repo's .crib name the project
    return _project(crib, None, None)  # neither given → sticky session project


# Documented exceptions to "a write NAMES its target" — both are declared at the
# tool (see `crib_tool`) and repeated in the tool's own docstring, so the exception
# is visible where it applies, not just here:
#   • learning_add / learning_edit / learning_forget (and reaffirm/rehome) use the
#     READ policy BY INTENT. A learning is about a SYMBOL in the code project you
#     are working in — it can't belong anywhere else — so the sticky/inferred
#     project is the right answer, and forcing `project=` on every pin would tax
#     the one write we most want to be frictionless.
#   • note_import / note_import_memory use the SOURCE policy: an import is about a
#     REPO, so the repo's `.crib` decides (never the sticky session). They also
#     declare `needs_target`, because with no source they used to fall through to
#     the config default project silently — the very thing `_write_project` forbids.
def _write_project(crib: Crib, project: str | None, project_path: str | None) -> str:
    """Project for a WRITE op (store/append/edit/forget/move). Writes must NAME their
    target — they do NOT inherit the sticky session project, because a durable fact
    belongs to the project it's ABOUT, not whatever repo you're browsing (that's how a
    shuck note once landed in `zdot`). Precedence: explicit `project`; else the `.crib`
    at `project_path`; else ERROR asking the caller to specify. Reads keep the sticky
    convenience via `_project`; only writes are forced."""
    if project:
        return project
    if project_path:
        return crib.resolve_project(None, _cwd(project_path))
    raise CribUserError(
        "a write needs an explicit target: pass project=<name> — the project this "
        "fact is ABOUT, which may differ from your current one (cross-cutting tooling "
        "knowledge often belongs in `default` or its own project) — or project_path="
        "<a path in that repo>. Writes don't inherit the sticky current project."
    )


async def _write_project_elicit(
    crib: Crib, project: str | None, project_path: str | None, ctx: Any
) -> str:
    """Like `_write_project`, but when NEITHER project nor project_path is given, ASK
    the client for the project (MCP elicitation) instead of hard-erroring — the human
    decides the fact's home. Degrades gracefully: a client that declines/cancels or
    doesn't support elicitation falls through to the `_write_project` error."""
    if project or project_path:
        return _write_project(crib, project, project_path)
    try:
        result = await ctx.elicit(
            "Which crib project should this fact be stored in? Name the project it's "
            "ABOUT — often `default` for cross-cutting tooling/convention knowledge, "
            "not the repo you're currently working in.",
            response_type=str,
        )
        chosen = getattr(result, "data", None)  # AcceptedElicitation.data
        if isinstance(chosen, str) and chosen.strip():
            return chosen.strip()
    except Exception:  # noqa: BLE001 — no elicitation support → fall back to the error
        # pi-lens-ignore: python-empty-except
        pass
    return _write_project(crib, None, None)  # raises the explicit-target error


async def _confirm_destructive(project: str, verb: str, detail: str) -> None:
    """ALWAYS elicit (MCP mode): a destructive verb runs ONLY after the client
    affirms it — the human decision is not a parameter an agent can supply, so
    it is never a signature argument. `detail` states EXACTLY what happens
    (the caller has already computed the fences/preview, so the wording can
    be truthful about force/undo rather than optimistic). The confirmation is
    typed (echo the project name) and a wrong/blank/declined answer is a
    refusal. A client that doesn't support elicitation is refused with the
    terminal path: the CLI confirms in its own interactive prompt."""
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
        result = await ctx.elicit(
            f"Confirm: {detail} Type the project name '{project}' to confirm, "
            "anything else to abort.",
            response_type=str,
        )
    except Exception as e:  # noqa: BLE001 — no elicitation ⇒ no deletion
        raise CribUserError(
            f"{verb} requires your confirmation, but this client does not "
            f"support MCP elicitation — run `crib project delete {project}` "
            "in your terminal instead"
        ) from e
    chosen = getattr(result, "data", None)
    if not (isinstance(chosen, str) and chosen.strip() == project):
        raise CribUserError(f"{verb} aborted: confirmation did not echo {project!r}")


async def _read_project_elicit(crib: Crib, res: ProjectResolution) -> ProjectResolution:
    """An UNANCHORED read — no selector, no path, no session — must not answer from
    a guess: the daemon's own startup directory is meaningless to every chat, and
    `default` merely LOOKS like an answer. So ASK (MCP elicitation) when the client
    supports it; REFUSE with the actionable error when it does not. Either way the
    caller ends up anchored on something a human named, never on where the daemon
    happened to be started. (Every chat lands here right after a daemon restart —
    the session state that made it anchored died with the old process.)"""
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
        result = await ctx.elicit(
            "No project is anchored for this session (sessions reset when the crib "
            "daemon restarts). Which project should this call use? Name one of: "
            + ", ".join(sorted(crib.projects()))
            + " — or give a path inside the "
            "repo you mean.",
            response_type=str,
        )
        chosen = getattr(result, "data", None)
        if isinstance(chosen, str) and chosen.strip():
            chosen = chosen.strip()
            if "/" in chosen or chosen.startswith("~"):
                proj = crib.resolve_project(None, Path(chosen).expanduser())
            else:
                if chosen not in crib.projects():
                    # the dir named in the refusal is the evidence that makes this
                    # failure class self-explaining (flake investigation 2026-11-02:
                    # a one-off empty list had no breadcrumbs to diagnose)
                    raise CribUserError(
                        f"no project named {chosen!r} — one of: "
                        + ", ".join(sorted(crib.projects()))
                        + f" (projects dir: {crib.paths.projects_dir}; CRIB_DATA_DIR: "
                        f"{os.environ.get('CRIB_DATA_DIR')})"
                    )
                proj = chosen
            # the human just named it — anchor (handshake era only) and say so;
            # on the stateless era the answer is for this call, quietly
            anchored = adopt_into_session(proj)
            # pi-lens-ignore: unreachable-except
            return ProjectResolution(proj, "elicited", session_set=anchored)
    except CribUserError:
        # pi-lens-ignore: python-empty-except
        raise
    except Exception:  # noqa: BLE001 — no elicitation support / declined → refuse
        pass
    raise CribUserError(
        "no project is anchored for this session and none was named"
        + (
            f" — {STATELESS_HINT}"
            if request_era() == ERA_STATELESS
            else " (sessions reset when the crib daemon restarts): pass "
            "project=<name> or project_path=<a path in the repo>, or call "
            "project_use once"
        )
        + ". Projects: "
        + ", ".join(sorted(crib.projects()))
    )


# ── The policy is DECLARED, not chosen in the body ────────────────────────────
# Which of the resolvers above a tool uses is stated once, at its registration
# (`@crib_tool("read"|"write"|"source"|"session"|"none")`); the decorator wires the
# resolver and hands the body an ALREADY-RESOLVED `project`. No tool body calls a
# resolver, because that is exactly how the policy drifts invisibly: `code_index`
# spent a release on the READ policy — one wrong helper call in one body — and
# filed another repo's symbols under the sticky session project. Declaring it makes
# the choice reviewable in the diff and checkable by a test.
#   read    — explicit > sticky session > seed-from-path (`_resolve`)
#   write   — must NAME the target (`_write_project`)
#   source  — the repo at `project_path` decides; sticky never wins (`_source_project`)
#   session — the tool IS the session pointer (project_use/project_current): it sets
#             or reports the current project rather than resolving one for an op
#   none    — no project args at all (whole-store / cross-project ops)
# `TOOL_POLICY` records every registration so a table test can assert the surface is
# fully declared (and the CLI's VERBS registry can be checked against it).
TOOL_POLICY: dict[str, str] = {}
_POLICIES = ("read", "write", "source", "session", "none")


# ── Per-deployment tool-set EXPOSURE (crib serve --tools / [tools].expose) ────
# A general-assistant deployment shows the prose facets only; a pi session gets
# everything. The mapping is the tool NAME's own family — the same grouping the
# CLI's nouns use — plus `system`: status/ops/project_reconcile (the poll/meta
# surface) always ride along, filtered or not, because polling a job needs no
# code intelligence.
# A declared facet (crib.facets) is its own set, named by its plural, after
# crib's own.
TOOL_SET_NAMES = (
    "notes",
    "designs",
    "plans",
    "code",
    "learnings",
    "projects",
    "memory",
    "system",
    *(s.plural for s in _facets.FACETS if not s.builtin),
)
_SET_FAMILIES = {
    "note": "notes",
    "design": "designs",
    "plan": "plans",
    "code": "code",
    "learning": "learnings",
    "project": "projects",
    "memory": "memory",
    **{s.noun: s.plural for s in _facets.FACETS if not s.builtin},
}
TOOL_SETS: dict[str, str] = {}  # tool name -> set name, filled as tools register
_EXPOSED: frozenset[str] | None = None  # None (the default) = every set
_RO: frozenset[str] = frozenset()  # the exposed-but-READ-ONLY sets

# ── the read-only classification, per set ─────────────────────────────────────
# `notes:ro` = the set exposes ONLY these verbs: the model never sees its
# mutating half (no refusal at call time, no unusable verbs advertised). A new
# tool must land in one of the two lists or the coverage test fails — the
# classification is data, reviewed in the diff like a policy declaration.
# Derived-index maintenance (reindex/distill/elaborate/summarize/code_index/
# code_convert) counts as WRITING: it costs generation and rewrites indexes.
READ_ONLY_TOOLS: dict[str, frozenset[str]] = {
    # every generated family's (notes', any declared facet's): its templates'
    # read-only verbs (crib.verbs)
    **{
        s.plural: _verbs.view(s).read_only_tools
        for s in _facets.FACETS
    },
    "code": frozenset({"code_lookup", "code_dossier", "code_graph", "code_xref"}),
    "learnings": frozenset({"learning_read", "learning_report"}),
    "memory": frozenset({"memory_history"}),
    "projects": frozenset(
        {"project_list", "project_status", "project_wait", "project_doctor",
         "project_related", "project_lookup"}
    ),
}


def tool_set_of(name: str) -> str:
    """The tool set a tool belongs to — the CLI noun it nests under.
    `project_reconcile` is the one prefix-straddler: it sweeps EVERY project
    (whole-store housekeeping), so it rides with the poll/meta `system` set,
    not with a namespace's management."""
    set_name = (
        "system"
        if name == "project_reconcile"
        else _SET_FAMILIES.get(name.split("_", 1)[0], "system")
    )
    TOOL_SETS[name] = set_name
    return set_name


def exposure(
    spec: str | list[str] | None,
) -> tuple[frozenset[str], frozenset[str]] | None:
    """`--tools notes:ro,plans` / `[tools] expose` → ({exposed}, {read-only});
    None (the default) = everything, nothing read-only. `system` always joins
    the exposed sets. UNKNOWN set names REFUSE here — at serve startup, naming
    the valid list — rather than starting a server quietly missing sets someone
    asked for. The suffix is `:ro` (the set's mutating verbs vanish from the
    face); an unknown suffix is a refusal too."""
    if not spec:
        return None
    asked = [
        t.strip()
        for t in (spec.split(",") if isinstance(spec, str) else spec)
        if t.strip()
    ]
    if not asked:
        return None
    sets, ro = set(), set()
    bad: list[str] = []
    for token in asked:
        name, _, mode = token.partition(":")
        if name not in TOOL_SET_NAMES:
            bad.append(token)
            continue
        sets.add(name)
        if mode == "ro":
            ro.add(name)
        elif mode:
            bad.append(token)
    if bad:
        raise ValueError(
            f"unknown tool set(s) {', '.join(bad)} — the sets are: "
            + ", ".join(TOOL_SET_NAMES)
            + "; a set may carry `:ro` (read-only); `system` always rides along"
        )
    return frozenset(sets) | {"system"}, frozenset(ro)


def _switch_if_created(result: dict) -> dict:
    """Creating a project switches the session into it — referencing an existing
    one (a one-off `project` arg) does not (DESIGN §15). The switch is the ONE
    anchor gate (`adopt_into_session`): on the stateless era nothing anchors,
    so say so instead of silently pretending."""
    if isinstance(result, dict) and result.get("created"):
        if not adopt_into_session(result.get("project")):
            result["anchored"] = False
            result["note"] = STATELESS_HINT
    return result


def build_server(crib: Crib | None = None, tools: str | list[str] | None = None):
    from fastmcp import FastMCP  # lazy
    from fastmcp.tools import FunctionTool

    global _EXPOSED
    global _EXPOSED, _RO
    exposure_result = exposure(tools)
    _EXPOSED = frozenset(exposure_result[0]) if exposure_result else None
    _RO = frozenset(exposure_result[1]) if exposure_result else frozenset()
    preamble: str = ""
    if _EXPOSED is not None:
        served = sorted(_EXPOSED - {"system"})
        ro = sorted(_EXPOSED & _RO)
        print(
            f"[crib] MCP tool sets exposed: {', '.join(served)} (+ system); "
            + (f"read-only: {', '.join(ro)}; " if ro else "")
            + f"{len(TOOL_SET_NAMES) - 1 - len(served)} set(s) not exposed: "
            + ", ".join(
                n for n in TOOL_SET_NAMES if n not in _EXPOSED and n != "system"
            )
        )
        # THE MARGINAL CONCERN, handled at the source: the instructions name
        # tools from every set, but the guidance below can never be surgically
        # filtered — so the preamble says WHAT is on this deployment's face and
        # that anything else named in the text is absent. The model reads the
        # caveat BEFORE it tries a missing tool.
        preamble = (
            "THIS DEPLOYMENT'S EXPOSURE: the tool sets on the wire are "
            + ", ".join(served)
            + " (plus the status/ops/meta tools — `system`; they poll jobs and "
            "report health). "
            + (
                "READ-ONLY DEPLOYMENT for: " + ", ".join(ro) + " — only their "
                "reading verbs exist (no writes, no LLM revision, no index "
                "rebuilds). "
                if ro
                else ""
            )
            + "Any tool named in the guidance below that belongs to a set not "
            "listed here is NOT on this server — don't call it. "
        )
    crib = crib or Crib.open()
    mcp = FastMCP(
        "cribsheet",
        instructions=(
            preamble + "Shared, durable project memory: markdown notes with semantic + "
            "keyword search, persisting across sessions and shared across "
            "agents and tools. Use it IN ADDITION TO any built-in memory you "
            "have, not instead of — this is the cross-session, cross-agent "
            "store of record. "
            "CONSULT IT any time you need information about this project or a "
            "topic — a past decision, convention, gotcha, API detail, or prior "
            "investigation may already be stored. Call `note_lookup` to find it, or "
            "`note_apropos` to read the full matching sections. Do this before "
            "answering from memory alone; the stored answer may be more current. "
            "PERSIST what's worth keeping — whenever the user shares, or you "
            "establish, something durable (a decision, preference, convention, "
            "gotcha, or hard-won fact), also save it here so it outlives this "
            "session and reaches other agents: `note_store` a new note, or "
            "`note_append`/`note_edit` one found via `note_lookup`. Prefer updating an existing "
            "note over creating near-duplicates. "
            "DESIGN + PLANS: decisions live in their OWN facet, and its verbs — not "
            "the note verbs — are the way in, because only they speak the dependency "
            "EDGES. When a design question gets SETTLED, `design_add` it (with `deps` "
            "naming the decisions it builds on); to read one, `design_read` (body + "
            "what it rests on + what rests on it + whether either moved); to find one, "
            "`design_lookup`; to change one, `design_edit`/`design_append`, which "
            "answer with the decisions your change just put out of date. Before you "
            "change a decision — or code implementing one — `design_check` lists what "
            "has gone stale and why, and `design_reaffirm` clears each one you re-read "
            "(taint means a dep MOVED, not that the decision is wrong, so reaffirming "
            "is the cheap normal case). A hit carrying `tainted: true` is a decision "
            "nobody has re-read since its ground shifted: don't quietly reason from it. "
            "For work spanning sessions — anytime you'd write a todo list — `plan_add` "
            "the items (one call takes a batch) and `plan_status` them as you go; "
            "`plan_next` is the 'where was I' at the start of a session, and completing "
            "an item names what it unblocked. "
            "Asked to capture a design doc or a plan file INTO the graph? "
            "`design_import`/`plan_import` that doc and follow the procedure they "
            "return — they run no model, they hand you the doc's exact citable "
            "sections and the steps; extracted decisions land `proposed` until "
            "`design_promote`. "
            "CODE: a project may carry a *code symbol index* — its functions, classes, "
            "globals and class members, each with an LLM 'what it does' description, a "
            "real cross-file call graph (callers/callees) and references. For ANY code "
            "question — *where/what/how is X*, *what calls Y*, *what does Z do* — reach "
            "for these BEFORE grep/Read: `code_lookup` FIRST (find a symbol by CONCEPT or "
            "by name, even a cryptic private one — answers by intent, which grep can't), "
            "then `code_dossier <symbol>` for the full picture (signature + description + "
            "callers/callees/references, each neighbour annotated, + any pinned learning) "
            "in one call, or `code_xref`/`code_graph` to walk the graph. Don't grep or "
            "read files first and reach for these as a fallback — invert it. If the repo "
            "you're in has NO index, that's the EXPECTED first step, not a dead end: "
            "INDEX IT — `project_index` (project_path=<the repo dir>) indexes the source in one "
            "call — then look up. Do NOT read files or grep instead; indexing first is "
            "how you explore effectively (there's no shortcut — the utility comes from "
            "the index). PROJECT MODEL: code tools act on ONE current project. Set it once "
            "for the codebase you're working in — `project_use <name>`, or your FIRST call "
            "carrying `project_path` adopts that repo (the result says so when it does) — "
            "then reads need no project args. To act on a DIFFERENT project (a related "
            "codebase you're referencing), NAME it on that call: `project=<name>` or "
            "`project_path=<a path inside that repo>`. Naming one always wins over the "
            "current project and never changes it, so a cross-project lookup cannot "
            "re-home the writes that follow it — but it only applies to the call you put "
            "it on, so name it every time you mean elsewhere. `project_path` is NOT your "
            "shell cwd — it just identifies which repo you mean. "
            "When you finally UNDERSTAND a symbol — a "
            "subtlety, a gotcha, a 'now I get it' — `learning_add <symbol> \"…\"` pins a "
            "durable learning to it (survives re-indexing, works even on code you can't "
            "edit); it surfaces back via `code_lookup`/`code_xref`/`code_dossier`. "
            "CROSS-MACHINE: some notes are mirrored from another machine's Claude "
            "memory (frontmatter `source: claude_memory`, `host: <name>`, under "
            "`claude-memory/<host>/`). Treat the *learning* as portable — "
            "decisions, conventions, gotchas usually travel — but verify "
            "machine-specific details (absolute paths, ports, hostnames, install "
            "locations) against the local machine before relying on them."
            # this machine's declared facets: generated here, where they exist
            + (f"\n\n{text}" if (text := _facets.instructions(
                pending=crib.pending_facets)) else "")
        ),
    )

    def _wire(fn, resolution: str, echo: bool, elicit: bool, needs_target: bool):
        """Apply the DECLARED policy around a tool body: resolve the caller's
        `project`/`project_path` and pass the body the resolved project as `project`.

        The wrapper binds against the body's OWN signature (which `functools.wraps`
        keeps visible to FastMCP/pydantic), so it is transparent on the wire — same
        params, same schema, same Context injection."""
        sig = inspect.signature(fn)

        def prepare(args, kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            a = bound.arguments
            project, path = a.get("project"), a.get("project_path")
            if needs_target and not (project or path):
                raise CribUserError(
                    f"{fn.__name__} needs a SOURCE: pass project_path=<a path in that "
                    "repo> (or project=<name>). It acts on a specific repo, so it must "
                    "not fall through to whatever project happens to be current."
                )
            return a, project, path

        def resolve(project, path):
            """(resolved project, ProjectResolution|None) for the non-eliciting policies."""
            if resolution == "read":
                res = _resolve(crib, project, path)
                return res.project, res
            if resolution == "source":
                return _source_project(crib, project, path), None
            return _write_project(crib, project, path), None

        def finish(out, res, project, path):
            if echo:  # surface an IMPLICIT resolution
                out = (
                    _echo_list(out, res)
                    if isinstance(out, list)
                    else _echo_dict(out, res)
                )
            if res is not None and res.path_unmatched:
                # a pointed-at repo that anchored nothing — always surfaced, even on
                # echo=False reads (the reported case was plan_list, an echo=False
                # dict). The CLI strips this key: it owns the same advisory on stderr
                # and auto-fills cwd, so it must stay silent for a bare (no -P) call.
                out = _echo_unlinked(out, res)
            if elicit and isinstance(out, dict):
                out["project_source"] = (
                    "explicit" if project else "project_path" if path else "elicited"
                )
            return out

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def run(*args, **kwargs):
                a, project, path = prepare(args, kwargs)
                res = None
                if elicit:  # ask the human rather than erroring
                    a["project"] = await _write_project_elicit(
                        crib, project, path, a.get("ctx")
                    )
                else:
                    a["project"], res = resolve(project, path)
                    if res is not None and res.unanchored:
                        # an unanchored read never answers from a guess: ask, or
                        # refuse with the actionable error (see _read_project_elicit)
                        res = await _read_project_elicit(crib, res)
                        a["project"] = res.project
                return finish(await fn(**a), res, project, path)

            return run

        @functools.wraps(fn)
        def run_sync(*args, **kwargs):
            a, project, path = prepare(args, kwargs)
            a["project"], res = resolve(project, path)
            if res is not None and res.unanchored:
                # a sync tool cannot elicit — refuse with the same actionable error
                # the async path falls back to; the message reaches the LLM verbatim
                # (CribUserError rides the MCP face), and re-calling with project=
                # is the recovery. Never answer from a guess.
                anchor_help = (
                    STATELESS_HINT
                    if request_era() == ERA_STATELESS
                    else "(sessions reset when the crib daemon restarts): pass "
                    "project=<name> or project_path=<a path in the repo>, or "
                    "call project_use once"
                )
                raise CribUserError(
                    "no project is anchored for this session " + anchor_help
                )
            return finish(fn(**a), res, project, path)

        return run_sync

    def _user_facing(fn):
        """Deliver an EXPECTED refusal as the message it is. FastMCP renders a
        `ToolError` verbatim and is free to mask anything else, so a `CribUserError`
        left as a plain ValueError can reach the model as a generic "error calling
        tool" — dropping the part that was the answer (the candidate list, the
        heading names, what to pass instead). Applied to every tool, so a new
        `CribUserError` subclass is delivered correctly without touching this file.
        Anything that is NOT a CribUserError falls through untouched: a real bug
        should look like one."""
        from fastmcp.exceptions import ToolError

        from .errors import CribUserError

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def run(*args, **kwargs):
                try:
                    return await fn(*args, **kwargs)
                except CribUserError as e:
                    raise ToolError(str(e)) from e

            return run

        @functools.wraps(fn)
        def run_sync(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except CribUserError as e:
                raise ToolError(str(e)) from e

        return run_sync

    def crib_tool(
        resolution: str,
        *,
        echo: bool = False,
        elicit: bool = False,
        needs_target: bool = False,
    ):
        """Register an MCP tool AND declare its project-resolution policy (see the
        block above `_switch_if_created`). The declaration is the only place a policy
        is chosen; the body just uses the `project` it is handed.

        Refinements: `echo` — a read that surfaces an implicit resolution on its
        result; `elicit` — a write that ASKS for the target when it's omitted (and
        stamps `project_source`); `needs_target` — a repo-scoped tool that is
        meaningless without a source, so refuse the silent fall-through.

        `write` (and any `needs_target` tool) also gets a wire-schema constraint that
        `project` OR `project_path` must be supplied (a top-level JSON-Schema `anyOf`
        on `required`), so a validating client sees the requirement up front — not
        just the runtime guard. `add_tool` returns the FunctionTool, whose
        `.parameters` dict is the served input schema."""
        if resolution not in _POLICIES:
            raise ValueError(f"unknown resolution policy {resolution!r}")

        def register(fn):
            TOOL_POLICY[fn.__name__] = resolution  # the policy table is FULL, even
            # when THIS deployment doesn't expose the tool (the exposure gate is
            # below): the declared/reserved policy stays checkable everywhere
            set_name = tool_set_of(fn.__name__)
            if _EXPOSED is not None and set_name not in _EXPOSED:
                return fn  # not exposed on THIS deployment: never registered
            if set_name in _RO and fn.__name__ not in READ_ONLY_TOOLS.get(
                set_name, frozenset()
            ):
                return fn  # a read-only set: the mutating verbs never register
            impl = (
                fn
                if resolution in ("session", "none")
                else _wire(fn, resolution, echo, elicit, needs_target)
            )
            tool = mcp.add_tool(FunctionTool.from_function(_user_facing(impl)))
            if resolution == "write" or needs_target:
                tool.parameters.setdefault(
                    "anyOf", [{"required": ["project"]}, {"required": ["project_path"]}]
                )
            return fn

        return register

    # Every generated facet's verb family (crib.verbs): the notes tools, and
    # any declared facet's. Registered here, where the hand-written notes
    # tools were, through the same `crib_tool` policy declaration.
    _verbs.register(crib, crib_tool)

    @crib_tool("none")
    async def project_reconcile() -> dict[str, Any]:
        """Sweep every project for changes made while crib was down and bring the
        index back in line. Safe to call anytime — the hash gate no-ops anything
        already current."""
        return await crib.reconcile_all()

    @crib_tool("none")
    def ops() -> dict[str, Any]:
        """The ops view: what the engine is doing RIGHT NOW — live sweeps
        (done/total/failed/scope), per-project in-flight files, and generation
        slot occupancy (in-flight / waiting / rate-limited / cooldown seconds).
        The one call that answers "what is running and why is nothing moving."""
        return crib.ops_status()

    @crib_tool("source")
    async def code_index(
        path: str, project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        """Populate the code index for a source file: extract its symbols (functions,
        classes, globals, class members) + call graph + references via the LSP,
        describe them, persist under `<project>/symbol_index/`. Use when code_lookup
        says a project isn't indexed yet. `path` MUST be ABSOLUTE — a relative path
        resolves against the daemon's cwd (not yours) and fails; also pass
        `project_path=<your working dir>` so the project resolves via .crib (which
        WINS over the sticky session project — indexing a repo files its symbols
        under that repo's project, never the one you happen to be sitting in)."""
        # Declared REPO-SCOPED (`source`) like its project_* siblings, not `read`:
        # a sticky session must never capture another repo's symbols.
        return await crib.code_index(path, project, cwd=_cwd(project_path))

    @crib_tool("source")
    async def project_setup(
        project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        """ONBOARD a repo for crib in one call — when code_lookup says a project isn't
        indexed, do THIS, don't fall back to grep. Ensures a `.crib` (auto-created with
        sensible defaults if missing), imports the repo's docs into notes, AND indexes
        all its source code (functions/classes/globals/members + call graph +
        references + descriptions). Pass `project_path=<the repo dir>` (a bare
        `project=<name>` works only for an already-indexed project — it resolves the
        recorded root). Idempotent. Then code_lookup/code_dossier work. Code-only
        variant: project_index."""
        return _switch_if_created(
            await crib.project_setup(project, cwd=_cwd(project_path))
        )

    @crib_tool("source")
    async def project_index(
        project: str | None = None,
        project_path: str | None = None,
        budget_s: float | None = None,
    ) -> dict[str, Any]:
        """(Re)index a project's SOURCE CODE from its `.crib`, PLUS the prose it
        declares under `docs:` — those are indexed IN-SITU (searchable via
        note_lookup; the repo keeps the only copy). It differs from
        project_setup only in not COPYING files into crib-owned notes. Use to
        index a repo for code_lookup/code_dossier, or to refresh after edits
        (cheap: unchanged files are skipped). Pass
        `project_path=<the repo dir>` (a `.crib` is auto-created if missing); a
        bare `project=<name>` re-indexes an ALREADY-INDEXED project from its
        recorded root.

        THE SWEEP RUNS DAEMON-OWNED and this call returns at once — a RUNNING
        HANDLE (`running: true` + done/total) or, for an instant sweep, its full
        result. DON'T WAIT: poll `project_status` (its `jobs` section: never
        errors, never blocks, reports failed jobs); re-invoke to join; a
        completed sweep's re-invoke returns its full result. `budget_s=<seconds>`
        bounds the WORK (not the call): files not reached are deferred
        (`complete=false, remaining=N`) — re-invoke to continue (finished files
        re-skip via the content-hash gate)."""
        res = await crib.project_index(
            project, cwd=_cwd(project_path), budget_s=budget_s
        )  # the polling contract — a RUNNING HANDLE at once (wait_s=None)
        return _switch_if_created(res)

    @crib_tool("none")
    async def project_relocate(
        project: str | None = None,
        project_path: str | None = None,
        source: str | None = None,
        keep_prefix: bool = False,
    ) -> dict[str, Any]:
        """The move-and-rename verb: a repo project's checkout was moved and/or
        renamed on this machine (works after the fact — the old path may be gone,
        the project renamed, an earlier relocate cut halfway — re-invoke RESUMES).
        The fast half (code-index .source_root, doc-source mounts, the repo's own
        .crib line when it names the pre-rename name, an in-repo store stub) is
        re-pointed at once and idempotent. The HEAVY half — re-homing the
        `sources/<old>/` mount when the DIR was renamed (doc chunks rekey +
        re-embed; every citation in THIS project's notes rewritten to the new
        spelling) — runs as a DAEMON-OWNED job: the call returns a RUNNING HALF
        at once with `progress` + `resume_hint`. DON'T WAIT: poll `project_status`
        (its `jobs` section carries live done/total + the banked counts);
        re-invoking the verb joins when the job is still running. A trailing CODE
        rehash sweep joins too — never an error. keep_prefix=true = identity-
        keeping only. Foreign sites are REPORTED, not edited."""
        return await crib.project_relocate(project, project_path, source, keep_prefix)

    @crib_tool("source")
    def project_status(
        project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        """Is this repo code-indexed? Returns symbol/file counts, a kind breakdown, and
        the `.crib` source paths — to orient before project_setup / a code_lookup. Pass
        `project_path=<the repo dir>`."""
        return crib.project_status(project, cwd=_cwd(project_path))

    @crib_tool("source")
    def project_doctor(
        project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        """What in a project needs attention, report-only: dangling deps, deps
        on superseded/declined items, changed or vanished cited sections,
        moved/orphaned learnings, one id in several files, near-duplicate
        notes/decisions/plans, pending descriptions. Each finding names the
        verb that fixes it; nothing is changed."""
        return crib.project_doctor(project, cwd=_cwd(project_path))

    @crib_tool("source")
    def project_lookup(
        query: str,
        k: int = 8,
        kinds: list[str] | None = None,
        status: list[str] | None = None,
        project: str | None = None,
        project_path: str | None = None,
    ) -> list[dict[str, Any]]:
        """One search across notes, decisions, plans and learnings at once:
        each hit carries its `kind` and, for a decision or plan, its `status`.
        `kinds` (note/design/plan/learning) and `status` (e.g. active, todo)
        narrow it. Use the per-pillar lookups when you know which you want."""
        return crib.lookup_all(query, project, k, kinds, status, cwd=_cwd(project_path))

    @crib_tool("source")
    def project_related(
        ref: str,
        k: int = 3,
        project: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """One item and everything around it, across kinds: a note, decision,
        plan, learning or code symbol (by id, relpath, title or symbol name),
        its declared links both ways (deps with their kinds, dependents,
        drawn_from, superseded_by, learns, member_of, calls/references), and
        its `k` nearest items of every kind by stored vector, with status."""
        return crib.project_related(ref, k, project, cwd=_cwd(project_path))

    @crib_tool("none")
    async def project_wait(
        project: str | None = None,
        project_path: str | None = None,
        wait_s: float = 30.0,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """PREFER POLLING (`project_status`'s `jobs` section) — a blocking wait
        ties up a client request and gets cut off at proxy caps. This verb is
        the JOIN primitive for scripts/terminals only: bounded-block on a
        running index job for this project. The final result if the sweep
        completes within wait_s; else the live partial state (a re-invoke keeps
        waiting). Emits progress markers while it waits (`42/317 files (13%) ·
        eta 1:50`). Never starts a sweep; `{running: false}` when idle."""
        return await crib.project_wait(
            project, cwd=_cwd(project_path), wait_s=wait_s, ctx=ctx
        )

    @crib_tool("source")
    async def project_cancel(
        project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        """Cancel a running index sweep for this project. Completed files remain
        (hash-gated); undescribed files retry on the next project_index."""
        return await crib.project_cancel(project, cwd=_cwd(project_path))

    @crib_tool("source")
    def project_forget(
        project: str | None = None,
        with_learnings: bool = False,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """Clear a project's CODE INDEX (symbol_index). Keeps attached learnings, notes
        and `.crib` by default (learnings are durable — pass with_learnings=True to drop
        them too). Recoverable by re-running project_index. Pass `project_path=<the repo dir>`."""
        return crib.project_forget(
            project, with_learnings=with_learnings, cwd=_cwd(project_path)
        )

    @crib_tool("source")
    async def project_adopt(
        project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        """Move a project's NOTES into the repo that owns them, at the dir its
        `.crib` declares as `store:` (repo-root-relative). After this the repo's own
        git carries the notes — commit them with your code; crib's `memory
        sync`/`snapshot` refuse for this project. The derived index (embeddings,
        symbol index) stays machine-local, so nothing unshareable lands in the repo.
        Note ids and search results are unchanged — this moves files, it doesn't
        reindex. Pass `project_path=<the repo dir>`. Idempotent; `project_release`
        undoes it."""
        return await crib.project_adopt(project, cwd=_cwd(project_path))

    @crib_tool("source")
    async def project_release(
        project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        """Move an adopted project's notes back OUT of its repo into the global
        crib store (the inverse of `project_adopt`) — for when the repo is going
        away, or the notes should stop travelling with it. Also the fix when a
        project is listed `unavailable` because its repo isn't on this machine…
        except that the notes have to BE here to move: clone the repo first.
        Idempotent. Pass `project_path=<the repo dir>` or `project=<name>`."""
        return await crib.project_release(project, cwd=_cwd(project_path))

    @crib_tool("source")
    async def project_migrate(
        project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        """Move a project's legacy facet notes (`notes/design/`, `notes/plans/`,
        `notes/code-learnings/`) into their sibling pillar stores and requalify
        their citations — the pre-split → split layout migration, on demand.
        Every full reindex runs the same routine automatically, so this verb is
        for driving the move (and reading its report) explicitly. Idempotent;
        name collisions between the two layouts are skipped and reported, never
        merged."""
        return await crib.project_migrate(project, cwd=_cwd(project_path))

    # NO resolution policy on create: the name is caller-supplied OR derived
    # from the repo. The "write" default-resolution would land a name-less
    # call on the session default and write `project: default` into a foreign
    # repo's .crib — the exact misroute this policy system exists to refuse.
    @crib_tool("none")
    async def project_create(
        project: str | None = None,
        store: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly create a project: pillar skeleton + birth manifest
        (created/host in `.cribproject`). A DOORWAY, not a gate — writes still
        create implicitly; use this when a project should exist before its
        first write, or to declare RESIDENCY AT BIRTH: `store=<repo-relative
        path>` + `project_path=<the repo dir>` writes the repo's `.crib`
        (`project:` + `store:`) and the machine stub, so the project is born
        in-repo (its pillar stores travel with the repo; commit them). The
        at-birth store must be empty — moving an existing global project's
        notes into the repo is `project_adopt`, not this. Idempotent.

        The name comes from you OR the repo's `.crib`/repo dir — never from
        the session's sticky current project (a fresh repo without a `.crib`
        derives its name from the repo dir; this is why create runs with NO
        resolution policy: the default-project fallback would silently point
        a repo-bound store at the shared bucket). The default project is
        refused as a residency target."""
        return await crib.project_create(project, cwd=_cwd(project_path), store=store)

    @crib_tool("write")
    async def project_delete(project: str, force: bool = False) -> dict[str, Any]:
        """Fenced removal of a project from CRIB: its index entries, caches,
        code-index state, mirror bindings, and the machine-local tree — moved
        to `.trash/` (one-level undo), not rm -rf'd.

        THE INVARIANT: crib NEVER removes anything from a user repo. For an
        in-repo project the committed notes, the store's ring and the repo's
        `.crib` STAY (the result says so, and how to release them first if
        they should follow the namespace). `force` is accepted for surface
        compatibility and gates nothing. The default project is refused (no
        owner to consent); a running code sweep or background reconcile is
        refused. Deletion never resolves a session default or a cwd:
        `project=` is required and is the whole answer.

        ALWAYS elicited in MCP mode, and the fences run FIRST: the body
        computes the deletion plan (which refuses any fence), then the client
        is asked to echo the project name against an exact account of what
        will happen. A client without elicitation support is refused (the
        CLI's terminal prompt is the same gate in the other mode)."""
        plan = crib._delete_plan(project, force)  # fences run BEFORE the elicitation
        if not plan["exists_anywhere"]:
            return {
                "project": project,
                "deleted": False,
                "message": "nothing to delete — no such project on this machine",
            }
        detail = (
            "DELETE crib project '"
            + project
            + "'"
            + (
                # The invariant, spoken: crib's index/cache/tree state goes; a user
                # repo is never touched, whatever the force flag says.
                " — crib's index entries, caches and the machine-local tree (to "
                ".trash) are removed. YOUR REPO IS NOT TOUCHED."
                + (
                    " Your notes stay in the repo store at "
                    + str(plan["store_token"])
                    + " — release them first if you want them in the global store."
                    if plan["in_repo"]
                    else ""
                )
            )
        )
        await _confirm_destructive(project, "project_delete", detail)
        return await crib.project_delete(project, force=force, cwd=None)

    @crib_tool("write")
    async def project_rename(project: str, to: str) -> dict[str, Any]:
        """Rename a project namespace (`projects/<old>` → `projects/<new>`).

        Travels: the pillar tree, machine stub, symbol_index, version ring
        (note ids live in frontmatter — untouched). Churns: every chunk id
        (they hash the project name), so this deletes the old chunks and
        reindexes under the new name (a full re-embed; the known cost of a
        rare verb), after stamping any id-less note with its pre-rename
        derived id so ring history stays resolvable. Other projects' `.crib`
        `refs:` naming the old name are reported (they live in foreign repos —
        fix by hand). Pass `project_path=` only to resolve OTHER projects'
        context; `project=` is required."""
        return await crib.project_rename(project, to, cwd=None)

    @crib_tool("read", echo=True)
    async def code_xref(
        symbol: str,
        path: str = "",
        scope: str = "",
        lang: str = "",
        project: str | None = None,
        project_path: str | None = None,
    ) -> list[dict[str, Any]]:
        """A symbol's callers (←), callees (→) and references (⇐ — broader than calls),
        plus any human learning pinned to it — from the persisted index, no live LSP.
        `symbol` is a bare name or dotted fqname. Name the project on the call —
        `project_path=<a path in that repo>` or `project=<name>` — whenever you mean
        somewhere other than the current one; naming it always wins."""
        return crib.code_xref(symbol, project, path=path, scope=scope, lang=lang)

    @crib_tool("read", echo=True)
    async def code_lookup(
        query: str,
        project: str | None = None,
        k: int = 8,
        project_path: str | None = None,
    ) -> list[dict[str, Any]]:
        """FIND A SYMBOL BY CONCEPT OR NAME — reach for this FIRST, before grep/Read,
        on ANY "where/what/how is X" code question ("where do we fuse ranked lists",
        "the oauth refresh", a bare/cryptic name). HYBRID: dense search over LLM 'what
        it does' descriptions ⊕ name/subtoken match — finds by intent (grep can't) OR by
        name. Returns ranked symbols with signature, file:line, callers/callees/refs. If
        the project isn't indexed it SELF-DIAGNOSES — so just try it. If THIS repo has no
        index yet, that's the normal first step: INDEX IT with `project_index`
        (project_path=<the repo dir>), then retry the lookup — do NOT read files or grep instead.
        Pass `project_path=<a path in that repo>` (or `project=<name>`) to search a
        project other than the current one — it wins for that call and changes
        nothing after it. Then `code_dossier` a hit to go deep, or `code_graph` to
        walk the tree."""
        return crib.code_lookup(query, project, k)

    @crib_tool("read", echo=True)
    async def code_dossier(
        symbol: str,
        project: str | None = None,
        project_path: str | None = None,
        path: str = "",
        scope: str = "",
        lang: str = "",
    ) -> dict[str, Any]:
        """EVERYTHING about one symbol in a single call: signature + description, and its
        callers/callees/references EACH annotated with the NEIGHBOUR'S own description,
        plus any pinned learning. The efficient way to *understand* a symbol (vs
        code_lookup which *finds* it) — read a symbol and its whole neighbourhood without
        follow-up lookups. `symbol` is a bare name or dotted fqname.

        NARROW an ambiguous name on the axis you actually know, rather than guessing
        crib's qualified spelling: `path=` a trailing run of the file path
        (`state.rs`, `core/state.rs`), `scope=` a trailing run of the language's own
        scope (`ServerState`, `state::ServerState`), `lang=` exact. They are
        constraints — several narrow further, and none makes an ambiguous name
        resolve by picking one. Pass `project_path=`/`project=` to target a DIFFERENT
        project than your current resolution."""
        return crib.code_dossier(symbol, project, path=path, scope=scope, lang=lang)

    @crib_tool("read", echo=True)
    async def code_graph(
        symbol: str | None = None,
        direction: str = "callees",
        depth: int = 6,
        project: str | None = None,
        project_path: str | None = None,
        shape: str | None = None,
        group_by: str | None = None,
        group_depth: int = 0,
        path: str = "",
        scope: str = "",
        lang: str = "",
        under: str = "",
        exclude: str = "",
    ) -> dict[str, Any]:
        """Call graph around a symbol from the index: `callees` (what it calls),
        `callers` (what calls it), or `references` (everywhere mentioned — broader than
        calls, and the only relation for symbols-only servers like zsh's shuck),
        recursive to `depth`. Nodes with a pinned learning are flagged.

        TWO SHAPES, and the default is the reading one:
        - `shape="tree"` (default) — nested {fqname, kind, file, line, children[]} for
          following one chain by eye. A symbol reached by several paths CANNOT be shown
          as one node here: it is duplicated, or marked `repeat` and cut off.
        - `shape="edges"` — the depth-bounded SUBGRAPH as {nodes[], edges[]}: each
          symbol once, at its shortest distance, with EVERY edge kept and deduplicated,
          oriented caller→callee whichever direction you walked. REACH FOR THIS when
          convergence is the point (which paths all end at the same symbol) or when the
          output feeds a diagram/layout tool — it is their native input, and it makes
          convergence explicit instead of something you reconstruct by hand.

        `group_by` (implies `shape="edges"`) rolls symbol edges up onto ONE AXIS,
        each edge carrying a `weight` — the architecture diagram. `file` is which
        files depend on which; `dir` is the same at directory grain, with
        `group_depth=N` choosing how coarse; `scope` groups by what the LANGUAGE
        says a symbol belongs to, which is a different question from where it lives
        (they nearly agree in Python, are unrelated in C++, and C has no scope at
        all — those symbols group under `(no scope)`).

        OMIT `symbol` for the WHOLE PROJECT: every indexed symbol and every edge, no
        root, no depth bound — including symbols no walk reaches (entry points, dead
        code). Large repos: prefer `group_by="module"` (uncapped) over the raw symbol
        export.

        `symbol` takes a full qualified name, a trailing run of its segments, or a
        bare local name, in any language's separator. A bare name matching SEVERAL
        symbols returns no graph and errors with the candidates ranked by caller
        count. NARROW it on the axis you actually know rather than guessing crib's
        spelling: `path=` a trailing run of the file path (`state.rs`,
        `core/state.rs`), `scope=` a trailing run of the language's own scope
        (`ServerState`, `state::ServerState`), `lang=` exact. They are constraints —
        several narrow further, and none of them makes an ambiguous name resolve by
        picking one. Every result carries `resolved` naming what the symbol resolved
        to. Pass `project_path=<a path in the repo>` (or `project=<name>`) only to
        target a DIFFERENT project than your current one."""
        return crib.code_graph(
            symbol,
            direction,
            depth,
            project,
            shape=shape,
            group_by=group_by,
            group_depth=group_depth,
            path=path,
            scope=scope,
            lang=lang,
            under=under,
            exclude=exclude,
        )

    @crib_tool("write")
    async def code_convert(
        project: str | None = None, project_path: str | None = None, apply: bool = False
    ) -> dict[str, Any]:
        """Convert this project's symbol store to the current shape, in place — no
        LSP, no LLM, descriptions/keywords/edges byte-identical or the record is
        left alone and reported. Per record and resumable: re-run after a crash and
        it continues; a record with no `file`/`name` is reported under
        `needs_reindex` instead of guessed at. DRY RUN unless `apply=True`."""
        return await crib.code_convert(project, cwd=_cwd(project_path), apply=apply)

    # ── design decisions & plan items (crib/designs.py) ───────────────────────
    # THE FACET IS THE INTERFACE. These verbs — not `note_read`/`note_edit` on a
    # path under `design/` — are how decisions are read and written, because only
    # they can speak the EDGES: what a decision rests on, what rests on it, and
    # what a change just put out of date. Notes-in-a-directory is the backend.
    #
    # Every docstring below states the taint CONTRACT in one line and, where it
    # applies, the CUE that should make an agent reach for that verb — a tool's
    # description is its usage instruction (DESIGN §5), and the global rules are
    # not in front of the model at the moment its hand is already on the tool.
    #
    # Policy split, declared per verb: the two `*_add` verbs CREATE a durable fact,
    # so they must NAME their project like any other write. Every other verb is
    # keyed by a `ref` that only resolves INSIDE one project — naming the wrong
    # project fails to resolve rather than misfiling anything — so they take the
    # READ policy, the same documented exception the `learning_*` verbs carry, and
    # each says so in its own docstring.

    @crib_tool("none")
    def memory_snapshot(message: str | None = None) -> str:
        """Create a git checkpoint of the whole memory store's data tree (if git is set up)."""
        return crib.snapshot(message)

    @crib_tool("none")
    def memory_history(relpath: str | None = None) -> list[str]:
        """Show git commit history for the whole tree (or a single note)."""
        return crib.history(relpath)

    @crib_tool("source", needs_target=True)
    async def note_import_memory(
        project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        """Mirror Claude Code's own harness memory (the `memory/*.md` files it
        writes for this project) into a crib project, so those notes become
        searchable here alongside everything else. One-way + idempotent; opts the
        repo into the daemon's live mirror so future memory edits sync on their
        own. REQUIRES a source repo — pass `project_path=<the repo dir>` (or
        `project=`); like `note_import` it resolves repo-scoped, never from the
        sticky session or the default project."""
        return _switch_if_created(
            await crib.import_claude_memory(project, cwd=_cwd(project_path))
        )

    @crib_tool("none")
    def status() -> dict[str, Any]:
        """One-call health summary: every project's inventory (notes, in-situ doc
        chunks, code symbols, learnings), how many of its design decisions have
        gone stale (`design_tainted` — `design_check` says which and why),
        git-sync state (dirty/ahead/behind),
        which warm LSP sessions are attached (alive/busy/idle), and any indexing
        currently in flight. `sweeps` is the RELIABLE wait signal for a background
        `project_index`: {project: {done, total}} while it runs, absent when done —
        poll status until your project leaves `sweeps`. Use to orient across ALL
        projects; `project_status` goes deep on one."""
        return crib.status()

    @crib_tool("none")
    def project_list() -> list[dict[str, Any]]:
        """List crib projects (separate memory namespaces). Use to discover
        what's available before a `note_lookup`/`note_store` in a specific project.
        A project whose notes live in its own repo also carries `store_root`, and
        `unavailable: true` when that repo isn't on this machine (clone it, or
        `project_release` the project) — such a project can't be read or written
        until then."""
        return crib.project_list()

    @crib_tool("session")
    def project_use(project: str) -> dict[str, Any]:
        """Set THIS session's current project — subsequent `note_lookup`/`note_store`/etc.
        target it without passing `project` each time. Sticky for the connection; a
        per-call `project`/`project_path` still wins for that one call and leaves this
        unchanged. Your first call carrying `project_path` adopts that repo the same
        way (and says so), so call this to SWITCH, or to be explicit up front. The
        namespace is created immediately (so it's real and listed, not a phantom
        you're 'in' until the first write)."""
        # One implementation, shared with the in-process CLI: `Crib.use_project`
        # validates the name (before the eager mkdir — `../x` would otherwise plant a
        # namespace outside the projects tree), creates the namespace, and sets the
        # session pointer. A second copy here would only drift.
        return crib.use_project(project)

    @crib_tool("session")
    def project_current(project_path: str | None = None) -> dict[str, Any]:
        """Show this session's current project (seeding it from `project_path`/.crib if not
        yet set), how it resolved, plus the available projects."""
        # Shared with the in-process CLI (see `project_use`): `Crib.current_project`
        # applies the READ policy itself — this tool reports the session pointer
        # rather than resolving a project for an op, hence the `session` declaration.
        return crib.current_project(cwd=_cwd(project_path))

    return mcp


def _prepare_http_serve(mcp) -> None:
    """Prep the FastMCP server for ITS HTTP face (called from `_serve_async`'s
    http branch — deliberately NOT from `build_server`: the in-memory transport
    shares one stream pair, where an auto client's discover probe would open the
    modern era before the handshake- fallback can take effect; over HTTP the
    probe is a separate connection, so the pin lands cleanly there)."""
    # Sessions must not silently reset mid-chat (§15): mcp 2 expires idle
    # stateful HTTP sessions after 30 min by default; 24h is beyond any pause
    # while still eventually reaping a vanished client.
    import fastmcp

    # pydantic settings on the fastmcp 4 line have this field; setattr because
    # the type resolver may resolve a different fastmcp.
    setattr(fastmcp.settings, "http_session_idle_timeout", 86_400)  # noqa: B010
    # The daemon serves the handshake era only: a modern-era direct client
    # would otherwise statelessly re-resolve per request (§15's sticky project
    # dies) and lose elicitation.
    _pin_legacy_era(mcp)


def _pin_legacy_era(mcp) -> None:
    """Advertise ONLY the handshake era, so clients with era defaults negotiate
    stateful: modern-era (2026-07-28) stateless connections build a Connection
    per request, which kills the per-session current project (DESIGN §15) and
    forbids server-initiated requests (elicitation). Verified: without this pin
    a client whose default is `mode='auto'` connects directly and negotiates
    `2026-07-28` — sticky adoption dies between calls, silently; with an empty
    `supported_versions` the same client falls back to the handshake era (`
    `2025-11-25``) and everything works.

    This is a deliberate SERVER-side decision, the mirror of the combiner
    pinning ITS clients to `mode='legacy'`: crib's session model (§15) and the
    destructive-confirm gates depend on the back-channel that the handshake era
    has and the modern era removes, so cribsheet does not yet support the
    modern era. Revisit when the stateless era grows per-session state or
    crib's session model moves to per-call selectors.

    (Direct mcp 2 lowlevel-server API: `mcp._mcp_server` is fastmcp's own
    surface into it — the same route fastmcp's internal code takes. If a later
    fastmcp moves it, the e2e HTTP session tests fail loudly, flagging the
    migration.)"""
    from mcp.types import DiscoverResult, RequestParams, ServerCapabilities

    async def legacy_only_discover(ctx, params=None):  # noqa: ANN001
        return DiscoverResult(supported_versions=[], capabilities=ServerCapabilities())

    mcp._mcp_server.add_request_handler(
        "server/discover", RequestParams, legacy_only_discover
    )


async def _serve_async(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8787,
    tools: str | list[str] | None = None,
) -> None:
    from crib.diag import DIAG, install_log

    if transport != "stdio":
        # The daemon: sharedserver keeps no log of its stderr, so crib does —
        # started BEFORE the store opens, so a hang there leaves a trace.
        from crib.config import Config
        from crib.paths import Paths

        paths = Paths.resolve()
        daemon_cfg = Config.load(paths.config_file).daemon
        log = daemon_cfg.log_file
        if log:
            path = paths.index_dir / "daemon.log" if log == "auto" else Path(log)
            install_log(path.expanduser(), daemon_cfg.log_max_mb * 1024 * 1024)
        DIAG.milestone("opening the store")
    crib = Crib.open()
    from crib import __version__

    DIAG.milestone("store ready", store=type(crib.store).__name__, version=__version__)
    mcp = build_server(crib, tools=tools)
    DIAG.milestone("tools registered", tools=len(await mcp.list_tools()))
    # Watcher runs on THIS loop so its index_file calls share the per-path locks
    # with the tool calls (DESIGN §4) — correctness depends on one loop.
    if crib.config.watch:
        try:
            crib.start_watchers(asyncio.get_running_loop())
            DIAG.milestone("watchers started")
        except Exception as e:  # noqa: BLE001 — watchdog optional; degrade quietly
            print(f"[crib] watcher disabled: {e}", file=sys.stderr)
    # Catch up on anything changed while crib (and its watcher) were down — in the
    # BACKGROUND, so a cold daemon with an offline backlog can't hold the port
    # closed past the client's ready timeout. Watcher first so edits during the
    # sweep aren't missed; the hash gate makes overlap a no-op (DESIGN §4/§9).
    # Progress and the skip list surface via `status` / stderr.
    crib.reconcile_in_background(asyncio.get_running_loop())
    # Catch up + live-mirror any bound Claude harness memory dirs (DESIGN §13).
    try:
        await crib.start_memory_mirror(asyncio.get_running_loop())
    except Exception as e:  # noqa: BLE001 — watchdog optional / stale binding; degrade
        print(f"[crib] memory mirror disabled: {e}", file=sys.stderr)
    try:
        if transport == "stdio":
            await mcp.run_async(transport="stdio")
        else:
            # Keep a chat's session state alive across an idle pause and pin the
            # handshake era (both live in _prepare_http_serve; see its rationale).
            _prepare_http_serve(mcp)
            # Optional inbound bearer auth on /mcp (off unless CRIBSHEET_AUTH_TOKEN
            # is set). inbound_auth.py is vendored byte-identical from mcp-companion's
            # combiner — no dependency on it. /health stays open.
            from starlette.middleware import Middleware

            from crib.inbound_auth import BearerAuthMiddleware, resolve_auth_token

            _mw: list[Middleware] = []
            _tok = resolve_auth_token("CRIBSHEET_AUTH_TOKEN")
            if _tok:
                _mw.append(
                    Middleware(
                        BearerAuthMiddleware,
                        token=_tok,
                        is_protected=lambda p: p != "/health",
                    )
                )
            DIAG.milestone("serving", transport="http", port=port)
            await mcp.run_async(transport="http", host=host, port=port, middleware=_mw)
    finally:
        crib.close()


def main(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8787,
    tools: str | list[str] | None = None,
) -> None:
    asyncio.run(_serve_async(transport, host, port, tools=tools))


if __name__ == "__main__":
    main()
