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
               cwd: str | None = None) -> list[dict[str, Any]]:
        """Semantic search over memory. Call this FIRST when the user asks
        about this project — a prior decision, convention, or investigation
        may already be stored. Returns ranked note sections, each with its
        relpath and the line_start/line_end span of the matching section so
        you can jump straight to it (pair with `locate` for the abspath)."""
        return [vars(h) for h in
                crib.lookup(query, _project(crib, project, cwd), k, tags)]

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
