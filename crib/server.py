"""FastMCP server exposing the crib tool surface (DESIGN §5).

Lazy-imports fastmcp so the package stays importable without it.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import sys
from pathlib import Path
from typing import Any

from .app import Crib
from .errors import CribUserError
from .session import ProjectResolution, resolve_session_project, session_state

# How often project_index emits an MCP progress notification while a sweep runs — frequent
# enough to keep the call alive (progress resets the client idle timeout) and feel live.
_PROGRESS_EVERY_S = 2.0

try:  # Context annotates the elicitation param; needed at runtime for injection AND
    from fastmcp import Context  # for the forward-ref eval of `ctx: Context`.
except Exception:  # pragma: no cover — package stays importable without fastmcp
    Context = Any  # type: ignore[assignment,misc]


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
def _resolve(crib: Crib, project: str | None,
             project_path: str | None) -> ProjectResolution:
    """The READ policy as a `ProjectResolution` (project + how it resolved)."""
    return resolve_session_project(
        session_state(), project, _cwd(project_path),
        lambda c: crib.resolve_project(None, c),
        default=crib.config.default_project)


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


def _echo_list(hits: Any, res: ProjectResolution) -> Any:
    """Surface an IMPLICIT resolution on a LIST result where it would otherwise be
    invisible: an EMPTY result from a sticky/seeded project is indistinguishable
    from 'answered the wrong project', so return one diagnostic marker instead of a
    bare `[]`. Non-empty lists already tag each hit with its owning `project`."""
    if res.implicit and isinstance(hits, list) and not hits:
        return [{"resolved_project": res.echo(), "matches": 0,
                 "note": (f"resolved implicitly to {res.project!r} via {res.via}; "
                          "0 matches. If you meant another project pass "
                          "project=<name> or project_path=<a path in that repo>.")}]
    return hits


def _source_project(crib: Crib, project: str | None,
                    project_path: str | None) -> str | None:
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
        return None                     # let the repo's .crib name the project
    return _project(crib, None, None)   # neither given → sticky session project


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
        "<a path in that repo>. Writes don't inherit the sticky current project.")


async def _write_project_elicit(crib: Crib, project: str | None,
                                project_path: str | None, ctx: Any) -> str:
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
            "not the repo you're currently working in.", response_type=str)
        chosen = getattr(result, "data", None)          # AcceptedElicitation.data
        if isinstance(chosen, str) and chosen.strip():
            return chosen.strip()
    except Exception:  # noqa: BLE001 — no elicitation support → fall back to the error
        pass
    return _write_project(crib, None, None)             # raises the explicit-target error


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
            + ", ".join(sorted(crib.projects())) + " — or give a path inside the "
            "repo you mean.", response_type=str)
        chosen = getattr(result, "data", None)
        if isinstance(chosen, str) and chosen.strip():
            chosen = chosen.strip()
            if "/" in chosen or chosen.startswith("~"):
                proj = crib.resolve_project(None, Path(chosen).expanduser())
            else:
                if chosen not in crib.projects():
                    raise CribUserError(
                        f"no project named {chosen!r} — one of: "
                        + ", ".join(sorted(crib.projects())))
                proj = chosen
            # the human just anchored the session — adopt, and say so
            session_state().current_project = proj
            return ProjectResolution(proj, "elicited", session_set=True)
    except CribUserError:
        raise
    except Exception:  # noqa: BLE001 — no elicitation support / declined → refuse
        pass
    raise CribUserError(
        "no project is anchored for this session and none was named (sessions "
        "reset when the crib daemon restarts): pass project=<name> or "
        "project_path=<a path in the repo>, or call project_use once. Projects: "
        + ", ".join(sorted(crib.projects())))


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


def _switch_if_created(result: dict) -> dict:
    """Creating a project switches the session into it — referencing an existing
    one (a one-off `project` arg) does not (DESIGN §15)."""
    if isinstance(result, dict) and result.get("created"):
        session_state().current_project = result.get("project")
    return result


def build_server(crib: Crib | None = None):
    from fastmcp import FastMCP  # lazy
    from fastmcp.tools import FunctionTool

    crib = crib or Crib.open()
    mcp = FastMCP(
        "cribsheet",
        instructions=(
            "Shared, durable project memory: markdown notes with semantic + "
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
                    "not fall through to whatever project happens to be current.")
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
            if echo:                            # surface an IMPLICIT resolution
                out = (_echo_list(out, res) if isinstance(out, list)
                       else _echo_dict(out, res))
            if elicit and isinstance(out, dict):
                out["project_source"] = ("explicit" if project else
                                         "project_path" if path else "elicited")
            return out

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def run(*args, **kwargs):
                a, project, path = prepare(args, kwargs)
                res = None
                if elicit:                      # ask the human rather than erroring
                    a["project"] = await _write_project_elicit(crib, project, path,
                                                               a.get("ctx"))
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
                raise CribUserError(
                    "no project is anchored for this session (sessions reset when "
                    "the crib daemon restarts): pass project=<name> or "
                    "project_path=<a path in the repo>, or call project_use once. "
                    "Projects: " + ", ".join(sorted(crib.projects())))
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

    def crib_tool(resolution: str, *, echo: bool = False, elicit: bool = False,
                  needs_target: bool = False):
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
            TOOL_POLICY[fn.__name__] = resolution
            impl = (fn if resolution in ("session", "none")
                    else _wire(fn, resolution, echo, elicit, needs_target))
            tool = mcp.add_tool(FunctionTool.from_function(_user_facing(impl)))
            if resolution == "write" or needs_target:
                tool.parameters.setdefault(
                    "anyOf", [{"required": ["project"]}, {"required": ["project_path"]}])
            return fn
        return register

    @crib_tool("read")
    def note_lookup(query: str, project: str | None = None, k: int = 8,
               tags: list[str] | None = None,
               keyword_labels: list[str] | None = None,
               keyword_weight: float | None = None,
               summary_labels: list[str] | None = None,
               summary_weight: float | None = None,
               project_path: str | None = None) -> list[dict[str, Any]]:
        """Semantic search over memory. Call this FIRST when the user asks
        about this project — a prior decision, convention, or investigation
        may already be stored. Returns ranked note sections, each with its
        relpath and the line_start/line_end span of the matching section so
        you can jump straight to it (pair with `note_locate` for the abspath).
        `tags` filters by frontmatter tag. Notes only: design decisions, plan
        items and learnings live in their own pillar stores and never appear
        here — search them with `design_lookup` / `plan_lookup`, whose hits
        also carry the facet state.
        `keyword_labels`/`keyword_weight` (BM25 keyword_index) and
        `summary_labels` (dense summary_index aliases) override which LLM index
        sets feed retrieval (default from config); mainly for eval sweeps.
        A hit carrying `index_rebuilding: true` means this project is still being
        re-embedded after a store wipe (`status` shows the sweep) — the result set
        is INCOMPLETE, so retry once it clears rather than concluding nothing exists."""
        return [vars(h) for h in
                crib.lookup(query, project, k, tags,
                            keyword_labels=keyword_labels,
                            keyword_weight=keyword_weight,
                            summary_labels=summary_labels,
                            summary_weight=summary_weight)]

    @crib_tool("read")
    def note_apropos(query: str, project: str | None = None, k: int = 8,
                tags: list[str] | None = None,
                project_path: str | None = None) -> list[dict[str, Any]]:
        """Like `note_lookup`, but each hit carries the full matching section's
        markdown (`section`) instead of a short snippet — for reading the
        matched sections in full, not just locating them. Carries the same
        `index_rebuilding` incompleteness flag."""
        return crib.apropos(query, project, k, tags)

    @crib_tool("read")
    def note_read(relpath: str, project: str | None = None,
             project_path: str | None = None) -> str:
        """Read a note's full raw markdown (frontmatter + body) — e.g. to see a
        `note_lookup` hit in full context, or before rewriting the note with `note_edit`."""
        return crib.read_note(relpath, project)

    @crib_tool("read")
    def note_locate(relpath: str, project: str | None = None,
               project_path: str | None = None) -> str:
        """Get the real on-disk path of a note so you can edit it with your own
        file tools. After editing, call `note_reindex(relpath)` to make it searchable
        now (the watcher would catch it shortly regardless)."""
        return crib.locate(relpath, project)

    @crib_tool("write", elicit=True)
    async def note_store(content: str, title: str | None = None,
                    project: str | None = None,
                    tags: list[str] | None = None,
                    project_path: str | None = None,
                    ctx: Context | None = None) -> dict[str, Any]:
        """Persist a durable fact to memory — a decision, preference,
        convention, gotcha, or hard-won detail worth recalling in a future
        session. Assigns an id, writes markdown, indexes it. If a related
        note already exists (check with `note_lookup`), prefer `note_append`/`note_edit`
        over creating a near-duplicate.

        PICK THE RIGHT PROJECT — REQUIRED. A fact belongs to the project it is ABOUT,
        which may NOT be the repo you're working in, so a write won't inherit your
        current project: pass `project=` (the subject's project — often `default` or a
        tool's own project for cross-cutting knowledge like a CLI/editor/convention),
        or `project_path=` a path in that repo. If you omit both, you'll be ASKED which
        project (elicitation). Then tell the user which project it landed in."""
        # A write is a ONE-OFF at an explicitly-named target — it must NOT flip the
        # session's current project (no _switch_if_created): storing a cross-cutting
        # fact shouldn't hijack the repo you're working in. `elicit=True` on the
        # declaration asks the client for the target when it's omitted (rather than
        # erroring) and stamps the answer's `project_source`; `ctx` is declared here
        # only so FastMCP injects it for that.
        return await crib.store_note(content, title, project, tags)

    @crib_tool("write")
    async def note_append(relpath: str, content: str, heading: str | None = None,
                     project: str | None = None,
                     project_path: str | None = None) -> dict[str, Any]:
        """Add to an existing note (found via `note_lookup`) — the right call when new
        information extends or continues something already remembered, rather than
        `note_store`-ing a near-duplicate. Optionally files it under a new heading."""
        return await crib.append_note(relpath, content, heading, project)

    @crib_tool("write")
    async def note_edit(relpath: str, new_content: str,
                   project: str | None = None,
                   project_path: str | None = None) -> dict[str, Any]:
        """Rewrite a note's full content — use when remembered information has
        changed, needs correcting, or several notes should be consolidated (read
        it first). Frontmatter (and the note's id/history) is preserved."""
        return await crib.edit_note(relpath, new_content, project)

    @crib_tool("write")
    async def note_forget(relpath: str, project: str | None = None,
                     project_path: str | None = None) -> dict[str, Any]:
        """Delete a note when its information is obsolete or wrong. Removed from
        disk and the index, but stashed to the version ring first, so it stays
        recoverable by id."""
        return await crib.forget(relpath, project)

    @crib_tool("read")
    async def note_reindex(relpath: str | None = None,
                      project: str | None = None,
                      project_path: str | None = None) -> dict[str, Any]:
        """Reindex a note (or the whole project). Call after editing a note via
        its raw path. Safe to call redundantly — it no-ops if already current."""
        return await crib.reindex(relpath, project)

    @crib_tool("read")
    def note_versions(relpath: str, project: str | None = None,
                 project_path: str | None = None) -> list[dict[str, Any]]:
        """List recoverable prior versions of a note."""
        return crib.list_versions(relpath, project)

    @crib_tool("read")
    async def note_restore(relpath: str, version: str,
                      project: str | None = None,
                      project_path: str | None = None) -> dict[str, Any]:
        """Restore a prior version of a note (itself undoable)."""
        return await crib.restore(relpath, version, project)

    @crib_tool("none")
    async def project_reconcile() -> dict[str, Any]:
        """Sweep every project for changes made while crib was down and bring the
        index back in line. Safe to call anytime — the hash gate no-ops anything
        already current."""
        return await crib.reconcile_all()

    @crib_tool("read")
    async def note_distill(relpath: str, project: str | None = None,
                      project_path: str | None = None) -> dict[str, Any]:
        """LLM-revise a note in place: compress, dedupe, normalize — keeping
        facts/decisions, dropping deliberation, preserving code verbatim.
        Thrash-guarded (no-op if unchanged); the prior version is recoverable."""
        return await crib.distill(relpath, project)

    @crib_tool("read")
    async def note_elaborate(label: str, relpath: str | None = None,
                        project: str | None = None, overwrite: bool = False,
                        project_path: str | None = None) -> dict[str, Any]:
        """keyword_index: generate BM25 search terms per section (or whole
        project), section-addressed under `label` (e.g. `keywords`, `questions`,
        `phrase`). Skips cached sections unless `overwrite`. Activate via
        [retrieve].keyword_labels."""
        return await crib.elaborate(label, relpath, project, overwrite=overwrite)

    @crib_tool("read")
    async def note_summarize(label: str, relpath: str | None = None,
                        project: str | None = None, overwrite: bool = False,
                        project_path: str | None = None) -> dict[str, Any]:
        """summary_index: generate LLM rephrasings per section (or whole project),
        embedded as dense alias vectors so paraphrased queries match a section
        with zero shared tokens. Skips cached sections unless `overwrite`.
        Activate via [retrieve].summary_labels."""
        return await crib.summarize(label, relpath, project, overwrite=overwrite)

    @crib_tool("source")
    async def code_index(path: str, project: str | None = None,
                         project_path: str | None = None) -> dict[str, Any]:
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
    async def project_setup(project: str | None = None,
                            project_path: str | None = None) -> dict[str, Any]:
        """ONBOARD a repo for crib in one call — when code_lookup says a project isn't
        indexed, do THIS, don't fall back to grep. Ensures a `.crib` (auto-created with
        sensible defaults if missing), imports the repo's docs into notes, AND indexes
        all its source code (functions/classes/globals/members + call graph +
        references + descriptions). Pass `project_path=<the repo dir>` (a bare
        `project=<name>` works only for an already-indexed project — it resolves the
        recorded root). Idempotent. Then code_lookup/code_dossier work. Code-only
        variant: project_index."""
        return _switch_if_created(
            await crib.project_setup(project, cwd=_cwd(project_path)))

    @crib_tool("source")
    async def project_index(project: str | None = None,
                            project_path: str | None = None,
                            budget_s: float | None = None,
                            ctx: Context | None = None) -> dict[str, Any]:
        """(Re)index a project's SOURCE CODE from its `.crib`, PLUS the prose it
        declares under `docs:` — those are indexed IN-SITU (searchable via note_lookup;
        the repo keeps the only copy). It differs from project_setup only in not
        COPYING files into crib-owned notes. Use to index a repo for
        code_lookup/code_dossier, or to refresh after edits (cheap: unchanged files
        are skipped). Pass
        `project_path=<the repo dir>` (a `.crib` is auto-created if missing); a bare
        `project=<name>` re-indexes an ALREADY-INDEXED project from its recorded root.

        Emits PROGRESS markers ({done,total} files) while the sweep runs, so a long index
        streams live progress and doesn't idle-time-out — it runs to completion in one call.
        (`project_status` also carries the live `indexing` counts.) If your client enforces
        a hard call timeout anyway, pass `budget_s=<seconds>`: files not reached by the
        soft deadline are deferred and the result says `complete=false, remaining=N` —
        re-invoke to continue (finished files re-skip via the content-hash gate)."""
        proj = project                          # resolved by the `source` policy
        before = set(crib.code.sweeps)          # sweeps already running for OTHER calls
        task = asyncio.create_task(
            crib.project_index(proj, cwd=_cwd(project_path), budget_s=budget_s))
        while not task.done():
            # wait, don't sleep: a quick (all-cached) reindex returns immediately
            # instead of eating a full progress interval
            await asyncio.wait({task}, timeout=_PROGRESS_EVERY_S)
            if ctx is None or task.done():
                continue
            # OUR sweep only: the named project's, else the one this call started
            # (proj is None when the repo's .crib names it) — never other projects'.
            sw = (crib.code.sweeps.get(proj) if proj else None) or next(
                (v for p, v in crib.code.sweeps.items() if p not in before), None)
            if sw and sw.get("total"):
                try:
                    await ctx.report_progress(progress=sw["done"], total=sw["total"],
                                              message=f"{sw['done']}/{sw['total']} files")
                except Exception:  # noqa: BLE001 — progress is best-effort
                    pass
        return _switch_if_created(await task)

    @crib_tool("source")
    def project_status(project: str | None = None,
                       project_path: str | None = None) -> dict[str, Any]:
        """Is this repo code-indexed? Returns symbol/file counts, a kind breakdown, and
        the `.crib` source paths — to orient before project_setup / a code_lookup. Pass
        `project_path=<the repo dir>`."""
        return crib.project_status(project, cwd=_cwd(project_path))

    @crib_tool("source")
    def project_forget(project: str | None = None, with_learnings: bool = False,
                       project_path: str | None = None) -> dict[str, Any]:
        """Clear a project's CODE INDEX (symbol_index). Keeps attached learnings, notes
        and `.crib` by default (learnings are durable — pass with_learnings=True to drop
        them too). Recoverable by re-running project_index. Pass `project_path=<the repo dir>`."""
        return crib.project_forget(project, with_learnings=with_learnings,
                                   cwd=_cwd(project_path))

    @crib_tool("source")
    async def project_adopt(project: str | None = None,
                            project_path: str | None = None) -> dict[str, Any]:
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
    async def project_release(project: str | None = None,
                              project_path: str | None = None) -> dict[str, Any]:
        """Move an adopted project's notes back OUT of its repo into the global
        crib store (the inverse of `project_adopt`) — for when the repo is going
        away, or the notes should stop travelling with it. Also the fix when a
        project is listed `unavailable` because its repo isn't on this machine…
        except that the notes have to BE here to move: clone the repo first.
        Idempotent. Pass `project_path=<the repo dir>` or `project=<name>`."""
        return await crib.project_release(project, cwd=_cwd(project_path))

    @crib_tool("source")
    async def project_migrate(project: str | None = None,
                              project_path: str | None = None) -> dict[str, Any]:
        """Move a project's legacy facet notes (`notes/design/`, `notes/plans/`,
        `notes/code-learnings/`) into their sibling pillar stores and requalify
        their citations — the pre-split → split layout migration, on demand.
        Every full reindex runs the same routine automatically, so this verb is
        for driving the move (and reading its report) explicitly. Idempotent;
        name collisions between the two layouts are skipped and reported, never
        merged."""
        return await crib.project_migrate(project, cwd=_cwd(project_path))

    @crib_tool("read", echo=True)
    async def code_xref(symbol: str, project: str | None = None,
                        project_path: str | None = None) -> list[dict[str, Any]]:
        """A symbol's callers (←), callees (→) and references (⇐ — broader than calls),
        plus any human learning pinned to it — from the persisted index, no live LSP.
        `symbol` is a bare name or dotted fqname. Name the project on the call —
        `project_path=<a path in that repo>` or `project=<name>` — whenever you mean
        somewhere other than the current one; naming it always wins."""
        return crib.code_xref(symbol, project)

    @crib_tool("read", echo=True)
    async def code_lookup(query: str, project: str | None = None, k: int = 8,
                          project_path: str | None = None) -> list[dict[str, Any]]:
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
    def code_dossier(symbol: str, project: str | None = None,
                     project_path: str | None = None, path: str = "",
                     scope: str = "", lang: str = "") -> dict[str, Any]:
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
    async def code_graph(symbol: str | None = None, direction: str = "callees",
                         depth: int = 6, project: str | None = None,
                         project_path: str | None = None, shape: str | None = None,
                         group_by: str | None = None, group_depth: int = 0,
                         path: str = "", scope: str = "",
                         lang: str = "", under: str = "",
                         exclude: str = "") -> dict[str, Any]:
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
        return crib.code_graph(symbol, direction, depth, project, shape=shape,
                               group_by=group_by, group_depth=group_depth,
                               path=path, scope=scope, lang=lang, under=under,
                               exclude=exclude)

    @crib_tool("read")
    async def learning_add(symbol: str, text: str, project: str | None = None,
                          project_path: str | None = None) -> dict[str, Any]:
        """Pin a durable human learning to a code symbol — the 'now I get it',
        the subtlety, the gotcha you don't want to re-derive next session. Stored
        as a first-class note in the learnings pillar store keyed to the symbol's
        fqn, SEPARATE from the regenerable LLM description, so it survives
        re-indexing and rides git sync (and works on code you can't edit — vendored
        deps, read-only explorations — where a comment can't go). Appends a dated
        entry to the symbol's running note. `symbol` is a bare name or dotted
        fqname already in the symbol_index (code_index the file first). Surfaces
        back via code_lookup/code_xref. A learning is ABOUT a symbol in the code
        project you're in, so — unlike a note write — it resolves that project the
        way the read tools do (a documented exception; pass `project=`/`project_path=`
        to pin one elsewhere)."""
        return await crib.learning_add(symbol, text, project)

    @crib_tool("read")
    async def learning_edit(symbol: str, new_content: str, project: str | None = None,
                        project_path: str | None = None) -> dict[str, Any]:
        """Rewrite a symbol's learning body wholesale (frontmatter preserved) —
        the standard edit, scoped to a symbol. Errors if none exists; `learning_add`
        creates. Resolves its project like a read, as `learning_add` does."""
        return await crib.learning_edit(symbol, new_content, project)

    @crib_tool("read")
    async def learning_forget(symbol: str, project: str | None = None,
                          project_path: str | None = None) -> dict[str, Any]:
        """Remove a symbol's learning (stashed to the version ring first, so it's
        recoverable) — the standard forget, scoped to a symbol. Resolves its project
        like a read, as `learning_add` does."""
        return await crib.learning_forget(symbol, project)

    @crib_tool("read")
    def learning_read(symbol: str, project: str | None = None,
                  project_path: str | None = None) -> dict[str, Any]:
        """Read a symbol's attached learning note (frontmatter + body), or found=
        False if none is written yet. `symbol` is a bare name or dotted fqname."""
        return crib.learning_read(symbol, project)

    @crib_tool("read")
    async def learning_reaffirm(symbol: str, project: str | None = None,
                            project_path: str | None = None) -> dict[str, Any]:
        """Clear a learning's ⚠︎ stale flag WITHOUT rewriting it — you re-checked the
        note against the current code and it still holds. Re-snapshots the symbol's
        content_hash so it reads as fresh again. Use when code_lookup shows a ※ note
        flagged stale but the understanding is still correct."""
        return await crib.learning_reaffirm(symbol, project)

    @crib_tool("read")
    async def learning_migrate(project: str | None = None,
                               project_path: str | None = None,
                               apply: bool = False) -> dict[str, Any]:
        """Rebind this project's learning notes to the current symbol identity.

        OPTIONAL TIDINESS — the learnings join resolves a note bound to any prior
        spelling, so an unconverted note keeps working forever; this buys canonical
        filenames and `symbol_ref` frontmatter. Per record: `noop` (already
        current), `convert` (rebound, file renamed, old binding kept in
        `symbol_was`), `orphan` (binding answers to nothing — `learning_rehome`
        repairs it), `collision` (two notes claim one symbol: both kept, said out
        loud, never merged). Re-running is the resume.

        DRY RUN unless `apply=True` — it renames files under a git-synced store."""
        return await crib.learning_migrate(project, cwd=_cwd(project_path),
                                           apply=apply)

    @crib_tool("write")
    async def code_convert(project: str | None = None,
                           project_path: str | None = None,
                           apply: bool = False) -> dict[str, Any]:
        """Convert this project's symbol store to the current shape, in place — no
        LSP, no LLM, descriptions/keywords/edges byte-identical or the record is
        left alone and reported. Per record and resumable: re-run after a crash and
        it continues; a record with no `file`/`name` is reported under
        `needs_reindex` instead of guessed at. DRY RUN unless `apply=True`."""
        return await crib.code_convert(project, cwd=_cwd(project_path), apply=apply)

    @crib_tool("read")
    def learning_report(project: str | None = None, orphans_only: bool = False,
                       project_path: str | None = None) -> list[dict[str, Any]]:
        """Health report for attached learnings: each is `ok` | `moved` (fqn resolves
        but the symbol's file drifted) | `orphan` (fqn no longer resolves — a rename/
        move/delete left the note dangling). `orphans_only` filters to the actionable
        ones. Report-only; drives cleanup via `learning_rehome` / `learning_forget`."""
        return crib.learning_report(project, orphans_only=orphans_only)

    @crib_tool("read")
    async def learning_rehome(old_fqn: str, new_fqn: str | None = None,
                          project: str | None = None,
                          project_path: str | None = None) -> dict[str, Any]:
        """Re-point an orphaned learning at the symbol it became. Call with just
        `old_fqn` FIRST to get ranked candidate targets (name/signature/file signals);
        then call again with the chosen `new_fqn` to move the note (id/history
        preserved, frontmatter re-snapshotted). Never auto-moves — you pick, because a
        wrong attach is worse than a dangling one."""
        return await crib.learning_rehome(old_fqn, new_fqn, project)

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

    @crib_tool("write")
    async def design_add(title: str, content: str, deps: list[str] | None = None,
                         project: str | None = None,
                         sources: list[str] | None = None, proposed: bool = False,
                         project_path: str | None = None) -> dict[str, Any]:
        """Record a DESIGN DECISION — the choice, why, and what was rejected — as a
        note under `design/`, declaring the decisions it builds on (`deps`: ids,
        relpaths or titles).

        CUE: a design question just got SETTLED ("chunks are keyed by X", "writes
        must name their project"). Reach for this then, so the next session sees
        not just what the code does but what it must keep doing. A dep is a
        promise — *if that changes, reconsider this* — and a plain note cannot make
        that promise.

        CONTRACT: staleness is computed from dep body hashes, so `checked` is
        seeded from the deps as they read right now and a new decision is born
        verified; if a dep later changes by ANY path, `design_check` says so.
        Body required (the rationale is the artifact). The result carries
        `similar` — near-duplicate decisions FORK THE GRAPH, so if one comes back,
        prefer `design_append`/`design_edit` on it. Like any write it must NAME its
        project (`project=`/`project_path=`).

        `sources` cites where the decision came FROM — `["docs/DESIGN.md#4.
        Coordination"]`, a doc path plus a unique heading-path suffix. It names a
        SECTION, never a whole doc (that is refused, listing the doc's headings —
        whole-file attribution would re-check this on any edit anywhere in the
        file); the only exception is a doc with no headings at all. Attribution
        edges check like deps (a changed section taints this) but never gate work;
        the citation records that section's hash now. `proposed=True` is the
        EXTRACTION tier and belongs to `design_import`'s procedure — hand-authored
        decisions land `active`, because you already made the judgement."""
        return await crib.design_add(title, content, deps, project, sources,
                                     proposed)

    @crib_tool("read")
    def design_read(ref: str, project: str | None = None,
                    project_path: str | None = None) -> dict[str, Any]:
        """A decision's DOSSIER in one call — body, status, every dep and dependent
        annotated (title, status, tainted?), and this decision's own taint with the
        chains explaining it. The `code_dossier` of the design facet.

        CUE: about to read, cite, or change a decision. Do this INSTEAD of
        `note_read` on a path under `design/` — same prose, plus what it rests on,
        what rests on it, and whether either moved under you. The file cannot tell
        you that; only the graph can.

        CONTRACT: taint is computed live from dep body hashes — an edit by any path
        (a facet verb, a raw file write, another agent, a git pull) is caught, so
        never track staleness yourself. When this decision is tainted the result
        ends with the verb to run next. Resolves its project like a read."""
        return crib.design_read(ref, project)

    @crib_tool("read")
    async def design_edit(ref: str, new_content: str, project: str | None = None,
                          sources: list[str] | None = None,
                          project_path: str | None = None) -> dict[str, Any]:
        """Rewrite a decision's body THROUGH THE FACET, and get back the causal
        consequences: `newly_tainted` lists every decision your change just put out
        of date, each with the chain that explains it — computed against the
        pre-edit state.

        CUE: changing a decision. PREFER THIS OVER `note_edit` for anything under
        `design/` — `note_edit` writes the same bytes but tells you nothing, and
        the whole point of recording a decision was to learn what a change to it
        implicates. Read it first (`design_read`).

        CONTRACT: hash-taint is the safety net for edits made by any other route,
        so nothing is lost by editing a decision as a raw file — but only this path
        can name the consequences in the same breath as the change. `sources`, when
        given, REPLACES the decision's citations (re-captured at their current
        section hashes); omitted, they are left alone. Resolves its project like a
        read, as `design_dep_add` does."""
        return await crib.design_edit(ref, new_content, project, sources)

    @crib_tool("read")
    async def design_append(ref: str, content: str, project: str | None = None,
                            project_path: str | None = None,
                            sources: list[str] | None = None) -> dict[str, Any]:
        """Extend a decision through the facet — the same edge-aware answer as
        `design_edit` (`newly_tainted` + chains), for when new information EXTENDS
        a decision rather than replacing it.

        CUE: you were about to `design_add` a decision that qualifies or elaborates
        one that already exists. Append to that one instead; a near-duplicate
        decision forks the graph and splits its dependents between two records.
        Resolves its project like a read.

        `sources` ADDS doc-section citations to the decision (deduped; existing
        ones keep their capture-time hashes) — the post-hoc wire for the common
        node-first-doc-later sequence, where the doc of record grows AFTER the
        decision was written and its edits would otherwise re-check plan items
        but silently miss the decision itself. Same `doc.md#Heading` spelling as
        `design_add`; `design_edit(sources=)` is the replace-everything form."""
        return await crib.design_append(ref, content, project, sources=sources)

    @crib_tool("read")
    def design_lookup(query: str, project: str | None = None, k: int = 8,
                      project_path: str | None = None) -> list[dict[str, Any]]:
        """Semantic search scoped to DECISIONS, each hit annotated with what
        decides whether to trust it: `status`, `tainted`, and dep/dependent counts.

        CUE: about to answer "why is it built this way", or to propose an
        architecture change. Search the decisions before reconstructing them from
        the code — and note the `tainted` flag: a stale decision is exactly the one
        you must not quietly reason from. Follow a hit with `design_read <ref>`.

        CONTRACT: taint is computed live from dep body hashes; `tainted: true` here
        means a dep moved and nobody has re-read this since. Resolves its project
        like a read."""
        return crib.design_lookup(query, project, k)

    @crib_tool("read")
    def design_list(tainted: bool = False, project: str | None = None,
                    project_path: str | None = None) -> dict[str, Any]:
        """Every decision as a flat table — title, ref, status, taint flag, edge
        counts. The inventory read (`design_tree` is the shape read); `tainted=True`
        filters to the stale ones, i.e. your work queue of decisions to re-read.
        Resolves its project like a read."""
        return crib.design_list(tainted, project)

    @crib_tool("read")
    async def design_dep_add(ref: str, dep_ref: str, project: str | None = None,
                             project_path: str | None = None) -> dict[str, Any]:
        """Declare that one decision BUILDS ON another (cycle-checked, refused if it
        would create one).

        CONTRACT: EVERY dep edge propagates checking — there is no "informed-by"
        edge that never taints, because that is precisely the hole through which an
        origin changes silently. The new edge is deliberately left unverified, so
        `ref` shows up in `design_check` until you re-read it against its new dep.
        Keyed by a ref inside one project, so it resolves that project like a
        read."""
        return await crib.design_dep_add(ref, dep_ref, project)

    @crib_tool("read")
    async def design_dep_remove(ref: str, dep_ref: str, project: str | None = None,
                                project_path: str | None = None) -> dict[str, Any]:
        """Drop a dependency edge between two decisions (the edge was wrong, or the
        decision no longer rests on it) — which also drops the checking that edge
        carried. Resolves its project like a read, as `design_dep_add` does."""
        return await crib.design_dep_remove(ref, dep_ref, project)

    @crib_tool("read")
    async def design_forget(ref: str, force: bool = False,
                            project: str | None = None,
                            project_path: str | None = None) -> dict[str, Any]:
        """Delete a decision (recoverable via the version ring). REFUSES while other
        decisions depend on it, listing them — `force=True` deletes anyway and
        leaves those dependents tainted, pointing at a missing dep. Prefer
        `design_supersede` when the decision was replaced rather than mistaken.
        Resolves its project like a read, as `design_dep_add` does."""
        return await crib.design_forget(ref, force, project)

    @crib_tool("read")
    def design_check(ref: str | None = None, project: str | None = None,
                     project_path: str | None = None) -> dict[str, Any]:
        """WHICH DECISIONS ARE NOW OUT OF DATE, and what to do about each.

        CUE: BEFORE you change a decision, or change code that implements one —
        and again after. This is the call that turns an unexpected consequence
        into an expected one.

        Each tainted entry carries its ref + title, the chain that explains it
        (`X → Y`, Y being what actually changed), the CHANGE KIND
        (`dep-edited` | `dep-superseded` | `dep-deleted` | `new-unverified-edge`),
        the dep's `updated` date, and ends with `next` — the verb to run.

        CONTRACT: computed live from the deps' current bodies, so an edit by ANY
        route (a facet verb, `note_edit`, your own editor, a git pull) is caught —
        never track changes yourself, just call this. Taint is COARSE: it means a
        dep moved, NOT that the decision is wrong. The normal outcome of reading a
        tainted decision is `design_reaffirm`, which is cheap and expected — not
        error recovery."""
        return crib.design_check(ref, project)

    @crib_tool("read")
    async def design_reaffirm(ref: str, project: str | None = None,
                              project_path: str | None = None) -> dict[str, Any]:
        """Re-record a decision's dep hashes — 'I re-read this against what changed
        and it still holds'. The only thing that clears taint short of rewriting
        the decision, so only call it after actually re-reading (`design_read`).

        Named as `learning_reaffirm` is, and meaning the same thing: a re-blessing
        against drift, not a proof. Because taint is coarse — a dep moved — this is
        the NORMAL, cheap ending for most tainted decisions; a trivial change to a
        dep should end here, not in soul-searching. When it no longer holds, that's
        `design_supersede`. Resolves its project like a read, as `design_dep_add`
        does. Re-records the decision's SOURCE hashes alongside its dep hashes —
        both are what "I re-read this and it still holds" is a statement about."""
        return await crib.design_reaffirm(ref, project)

    @crib_tool("read")
    async def design_promote(ref: str, project: str | None = None,
                             project_path: str | None = None) -> dict[str, Any]:
        """Promote an EXTRACTED decision: `proposed` → `active`. The human act
        that turns something an import proposed into settled ground.

        CUE: you (or the maintainer) have read a `proposed` entry against its
        sources and it is right. Until then a proposed decision taints nothing —
        so nothing inherits authority it hasn't earned — and it GATES any plan item
        that depends on it, because unpromoted ground is unstable ground.

        Seeds `checked` and the source hashes FRESH, so the entry becomes
        authoritative as of the graph and docs as they read at the moment it was
        confirmed. Resolves its project like a read, as `design_dep_add` does."""
        return await crib.design_promote(ref, project)

    @crib_tool("read")
    def design_import(relpath: str, project: str | None = None,
                      project_path: str | None = None) -> dict[str, Any]:
        """Capture a DESIGN DOC into the decision graph — the doc split into
        sections (each with its current `section_hash`, ready to cite verbatim),
        the entries that already cite it, and THE EXTRACTION PROCEDURE to follow,
        returned as this result's `instruction`.

        CUE: asked to capture a design doc, spec or architecture note into the
        graph ("get DESIGN.md into crib", "record the decisions in this doc").

        IT RUNS NO MODEL, WRITES NOTHING, AND INTERPRETS NOTHING — no guess at
        which passages are decisions, no summarizing. YOU do the reading and all
        the judgement; this hands you the one thing you can't compute yourself
        (the section hashes taint checking compares against, so a citation is
        correct by construction) and then the steps. Follow `instruction`:
        extracted decisions
        land `proposed` (quarantine tier) with `sources` citing the exact section,
        and `design_promote` is what makes each one real. `relpath` may be a note
        relpath or the repo-relative path of a doc indexed in situ (`DESIGN.md`).
        Resolves its project like a read."""
        return crib.design_import(relpath, project)

    @crib_tool("read")
    def plan_import(relpath: str, project: str | None = None,
                    project_path: str | None = None) -> dict[str, Any]:
        """Capture a PLAN DOC into the plan facet — the same shape as
        `design_import` (sections + hashes + existing citations + procedure), for
        turning a doc's actionable passages into plan items that cite them.

        CUE: asked to put a plan file, a spec's work list, or a TODO doc into the
        graph. Runs no model and writes nothing: follow the returned
        `instruction`, which ends in ONE batch `plan_add` whose items carry
        intra-batch deps and `sources`. A finished item whose cited passage later
        changes shows `revisit` in `plan_list --all` — the graph reports, it never
        re-opens a status. Resolves its project like a read."""
        return crib.plan_import(relpath, project)

    @crib_tool("read")
    def design_tree(ref: str | None = None, direction: str = "deps", depth: int = 6,
                    project: str | None = None,
                    project_path: str | None = None) -> dict[str, Any]:
        """The dependency TREE around a decision: `deps` (what it builds on) or
        `dependents` (what would be affected if you changed it) — the read to do
        BEFORE touching a decision, or before proposing an architecture change.
        Every node is taint-flagged. Without `ref`, renders every root.
        (`design_read` is the same picture for ONE decision, plus its body.)"""
        return crib.design_tree(ref, direction, depth, project)

    @crib_tool("read", echo=True)
    def design_graph(project: str | None = None, project_path: str | None = None,
                     sources: bool = False) -> dict[str, Any]:
        """The whole DECISION MAP as {nodes, edges} — for rendering or computing
        over, the same shape as code_graph's edge export: every node carries `id`
        (the pasteable `design:x.md` ref) and `name` (the title); every edge
        endpoint is declared. Edge kinds: `dep`, `superseded_by`. `tainted` on a
        node is live — the ground under that decision moved. `sources=True` adds
        doc-section attribution nodes and `source` edges."""
        return crib.design_graph(project, cwd=_cwd(project_path), sources=sources)

    @crib_tool("read", echo=True)
    def plan_graph(project: str | None = None, project_path: str | None = None,
                   sources: bool = False) -> dict[str, Any]:
        """The PLAN as {nodes, edges}, including the design decisions items rest
        on (those deps gate: an item drops out of plan_next while its decision is
        tainted). Same consumer contract as code_graph / design_graph. Edge kinds:
        `dep`, `superseded_by`; note-deps appear as lean external nodes."""
        return crib.plan_graph(project, cwd=_cwd(project_path), sources=sources)

    @crib_tool("read")
    async def design_supersede(ref: str, by_ref: str | None = None,
                               project: str | None = None,
                               project_path: str | None = None) -> dict[str, Any]:
        """Soft-delete a decision that was REPLACED (name the replacement with
        `by_ref`): marks it superseded, keeps it readable as history, and taints
        everything that built on it so those get re-checked — those dependents come
        back from `design_check` with change kind `dep-superseded`.

        This is where a tainted decision goes when re-reading shows it NO LONGER
        HOLDS; when it still holds, that's `design_reaffirm`. Resolves its project
        like a read, as `design_dep_add` does."""
        return await crib.design_supersede(ref, by_ref, project)

    @crib_tool("write")
    async def plan_add(title: str | None = None, content: str = "",
                       deps: list[str] | None = None, after: str | None = None,
                       before: str | None = None,
                       items: list[dict[str, Any]] | None = None,
                       project: str | None = None,
                       sources: list[str] | None = None,
                       project_path: str | None = None) -> dict[str, Any]:
        """Add durable PLAN ITEMS so multi-session work survives a context reset.

        CUE: you are about to write a todo list — into a file, into the chat, or
        into your own head. Put it here instead: `plan_next` resumes it in any
        later session, yours or another agent's, and an in-chat list dies with the
        chat.

        One item (`title`, optional `content`) or a BATCH: `items=[{title,
        content?, deps?}, …]`, added contiguously in order. A batch item may depend
        on an EARLIER item of the same batch by position (`deps=["#1"]`) as well as
        on any existing ref — which is what you usually mean, since the item you
        just wrote has no id yet. A plan item's body is OPTIONAL (a title is a
        legitimate whole item); a design decision's is not.

        `deps` are must-precede refs (plan or design); `after`/`before` place the
        batch in the order (default: end). Order is preference, deps are
        correctness. `sources` cites the doc SECTION an item came from
        (`["docs/plans/x.md#Tier 2"]`, also settable per batch item; a bare doc is
        refused) — a finished item whose cited passage later changes flags
        `revisit` rather than re-opening itself. Like any write it must NAME its
        project."""
        return await crib.plan_add(title, content, deps, after, before, items,
                                   project, sources)

    @crib_tool("read")
    async def plan_reaffirm(ref: str, project: str | None = None,
                            project_path: str | None = None) -> dict[str, Any]:
        """Re-record a plan item's dep AND source hashes — 'I re-read what moved
        and this item still stands against it.' The plan-side twin of
        `design_reaffirm`, and what clears a benign taint on an item whose DESIGN
        dep was edited/reaffirmed: without it the item sits blocked in `plan_next`
        (a design dep blocks while tainted) with no verb but the dep_remove/
        dep_add dance. A claim about the item's GROUND; `plan_status` is the claim
        about the WORK. Resolves its project like a read, as `plan_status` does."""
        return await crib.plan_reaffirm(ref, project)

    @crib_tool("read")
    async def plan_status(ref: str, status: str, project: str | None = None,
                          project_path: str | None = None) -> dict[str, Any]:
        """Move an item along: `todo` | `in-progress` | `done` | `verified` — and
        find out what that just freed.

        Completing an item answers with `unblocked`: the items whose deps are now
        all satisfied. That is the plan-side mirror of `design_edit`'s
        `newly_tainted` — finishing work is an edge event, so the next actionable
        step arrives with the completion instead of waiting to be asked for.

        CUE: as you go, not at the end — a resumed session (or another agent) reads
        this. `in-progress` is a CLAIM: it takes the item out of everyone else's
        `plan_next`. (`blocked` is DERIVED from deps and never set here.) Marking
        done with unfinished deps warns rather than blocks. Keyed by a ref inside
        one project, so it resolves that project like a read.

        A status write also re-records the item's SOURCE hashes — a status is a
        statement about the work as of now, so re-running it after re-reading a
        changed source is what clears a `revisit` flag."""
        return await crib.plan_status(ref, status, project)

    @crib_tool("read")
    def plan_lookup(query: str, project: str | None = None, k: int = 8,
                    project_path: str | None = None) -> list[dict[str, Any]]:
        """Semantic search scoped to PLAN ITEMS, each hit annotated with `status`,
        `tainted` and dep/dependent counts.

        CUE: "was this already planned?" — before adding an item, and when picking
        up work described in prose rather than by ref. Resolves its project like a
        read."""
        return crib.plan_lookup(query, project, k)

    @crib_tool("read")
    async def plan_dep_add(ref: str, dep_ref: str, project: str | None = None,
                           project_path: str | None = None) -> dict[str, Any]:
        """Declare that one plan item MUST FOLLOW another (cycle-checked). This is
        what makes `plan_next` trustworthy. Resolves its project like a read, as
        `plan_status` does."""
        return await crib.plan_dep_add(ref, dep_ref, project)

    @crib_tool("read")
    async def plan_dep_remove(ref: str, dep_ref: str, project: str | None = None,
                              project_path: str | None = None) -> dict[str, Any]:
        """Drop a must-precede edge between plan items. Resolves its project like a
        read, as `plan_status` does."""
        return await crib.plan_dep_remove(ref, dep_ref, project)

    @crib_tool("read")
    async def plan_forget(ref: str, force: bool = False, project: str | None = None,
                          project_path: str | None = None) -> dict[str, Any]:
        """Delete a plan item that is no longer wanted (recoverable via the ring).
        REFUSES while other items depend on it, listing them; `force=True` deletes
        anyway. Prefer `plan_status(done)` for work that actually happened.
        Resolves its project like a read, as `plan_status` does."""
        return await crib.plan_forget(ref, force, project)

    @crib_tool("read")
    async def plan_move(ref: str, after: str | None = None, before: str | None = None,
                        project: str | None = None,
                        project_path: str | None = None) -> dict[str, Any]:
        """Re-order a plan item (rank only — deps are untouched, so a move can never
        break correctness). Use `plan_dep_add` when the order is a real constraint,
        this when it's just preference. Resolves its project like a read."""
        return await crib.plan_move(ref, after, before, project)

    @crib_tool("read")
    def plan_list(all: bool = False, project: str | None = None,
                  project_path: str | None = None) -> dict[str, Any]:
        """THE PLAN as a working set: in-progress items first, then ready, then
        blocked (each naming what it waits on), finished hidden unless `all`.
        Topological + rank order holds within each group.

        Read this when picking up work — it's the persistent, resumable version of
        a todo list.

        CONTRACT (mixed deps): a **plan** dep blocks until it is done/verified; a
        **design** dep blocks only while it is TAINTED (an untainted decision is
        stable ground; a tainted one means the ground moved); a plain **note** dep
        never blocks — it is a reference, not a gate."""
        return crib.plan_list(all, project)

    @crib_tool("read")
    def plan_next(k: int = 5, project: str | None = None,
                  project_path: str | None = None) -> dict[str, Any]:
        """What to do NEXT: `todo` items nothing blocks, in order. The one-call
        'where was I' at the start of a session.

        EXCLUDES `in-progress` items by design — in-progress means CLAIMED, and
        several agents may read one plan. Each returned item carries the loop to
        run: mark it `in-progress` when you take it (that is how the claim becomes
        visible), `done` when you finish (the result names what you unblocked).

        CONTRACT (mixed deps): plan deps block until done; design deps block while
        tainted; note deps never block."""
        return crib.plan_next(k, project)

    @crib_tool("none")
    def memory_snapshot(message: str | None = None) -> str:
        """Create a git checkpoint of the whole memory store's data tree (if git is set up)."""
        return crib.snapshot(message)

    @crib_tool("none")
    def memory_history(relpath: str | None = None) -> list[str]:
        """Show git commit history for the whole tree (or a single note)."""
        return crib.history(relpath)

    @crib_tool("source", needs_target=True)
    async def note_import(paths: list[str], project: str | None = None,
                           project_path: str | None = None) -> dict[str, Any]:
        """Copy the NAMED files into memory as crib-owned notes (snapshot you own:
        git-synced, editable, versioned). Distinct from a repo's `.crib` docs, which
        are indexed IN-SITU (source is master, never copied) by `project index`.
        `paths` must be absolute, or relative to `project_path` (there is no shell
        cwd here for them to be relative to). An import is ABOUT a repo, so — unlike
        a note write — it resolves like the repo-scoped `project_*` tools and REQUIRES
        a source: name `project_path=<the repo dir>` (or `project=`), else it errors
        rather than quietly filing the copies in the default project."""
        return _switch_if_created(
            await crib.import_files(paths, project, cwd=_cwd(project_path)))

    @crib_tool("source", needs_target=True)
    async def note_import_memory(project: str | None = None,
                            project_path: str | None = None) -> dict[str, Any]:
        """Mirror Claude Code's own harness memory (the `memory/*.md` files it
        writes for this project) into a crib project, so those notes become
        searchable here alongside everything else. One-way + idempotent; opts the
        repo into the daemon's live mirror so future memory edits sync on their
        own. REQUIRES a source repo — pass `project_path=<the repo dir>` (or
        `project=`); like `note_import` it resolves repo-scoped, never from the
        sticky session or the default project."""
        return _switch_if_created(
            await crib.import_claude_memory(project, cwd=_cwd(project_path)))

    @crib_tool("write")
    async def note_move(relpath: str, to_project: str | None = None,
                   to_relpath: str | None = None, project: str | None = None,
                   project_path: str | None = None) -> dict[str, Any]:
        """Relocate a note to another project and/or rename it, preserving its id
        and version history (the curation primitive — not store-new + forget-old).
        `to_project` moves it across namespaces; `to_relpath` renames it."""
        # a move is a one-off curation op — it must not flip the session project
        # onto the source or destination (no _switch_if_created).
        return await crib.move_note(relpath, to_project, to_relpath, project)

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


async def _serve_async(transport: str = "stdio", host: str = "127.0.0.1",
                       port: int = 8787) -> None:
    crib = Crib.open()
    mcp = build_server(crib)
    # Watcher runs on THIS loop so its index_file calls share the per-path locks
    # with the tool calls (DESIGN §4) — correctness depends on one loop.
    if crib.config.watch:
        try:
            crib.start_watchers(asyncio.get_running_loop())
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
                        BearerAuthMiddleware, token=_tok, is_protected=lambda p: p != "/health"
                    )
                )
            await mcp.run_async(transport="http", host=host, port=port, middleware=_mw)
    finally:
        crib.close()


def main(transport: str = "stdio", host: str = "127.0.0.1",
         port: int = 8787) -> None:
    asyncio.run(_serve_async(transport, host, port))


if __name__ == "__main__":
    main()
