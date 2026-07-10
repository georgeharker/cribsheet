"""FastMCP server exposing the crib tool surface (DESIGN §5).

Lazy-imports fastmcp so the package stays importable without it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from .app import Crib
from .session import ProjectResolution, resolve_session_project, session_state

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
    """Stamp an IMPLICIT resolution onto a dict result (a non-breaking extra key),
    so an agent that didn't name a project can see which one — and how — answered."""
    if isinstance(out, dict) and res.implicit:
        out.setdefault("resolved", res.echo())
    return out


def _echo_list(hits: Any, res: ProjectResolution) -> Any:
    """Surface an IMPLICIT resolution on a LIST result where it would otherwise be
    invisible: an EMPTY result from a sticky/seeded project is indistinguishable
    from 'answered the wrong project', so return one diagnostic marker instead of a
    bare `[]`. Non-empty lists already tag each hit with its owning `project`."""
    if res.implicit and isinstance(hits, list) and not hits:
        return [{"resolved": res.echo(), "matches": 0,
                 "note": (f"resolved implicitly to {res.project!r} via {res.via}; "
                          "0 matches. If you meant another project pass "
                          "project=<name> or project_path=<a path in that repo>.")}]
    return hits


def _source_project(crib: Crib, project: str | None,
                    project_path: str | None) -> str | None:
    """Project selector for REPO-SCOPED ops (project_setup/index/status/forget).

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
    raise ValueError(
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
            "for the codebase you're working in — `project_use <name>`, or it's inferred "
            "from `project_path` on your first code call — then reads need no project args. "
            "To look up a DIFFERENT project (e.g. a related codebase you're referencing), "
            "you MUST name it: `project=<name>` or `project_path=<a path inside that repo>`. "
            "`project_path` is NOT your shell cwd — it just identifies which repo you mean. "
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

    def write_tool(fn):
        """Register a notes-WRITE tool with a wire-schema constraint that `project`
        OR `project_path` must be supplied (a top-level JSON-Schema `anyOf` on
        `required`), so a validating client sees the requirement up front — not just
        the runtime `_write_project` guard. `add_tool` returns the FunctionTool, whose
        `.parameters` dict is the served input schema."""
        tool = mcp.add_tool(FunctionTool.from_function(fn))
        tool.parameters.setdefault(
            "anyOf", [{"required": ["project"]}, {"required": ["project_path"]}])
        return fn

    @mcp.tool()
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
        `keyword_labels`/`keyword_weight` (BM25 keyword_index) and
        `summary_labels` (dense summary_index aliases) override which LLM index
        sets feed retrieval (default from config); mainly for eval sweeps."""
        return [vars(h) for h in
                crib.lookup(query, _project(crib, project, project_path), k, tags,
                            keyword_labels=keyword_labels,
                            keyword_weight=keyword_weight,
                            summary_labels=summary_labels,
                            summary_weight=summary_weight)]

    @mcp.tool()
    def note_apropos(query: str, project: str | None = None, k: int = 8,
                tags: list[str] | None = None,
                project_path: str | None = None) -> list[dict[str, Any]]:
        """Like `note_lookup`, but each hit carries the full matching section's
        markdown (`section`) instead of a short snippet — for reading the
        matched sections in full, not just locating them."""
        return crib.apropos(query, _project(crib, project, project_path), k, tags)

    @mcp.tool()
    def note_read(relpath: str, project: str | None = None,
             project_path: str | None = None) -> str:
        """Read a note's full raw markdown (frontmatter + body) — e.g. to see a
        `note_lookup` hit in full context, or before rewriting the note with `note_edit`."""
        return crib.read_note(relpath, _project(crib, project, project_path))

    @mcp.tool()
    def note_locate(relpath: str, project: str | None = None,
               project_path: str | None = None) -> str:
        """Get the real on-disk path of a note so you can edit it with your own
        file tools. After editing, call `reindex(relpath)` to make it searchable
        now (the watcher would catch it shortly regardless)."""
        return crib.locate(relpath, _project(crib, project, project_path))

    @write_tool
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
        # fact shouldn't hijack the repo you're working in. When the target is omitted,
        # elicit it from the client rather than erroring outright.
        proj = await _write_project_elicit(crib, project, project_path, ctx)
        res = await crib.store_note(content, title, proj, tags)
        if isinstance(res, dict):
            res["project_source"] = ("explicit" if project else
                                     "project_path" if project_path else "elicited")
        return res

    @write_tool
    async def note_append(relpath: str, content: str, heading: str | None = None,
                     project: str | None = None,
                     project_path: str | None = None) -> dict[str, Any]:
        """Add to an existing note (found via `note_lookup`) — the right call when new
        information extends or continues something already remembered, rather than
        `note_store`-ing a near-duplicate. Optionally files it under a new heading."""
        return await crib.append_note(relpath, content, heading,
                                      _write_project(crib, project, project_path))

    @write_tool
    async def note_edit(relpath: str, new_content: str,
                   project: str | None = None,
                   project_path: str | None = None) -> dict[str, Any]:
        """Rewrite a note's full content — use when remembered information has
        changed, needs correcting, or several notes should be consolidated (read
        it first). Frontmatter (and the note's id/history) is preserved."""
        return await crib.edit_note(relpath, new_content,
                                    _write_project(crib, project, project_path))

    @write_tool
    async def note_forget(relpath: str, project: str | None = None,
                     project_path: str | None = None) -> dict[str, Any]:
        """Delete a note when its information is obsolete or wrong. Removed from
        disk and the index, but stashed to the version ring first, so it stays
        recoverable by id."""
        return await crib.forget(relpath, _write_project(crib, project, project_path))

    @mcp.tool()
    async def note_reindex(relpath: str | None = None,
                      project: str | None = None,
                      project_path: str | None = None) -> dict[str, Any]:
        """Reindex a note (or the whole project). Call after editing a note via
        its raw path. Safe to call redundantly — it no-ops if already current."""
        return await crib.reindex(relpath, _project(crib, project, project_path))

    @mcp.tool()
    def note_versions(relpath: str, project: str | None = None,
                 project_path: str | None = None) -> list[dict[str, Any]]:
        """List recoverable prior versions of a note."""
        return crib.list_versions(relpath, _project(crib, project, project_path))

    @mcp.tool()
    async def note_restore(relpath: str, version: str,
                      project: str | None = None,
                      project_path: str | None = None) -> dict[str, Any]:
        """Restore a prior version of a note (itself undoable)."""
        return await crib.restore(relpath, version, _project(crib, project, project_path))

    @mcp.tool()
    async def project_reconcile() -> dict[str, Any]:
        """Sweep every project for changes made while crib was down and bring the
        index back in line. Safe to call anytime — the hash gate no-ops anything
        already current."""
        return await crib.reconcile_all()

    @mcp.tool()
    async def note_distill(relpath: str, project: str | None = None,
                      project_path: str | None = None) -> dict[str, Any]:
        """LLM-revise a note in place: compress, dedupe, normalize — keeping
        facts/decisions, dropping deliberation, preserving code verbatim.
        Thrash-guarded (no-op if unchanged); the prior version is recoverable."""
        return await crib.distill(relpath, _project(crib, project, project_path))

    @mcp.tool()
    async def note_elaborate(label: str, relpath: str | None = None,
                        project: str | None = None, overwrite: bool = False,
                        project_path: str | None = None) -> dict[str, Any]:
        """keyword_index: generate BM25 search terms per section (or whole
        project), section-addressed under `label` (e.g. `keywords`, `questions`,
        `phrase`). Skips cached sections unless `overwrite`. Activate via
        [retrieve].keyword_labels."""
        return await crib.elaborate(label, relpath, _project(crib, project, project_path),
                                    overwrite=overwrite)

    @mcp.tool()
    async def note_summarize(label: str, relpath: str | None = None,
                        project: str | None = None, overwrite: bool = False,
                        project_path: str | None = None) -> dict[str, Any]:
        """summary_index: generate LLM rephrasings per section (or whole project),
        embedded as dense alias vectors so paraphrased queries match a section
        with zero shared tokens. Skips cached sections unless `overwrite`.
        Activate via [retrieve].summary_labels."""
        return await crib.summarize(label, relpath, _project(crib, project, project_path),
                                    overwrite=overwrite)

    @mcp.tool()
    async def code_index(path: str, project: str | None = None,
                         project_path: str | None = None) -> dict[str, Any]:
        """Populate the code index for a source file: extract its symbols (functions,
        classes, globals, class members) + call graph + references via the LSP,
        describe them, persist under `<project>/symbol_index/`. Use when code_lookup
        says a project isn't indexed yet. `path` MUST be ABSOLUTE — a relative path
        resolves against the daemon's cwd (not yours) and fails; also pass
        `project_path=<your working dir>` so the project resolves via .crib."""
        return await crib.code_index(path, _project(crib, project, project_path), cwd=_cwd(project_path))

    @mcp.tool()
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
            await crib.project_setup(_source_project(crib, project, project_path),
                                     cwd=_cwd(project_path)))

    @mcp.tool()
    async def project_index(project: str | None = None,
                            project_path: str | None = None) -> dict[str, Any]:
        """(Re)index a project's SOURCE CODE from its `.crib` (code facet of
        project_setup — no doc import). Use to index a repo for code_lookup/code_dossier,
        or to refresh after edits (cheap: unchanged files are skipped). Pass
        `project_path=<the repo dir>` (a `.crib` is auto-created if missing); a bare
        `project=<name>` re-indexes an ALREADY-INDEXED project from its recorded
        root — an unknown name errors rather than guessing a directory."""
        return _switch_if_created(
            await crib.project_index(_source_project(crib, project, project_path),
                                     cwd=_cwd(project_path)))

    @mcp.tool()
    def project_status(project: str | None = None,
                       project_path: str | None = None) -> dict[str, Any]:
        """Is this repo code-indexed? Returns symbol/file counts, a kind breakdown, and
        the `.crib` source paths — to orient before project_setup / a code_lookup. Pass
        `project_path=<the repo dir>`."""
        return crib.project_status(_source_project(crib, project, project_path),
                                   cwd=_cwd(project_path))

    @mcp.tool()
    def project_forget(project: str | None = None, with_learnings: bool = False,
                       project_path: str | None = None) -> dict[str, Any]:
        """Clear a project's CODE INDEX (symbol_index). Keeps attached learnings, notes
        and `.crib` by default (learnings are durable — pass with_learnings=True to drop
        them too). Recoverable by re-running project_index. Pass `project_path=<the repo dir>`."""
        return crib.project_forget(_source_project(crib, project, project_path),
                                   with_learnings=with_learnings, cwd=_cwd(project_path))

    @mcp.tool()
    async def code_xref(symbol: str, project: str | None = None,
                        project_path: str | None = None) -> list[dict[str, Any]]:
        """A symbol's callers (←), callees (→) and references (⇐ — broader than calls),
        plus any human learning pinned to it — from the persisted index, no live LSP.
        `symbol` is a bare name or dotted fqname. Pass `project_path=<your working dir>` on first
        use so the right project resolves (via .crib)."""
        res = _resolve(crib, project, project_path)
        return _echo_list(crib.code_xref(symbol, res.project), res)

    @mcp.tool()
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
        Pass `project_path=<your working dir>` so the project resolves via .crib. Then `code_dossier`
        a hit to go deep, or `code_graph` to walk the tree."""
        res = _resolve(crib, project, project_path)
        return _echo_list(crib.code_lookup(query, res.project, k), res)

    @mcp.tool()
    def code_dossier(symbol: str, project: str | None = None,
                     project_path: str | None = None) -> dict[str, Any]:
        """EVERYTHING about one symbol in a single call: signature + description, and its
        callers/callees/references EACH annotated with the NEIGHBOUR'S own description,
        plus any pinned learning. The efficient way to *understand* a symbol (vs
        code_lookup which *finds* it) — read a symbol and its whole neighbourhood without
        follow-up lookups. `symbol` is a bare name or dotted fqname; pass `project_path=`/`project=` to target a DIFFERENT project than your current
        project resolution."""
        res = _resolve(crib, project, project_path)
        return _echo_dict(crib.code_dossier(symbol, res.project), res)

    @mcp.tool()
    async def code_graph(symbol: str, direction: str = "callees", depth: int = 6,
                         project: str | None = None,
                         project_path: str | None = None) -> dict[str, Any]:
        """Call-graph TREE around a symbol from the index: `callees` (what it calls),
        `callers` (what calls it), or `references` (everywhere mentioned — broader than
        calls, and the only relation for symbols-only servers like zsh's shuck),
        recursive to `depth`. Nested {fqname, kind, file, line, children[]}; nodes with a
        pinned learning are flagged. Pass `project_path=<a path in the repo>` (or `project=<name>`) only to target a DIFFERENT project than your current one."""
        res = _resolve(crib, project, project_path)
        return _echo_dict(crib.code_graph(symbol, direction, depth, res.project), res)

    @mcp.tool()
    async def learning_add(symbol: str, text: str, project: str | None = None,
                          project_path: str | None = None) -> dict[str, Any]:
        """Pin a durable human learning to a code symbol — the 'now I get it',
        the subtlety, the gotcha you don't want to re-derive next session. Stored
        as a first-class note under <project>/code-learnings/ keyed to the symbol's
        fqn, SEPARATE from the regenerable LLM description, so it survives
        re-indexing and rides git sync (and works on code you can't edit — vendored
        deps, read-only explorations — where a comment can't go). Appends a dated
        entry to the symbol's running note. `symbol` is a bare name or dotted
        fqname already in the symbol_index (code_index the file first). Surfaces
        back via code_lookup/code_xref."""
        return await crib.code_append(symbol, text, _project(crib, project, project_path))

    @mcp.tool()
    async def learning_edit(symbol: str, new_content: str, project: str | None = None,
                        project_path: str | None = None) -> dict[str, Any]:
        """Rewrite a symbol's learning body wholesale (frontmatter preserved) —
        the standard edit, scoped to a symbol. Errors if none exists; code_append
        creates."""
        return await crib.code_edit(symbol, new_content, _project(crib, project, project_path))

    @mcp.tool()
    async def learning_forget(symbol: str, project: str | None = None,
                          project_path: str | None = None) -> dict[str, Any]:
        """Remove a symbol's learning (stashed to the version ring first, so it's
        recoverable) — the standard forget, scoped to a symbol."""
        return await crib.code_forget(symbol, _project(crib, project, project_path))

    @mcp.tool()
    def learning_read(symbol: str, project: str | None = None,
                  project_path: str | None = None) -> dict[str, Any]:
        """Read a symbol's attached learning note (frontmatter + body), or found=
        False if none is written yet. `symbol` is a bare name or dotted fqname."""
        return crib.code_read(symbol, _project(crib, project, project_path))

    @mcp.tool()
    async def learning_reaffirm(symbol: str, project: str | None = None,
                            project_path: str | None = None) -> dict[str, Any]:
        """Clear a learning's ⚠ stale flag WITHOUT rewriting it — you re-checked the
        note against the current code and it still holds. Re-snapshots the symbol's
        content_hash so it reads as fresh again. Use when code_lookup shows a 📌 note
        flagged stale but the understanding is still correct."""
        return await crib.code_reaffirm(symbol, _project(crib, project, project_path))

    @mcp.tool()
    def learning_report(project: str | None = None, orphans_only: bool = False,
                       project_path: str | None = None) -> list[dict[str, Any]]:
        """Health report for attached learnings: each is `ok` | `moved` (fqn resolves
        but the symbol's file drifted) | `orphan` (fqn no longer resolves — a rename/
        move/delete left the note dangling). `orphans_only` filters to the actionable
        ones. Report-only; drives cleanup via code_rehome / code_forget."""
        return crib.code_learnings(_project(crib, project, project_path), orphans_only=orphans_only)

    @mcp.tool()
    async def learning_rehome(old_fqn: str, new_fqn: str | None = None,
                          project: str | None = None,
                          project_path: str | None = None) -> dict[str, Any]:
        """Re-point an orphaned learning at the symbol it became. Call with just
        `old_fqn` FIRST to get ranked candidate targets (name/signature/file signals);
        then call again with the chosen `new_fqn` to move the note (id/history
        preserved, frontmatter re-snapshotted). Never auto-moves — you pick, because a
        wrong attach is worse than a dangling one."""
        return await crib.code_rehome(old_fqn, new_fqn, _project(crib, project, project_path))

    @mcp.tool()
    def memory_snapshot(message: str | None = None) -> str:
        """Create a git checkpoint of the whole memory store's data tree (if git is set up)."""
        return crib.snapshot(message)

    @mcp.tool()
    def memory_history(relpath: str | None = None) -> list[str]:
        """Show git commit history for the whole tree (or a single note)."""
        return crib.history(relpath)

    @mcp.tool(name="note_import")
    async def note_import(paths: list[str], project: str | None = None,
                           project_path: str | None = None) -> dict[str, Any]:
        """Copy the NAMED files into memory as crib-owned notes (snapshot you own:
        git-synced, editable, versioned). Distinct from a repo's `.crib` docs, which
        are indexed IN-SITU (source is master, never copied) by `project index`.
        `paths` must be absolute, or relative to `project_path` (there is no shell
        cwd here for them to be relative to)."""
        return _switch_if_created(
            await crib.import_files(paths, project, cwd=_cwd(project_path)))

    @mcp.tool(name="note_import_memory")
    async def note_import_memory(project: str | None = None,
                            project_path: str | None = None) -> dict[str, Any]:
        """Mirror Claude Code's own harness memory (the `memory/*.md` files it
        writes for this project) into a crib project, so those notes become
        searchable here alongside everything else. One-way + idempotent; opts the
        repo into the daemon's live mirror so future memory edits sync on their
        own."""
        return _switch_if_created(
            await crib.import_claude_memory(project, cwd=_cwd(project_path)))

    @write_tool
    async def note_move(relpath: str, to_project: str | None = None,
                   to_relpath: str | None = None, project: str | None = None,
                   project_path: str | None = None) -> dict[str, Any]:
        """Relocate a note to another project and/or rename it, preserving its id
        and version history (the curation primitive — not store-new + forget-old).
        `to_project` moves it across namespaces; `to_relpath` renames it."""
        # a move is a one-off curation op — it must not flip the session project
        # onto the source or destination (no _switch_if_created).
        return await crib.move_note(
            relpath, to_project, to_relpath,
            _write_project(crib, project, project_path))

    @mcp.tool()
    def status() -> dict[str, Any]:
        """One-call health summary: every project's inventory (notes, in-situ doc
        chunks, code symbols, learnings), git-sync state (dirty/ahead/behind),
        which warm LSP sessions are attached (alive/busy/idle), and any indexing
        currently in flight. `sweeps` is the RELIABLE wait signal for a background
        `project_index`: {project: {done, total}} while it runs, absent when done —
        poll status until your project leaves `sweeps`. Use to orient across ALL
        projects; `project_status` goes deep on one."""
        return crib.status()

    @mcp.tool()
    def project_list() -> list[str]:
        """List crib projects (separate memory namespaces). Use to discover
        what's available before a `note_lookup`/`note_store` in a specific project."""
        return crib.projects()

    @mcp.tool()
    def project_use(project: str) -> dict[str, Any]:
        """Set THIS session's current project — subsequent `note_lookup`/`note_store`/etc.
        target it without passing `project` each time. Sticky for the connection;
        a per-call `project` arg still overrides for that one call. Seeded
        automatically from your working directory on first use, so call this only
        to switch. The namespace is created immediately (so it's real and listed,
        not a phantom you're 'in' until the first write)."""
        created = crib.project_is_new(project)
        crib.notes_dir(project)          # eager mkdir — no phantom namespace
        session_state().current_project = project
        return {"current_project": project, "created": created}

    @mcp.tool()
    def project_current(project_path: str | None = None) -> dict[str, Any]:
        """Show this session's current project (seeding it from `project_path`/.crib if not
        yet set), how it resolved, plus the available projects."""
        res = _resolve(crib, None, project_path)
        return {"current_project": res.project, "resolved_via": res.via,
                "projects": crib.projects()}

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
    # Catch up on anything changed while crib (and its watcher) were down.
    # Start the watcher first so edits during the sweep aren't missed; the hash
    # gate makes any overlap a harmless no-op (DESIGN §4).
    rec = await crib.reconcile_all()
    if rec["changed"] or rec["removed"]:
        print(f"[crib] startup reconcile: {rec['changed']} updated, "
              f"{rec['removed']} chunk(s) removed across {rec['projects']} project(s)",
              file=sys.stderr)
    # Catch up + live-mirror any bound Claude harness memory dirs (DESIGN §13).
    try:
        await crib.start_memory_mirror(asyncio.get_running_loop())
    except Exception as e:  # noqa: BLE001 — watchdog optional / stale binding; degrade
        print(f"[crib] memory mirror disabled: {e}", file=sys.stderr)
    try:
        if transport == "stdio":
            await mcp.run_async(transport="stdio")
        else:
            await mcp.run_async(transport="http", host=host, port=port)
    finally:
        crib.close()


def main(transport: str = "stdio", host: str = "127.0.0.1",
         port: int = 8787) -> None:
    asyncio.run(_serve_async(transport, host, port))


if __name__ == "__main__":
    main()
