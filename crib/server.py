"""FastMCP server exposing the crib tool surface (DESIGN §5).

Lazy-imports fastmcp so the package stays importable without it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from .app import Crib
from .session import resolve_session_project, session_state


def _cwd(cwd: str | None) -> Path | None:
    """The CLI (an MCP client) passes its own working directory so the daemon
    resolves `.crib`/project relative to the caller, not the daemon's cwd."""
    return Path(cwd) if cwd else None


def _project(crib: Crib, project: str | None, cwd: str | None) -> str:
    """Resolve the project for a tool call through the connection's session: an
    explicit `project` overrides for this call; otherwise the session's current
    project (seeded once from cwd/.crib) is used (DESIGN §15)."""
    return resolve_session_project(
        session_state(), project, _cwd(cwd),
        lambda c: crib.resolve_project(None, c))


def _switch_if_created(result: dict) -> dict:
    """Creating a project switches the session into it — referencing an existing
    one (a one-off `project` arg) does not (DESIGN §15)."""
    if isinstance(result, dict) and result.get("created"):
        session_state().current_project = result.get("project")
    return result


def build_server(crib: Crib | None = None):
    from fastmcp import FastMCP  # lazy

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
            "investigation may already be stored. Call `lookup` to find it, or "
            "`apropos` to read the full matching sections. Do this before "
            "answering from memory alone; the stored answer may be more current. "
            "PERSIST what's worth keeping — whenever the user shares, or you "
            "establish, something durable (a decision, preference, convention, "
            "gotcha, or hard-won fact), also save it here so it outlives this "
            "session and reaches other agents: `store` a new note, or "
            "`append`/`edit` one found via `lookup`. Prefer updating an existing "
            "note over creating near-duplicates. "
            "CODE: a project may also carry a *code symbol index* — its "
            "functions/classes/methods with an LLM 'what it does' description AND a "
            "real cross-file call graph (callers/callees). When you would reach for "
            "grep to answer a CODE question — *where is X handled*, *what does this "
            "function do*, *what calls Y*, *what does Z call* — try `code_lookup` "
            "(find a symbol by CONCEPT or by name, even a cryptic private one) and "
            "`code_xref` / `code_graph` (callers/callees, recursively) FIRST: they "
            "answer by intent and cross-reference, which grep cannot. `code_index "
            "<file>` populates it. When you finally UNDERSTAND a symbol — a subtlety, "
            "a gotcha, a 'now I get it' worth keeping — `code_append <symbol> \"…\"` "
            "pins a durable learning to it (survives re-indexing, works even on code "
            "you can't edit); it surfaces back next time via `code_lookup`/`code_xref`. "
            "CROSS-MACHINE: some notes are mirrored from another machine's Claude "
            "memory (frontmatter `source: claude_memory`, `host: <name>`, under "
            "`claude-memory/<host>/`). Treat the *learning* as portable — "
            "decisions, conventions, gotchas usually travel — but verify "
            "machine-specific details (absolute paths, ports, hostnames, install "
            "locations) against the local machine before relying on them."
        ),
    )

    @mcp.tool()
    def lookup(query: str, project: str | None = None, k: int = 8,
               tags: list[str] | None = None,
               keyword_labels: list[str] | None = None,
               keyword_weight: float | None = None,
               summary_labels: list[str] | None = None,
               summary_weight: float | None = None,
               cwd: str | None = None) -> list[dict[str, Any]]:
        """Semantic search over memory. Call this FIRST when the user asks
        about this project — a prior decision, convention, or investigation
        may already be stored. Returns ranked note sections, each with its
        relpath and the line_start/line_end span of the matching section so
        you can jump straight to it (pair with `locate` for the abspath).
        `keyword_labels`/`keyword_weight` (BM25 keyword_index) and
        `summary_labels` (dense summary_index aliases) override which LLM index
        sets feed retrieval (default from config); mainly for eval sweeps."""
        return [vars(h) for h in
                crib.lookup(query, _project(crib, project, cwd), k, tags,
                            keyword_labels=keyword_labels,
                            keyword_weight=keyword_weight,
                            summary_labels=summary_labels,
                            summary_weight=summary_weight)]

    @mcp.tool()
    def apropos(query: str, project: str | None = None, k: int = 8,
                tags: list[str] | None = None,
                cwd: str | None = None) -> list[dict[str, Any]]:
        """Like `lookup`, but each hit carries the full matching section's
        markdown (`section`) instead of a short snippet — for reading the
        matched sections in full, not just locating them."""
        return crib.apropos(query, _project(crib, project, cwd), k, tags)

    @mcp.tool()
    def read(relpath: str, project: str | None = None,
             cwd: str | None = None) -> str:
        """Read a note's full raw markdown (frontmatter + body) — e.g. to see a
        `lookup` hit in full context, or before rewriting the note with `edit`."""
        return crib.read_note(relpath, _project(crib, project, cwd))

    @mcp.tool()
    def locate(relpath: str, project: str | None = None,
               cwd: str | None = None) -> str:
        """Get the real on-disk path of a note so you can edit it with your own
        file tools. After editing, call `reindex(relpath)` to make it searchable
        now (the watcher would catch it shortly regardless)."""
        return crib.locate(relpath, _project(crib, project, cwd))

    @mcp.tool()
    async def store(content: str, title: str | None = None,
                    project: str | None = None,
                    tags: list[str] | None = None,
                    cwd: str | None = None) -> dict[str, Any]:
        """Persist a durable fact to memory — a decision, preference,
        convention, gotcha, or hard-won detail worth recalling in a future
        session. Assigns an id, writes markdown, indexes it. If a related
        note already exists (check with `lookup`), prefer `append`/`edit`
        over creating a near-duplicate."""
        return _switch_if_created(
            await crib.store_note(content, title, _project(crib, project, cwd), tags))

    @mcp.tool()
    async def append(relpath: str, content: str, heading: str | None = None,
                     project: str | None = None,
                     cwd: str | None = None) -> dict[str, Any]:
        """Add to an existing note (found via `lookup`) — the right call when new
        information extends or continues something already remembered, rather than
        `store`-ing a near-duplicate. Optionally files it under a new heading."""
        return await crib.append_note(relpath, content, heading,
                                      _project(crib, project, cwd))

    @mcp.tool()
    async def edit(relpath: str, new_content: str,
                   project: str | None = None,
                   cwd: str | None = None) -> dict[str, Any]:
        """Rewrite a note's full content — use when remembered information has
        changed, needs correcting, or several notes should be consolidated (read
        it first). Frontmatter (and the note's id/history) is preserved."""
        return await crib.edit_note(relpath, new_content, _project(crib, project, cwd))

    @mcp.tool()
    async def forget(relpath: str, project: str | None = None,
                     cwd: str | None = None) -> dict[str, Any]:
        """Delete a note when its information is obsolete or wrong. Removed from
        disk and the index, but stashed to the version ring first, so it stays
        recoverable by id."""
        return await crib.forget(relpath, _project(crib, project, cwd))

    @mcp.tool()
    async def reindex(relpath: str | None = None,
                      project: str | None = None,
                      cwd: str | None = None) -> dict[str, Any]:
        """Reindex a note (or the whole project). Call after editing a note via
        its raw path. Safe to call redundantly — it no-ops if already current."""
        return await crib.reindex(relpath, _project(crib, project, cwd))

    @mcp.tool()
    def versions(relpath: str, project: str | None = None,
                 cwd: str | None = None) -> list[dict[str, Any]]:
        """List recoverable prior versions of a note."""
        return crib.list_versions(relpath, _project(crib, project, cwd))

    @mcp.tool()
    async def restore(relpath: str, version: str,
                      project: str | None = None,
                      cwd: str | None = None) -> dict[str, Any]:
        """Restore a prior version of a note (itself undoable)."""
        return await crib.restore(relpath, version, _project(crib, project, cwd))

    @mcp.tool()
    async def reconcile() -> dict[str, Any]:
        """Sweep every project for changes made while crib was down and bring the
        index back in line. Safe to call anytime — the hash gate no-ops anything
        already current."""
        return await crib.reconcile_all()

    @mcp.tool()
    async def distill(relpath: str, project: str | None = None,
                      cwd: str | None = None) -> dict[str, Any]:
        """LLM-revise a note in place: compress, dedupe, normalize — keeping
        facts/decisions, dropping deliberation, preserving code verbatim.
        Thrash-guarded (no-op if unchanged); the prior version is recoverable."""
        return await crib.distill(relpath, _project(crib, project, cwd))

    @mcp.tool()
    async def elaborate(label: str, relpath: str | None = None,
                        project: str | None = None, overwrite: bool = False,
                        cwd: str | None = None) -> dict[str, Any]:
        """keyword_index: generate BM25 search terms per section (or whole
        project), section-addressed under `label` (e.g. `keywords`, `questions`,
        `phrase`). Skips cached sections unless `overwrite`. Activate via
        [retrieve].keyword_labels."""
        return await crib.elaborate(label, relpath, _project(crib, project, cwd),
                                    overwrite=overwrite)

    @mcp.tool()
    async def summarize(label: str, relpath: str | None = None,
                        project: str | None = None, overwrite: bool = False,
                        cwd: str | None = None) -> dict[str, Any]:
        """summary_index: generate LLM rephrasings per section (or whole project),
        embedded as dense alias vectors so paraphrased queries match a section
        with zero shared tokens. Skips cached sections unless `overwrite`.
        Activate via [retrieve].summary_labels."""
        return await crib.summarize(label, relpath, _project(crib, project, cwd),
                                    overwrite=overwrite)

    @mcp.tool()
    async def code_index(path: str, project: str | None = None,
                         cwd: str | None = None) -> dict[str, Any]:
        """symbol_index: extract a source file's symbols + call graph (callers/
        callees, via the LSP) and persist them content-addressed under
        `<project>/symbol_index/`. `path` is a source file (abs or relative to cwd).
        Structural facet of docs/code-symbol-index.md."""
        return await crib.code_index(path, _project(crib, project, cwd))

    @mcp.tool()
    async def code_xref(symbol: str, project: str | None = None,
                        cwd: str | None = None) -> list[dict[str, Any]]:
        """Look up a code symbol's callers/callees from the persisted symbol_index
        (no live LSP needed). `symbol` is a bare name or dotted fqname."""
        return crib.code_xref(symbol, _project(crib, project, cwd))

    @mcp.tool()
    async def code_lookup(query: str, project: str | None = None, k: int = 8,
                          cwd: str | None = None) -> list[dict[str, Any]]:
        """Find a code symbol by CONCEPT or NAME — call this FIRST when you'd grep
        the codebase for a function/class ("where do we fuse ranked lists", "the
        oauth refresh"). HYBRID: a dense search over LLM 'what it does' descriptions
        ⊕ a name/subtoken match, so it finds a symbol by intent (grep can't) OR by a
        bare/partial/cryptic name. Returns ranked symbols with signature, file:line,
        and callers/callees. Pair with `code_xref`/`code_graph` to walk the call
        graph. Populate a project first with `code_index <file>`."""
        return crib.code_lookup(query, _project(crib, project, cwd), k)

    @mcp.tool()
    async def code_graph(symbol: str, direction: str = "callees", depth: int = 6,
                         project: str | None = None,
                         cwd: str | None = None) -> dict[str, Any]:
        """Call-graph tree around a symbol from the symbol_index: `callees` (what it
        calls), `callers` (what calls it), or `references` (everywhere it's mentioned —
        broader than calls, and the only relation for symbols-only servers like zsh's
        shuck), recursive to `depth`. Nested {fqname, kind, file, line, children[]} —
        the CLI renders it pstree-style."""
        return crib.code_graph(symbol, direction, depth, _project(crib, project, cwd))

    @mcp.tool()
    async def code_append(symbol: str, text: str, project: str | None = None,
                          cwd: str | None = None) -> dict[str, Any]:
        """Pin a durable human learning to a code symbol — the 'now I get it',
        the subtlety, the gotcha you don't want to re-derive next session. Stored
        as a first-class note under <project>/code-learnings/ keyed to the symbol's
        fqn, SEPARATE from the regenerable LLM description, so it survives
        re-indexing and rides git sync (and works on code you can't edit — vendored
        deps, read-only explorations — where a comment can't go). Appends a dated
        entry to the symbol's running note. `symbol` is a bare name or dotted
        fqname already in the symbol_index (code_index the file first). Surfaces
        back via code_lookup/code_xref."""
        return await crib.code_append(symbol, text, _project(crib, project, cwd))

    @mcp.tool()
    async def code_edit(symbol: str, new_content: str, project: str | None = None,
                        cwd: str | None = None) -> dict[str, Any]:
        """Rewrite a symbol's learning body wholesale (frontmatter preserved) —
        the standard edit, scoped to a symbol. Errors if none exists; code_append
        creates."""
        return await crib.code_edit(symbol, new_content, _project(crib, project, cwd))

    @mcp.tool()
    async def code_forget(symbol: str, project: str | None = None,
                          cwd: str | None = None) -> dict[str, Any]:
        """Remove a symbol's learning (stashed to the version ring first, so it's
        recoverable) — the standard forget, scoped to a symbol."""
        return await crib.code_forget(symbol, _project(crib, project, cwd))

    @mcp.tool()
    def code_read(symbol: str, project: str | None = None,
                  cwd: str | None = None) -> dict[str, Any]:
        """Read a symbol's attached learning note (frontmatter + body), or found=
        False if none is written yet. `symbol` is a bare name or dotted fqname."""
        return crib.code_read(symbol, _project(crib, project, cwd))

    @mcp.tool()
    async def code_reaffirm(symbol: str, project: str | None = None,
                            cwd: str | None = None) -> dict[str, Any]:
        """Clear a learning's ⚠ stale flag WITHOUT rewriting it — you re-checked the
        note against the current code and it still holds. Re-snapshots the symbol's
        content_hash so it reads as fresh again. Use when code_lookup shows a 📌 note
        flagged stale but the understanding is still correct."""
        return await crib.code_reaffirm(symbol, _project(crib, project, cwd))

    @mcp.tool()
    def code_learnings(project: str | None = None, orphans_only: bool = False,
                       cwd: str | None = None) -> list[dict[str, Any]]:
        """Health report for attached learnings: each is `ok` | `moved` (fqn resolves
        but the symbol's file drifted) | `orphan` (fqn no longer resolves — a rename/
        move/delete left the note dangling). `orphans_only` filters to the actionable
        ones. Report-only; drives cleanup via code_rehome / code_forget."""
        return crib.code_learnings(_project(crib, project, cwd), orphans_only=orphans_only)

    @mcp.tool()
    async def code_rehome(old_fqn: str, new_fqn: str | None = None,
                          project: str | None = None,
                          cwd: str | None = None) -> dict[str, Any]:
        """Re-point an orphaned learning at the symbol it became. Call with just
        `old_fqn` FIRST to get ranked candidate targets (name/signature/file signals);
        then call again with the chosen `new_fqn` to move the note (id/history
        preserved, frontmatter re-snapshotted). Never auto-moves — you pick, because a
        wrong attach is worse than a dangling one."""
        return await crib.code_rehome(old_fqn, new_fqn, _project(crib, project, cwd))

    @mcp.tool()
    def snapshot(message: str | None = None) -> str:
        """Create a git checkpoint of the data tree (if git is set up)."""
        return crib.snapshot(message)

    @mcp.tool()
    def history(relpath: str | None = None) -> list[str]:
        """Show git commit history for a note or the whole tree."""
        return crib.history(relpath)

    @mcp.tool(name="import")
    async def import_docs(project: str | None = None,
                          cwd: str | None = None) -> dict[str, Any]:
        """Ingest local docs declared in the nearest `.crib` into a project — a
        one-way pull (source wins, note ids/history preserved), safe to re-run as
        the source repo's docs change."""
        return _switch_if_created(await crib.import_docs(project, cwd=_cwd(cwd)))

    @mcp.tool(name="import_memory")
    async def import_memory(project: str | None = None,
                            cwd: str | None = None) -> dict[str, Any]:
        """Mirror Claude Code's own harness memory (the `memory/*.md` files it
        writes for this project) into a crib project, so those notes become
        searchable here alongside everything else. One-way + idempotent; opts the
        repo into the daemon's live mirror so future memory edits sync on their
        own."""
        return _switch_if_created(
            await crib.import_claude_memory(project, cwd=_cwd(cwd)))

    @mcp.tool()
    async def move(relpath: str, to_project: str | None = None,
                   to_relpath: str | None = None, project: str | None = None,
                   cwd: str | None = None) -> dict[str, Any]:
        """Relocate a note to another project and/or rename it, preserving its id
        and version history (the curation primitive — not store-new + forget-old).
        `to_project` moves it across namespaces; `to_relpath` renames it."""
        return _switch_if_created(await crib.move_note(
            relpath, to_project, to_relpath, _project(crib, project, cwd)))

    @mcp.tool()
    def projects() -> list[str]:
        """List crib projects (separate memory namespaces). Use to discover
        what's available before a `lookup`/`store` in a specific project."""
        return crib.projects()

    @mcp.tool()
    def use_project(project: str) -> dict[str, Any]:
        """Set THIS session's current project — subsequent `lookup`/`store`/etc.
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
    def current_project(cwd: str | None = None) -> dict[str, Any]:
        """Show this session's current project (seeding it from cwd/.crib if not
        yet set), plus the available projects."""
        return {"current_project": _project(crib, None, cwd),
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
