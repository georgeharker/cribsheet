"""FastMCP server exposing the crib tool surface (DESIGN §5).

Lazy-imports fastmcp so the package stays importable without it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from .app import Crib


def _cwd(cwd: str | None) -> Path | None:
    """The CLI (an MCP client) passes its own working directory so the daemon
    resolves `.crib`/project relative to the caller, not the daemon's cwd."""
    return Path(cwd) if cwd else None


def build_server(crib: Crib | None = None):
    from fastmcp import FastMCP  # lazy

    crib = crib or Crib.open()
    mcp = FastMCP(
        "cribsheet",
        instructions=(
            "Long-term markdown memory that persists across sessions. "
            "Before answering questions about this project — a past "
            "decision, convention, gotcha, or prior investigation — call "
            "`lookup` first; the answer may already be stored. When the "
            "user shares something worth remembering across sessions (a "
            "decision, preference, convention, gotcha, or hard-won fact), "
            "persist it: `store` a new note, or `append`/`edit` an "
            "existing one found via `lookup`. Prefer updating an existing "
            "note over creating near-duplicates."
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
        return [vars(h) for h in crib.lookup(query, project, k, tags, cwd=_cwd(cwd))]

    @mcp.tool()
    def apropos(query: str, project: str | None = None, k: int = 8,
                tags: list[str] | None = None,
                cwd: str | None = None) -> list[dict[str, Any]]:
        """Like `lookup`, but each hit carries the full matching section's
        markdown (`section`) instead of a short snippet — for reading the
        matched sections in full, not just locating them."""
        return crib.apropos(query, project, k, tags, cwd=_cwd(cwd))

    @mcp.tool()
    def read(relpath: str, project: str | None = None,
             cwd: str | None = None) -> str:
        """Read a note's full raw markdown (frontmatter + body) — e.g. to see a
        `lookup` hit in full context, or before rewriting the note with `edit`."""
        return crib.read_note(relpath, project, cwd=_cwd(cwd))

    @mcp.tool()
    def locate(relpath: str, project: str | None = None,
               cwd: str | None = None) -> str:
        """Get the real on-disk path of a note so you can edit it with your own
        file tools. After editing, call `reindex(relpath)` to make it searchable
        now (the watcher would catch it shortly regardless)."""
        return crib.locate(relpath, project, cwd=_cwd(cwd))

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
        return await crib.store_note(content, title, project, tags, cwd=_cwd(cwd))

    @mcp.tool()
    async def append(relpath: str, content: str, heading: str | None = None,
                     project: str | None = None,
                     cwd: str | None = None) -> dict[str, Any]:
        """Add to an existing note (found via `lookup`) — the right call when new
        information extends or continues something already remembered, rather than
        `store`-ing a near-duplicate. Optionally files it under a new heading."""
        return await crib.append_note(relpath, content, heading, project, cwd=_cwd(cwd))

    @mcp.tool()
    async def edit(relpath: str, new_content: str,
                   project: str | None = None,
                   cwd: str | None = None) -> dict[str, Any]:
        """Rewrite a note's full content — use when remembered information has
        changed, needs correcting, or several notes should be consolidated (read
        it first). Frontmatter (and the note's id/history) is preserved."""
        return await crib.edit_note(relpath, new_content, project, cwd=_cwd(cwd))

    @mcp.tool()
    async def forget(relpath: str, project: str | None = None,
                     cwd: str | None = None) -> dict[str, Any]:
        """Delete a note when its information is obsolete or wrong. Removed from
        disk and the index, but stashed to the version ring first, so it stays
        recoverable by id."""
        return await crib.forget(relpath, project, cwd=_cwd(cwd))

    @mcp.tool()
    async def reindex(relpath: str | None = None,
                      project: str | None = None,
                      cwd: str | None = None) -> dict[str, Any]:
        """Reindex a note (or the whole project). Call after editing a note via
        its raw path. Safe to call redundantly — it no-ops if already current."""
        return await crib.reindex(relpath, project, cwd=_cwd(cwd))

    @mcp.tool()
    def versions(relpath: str, project: str | None = None,
                 cwd: str | None = None) -> list[dict[str, Any]]:
        """List recoverable prior versions of a note."""
        return crib.list_versions(relpath, project, cwd=_cwd(cwd))

    @mcp.tool()
    async def restore(relpath: str, version: str,
                      project: str | None = None,
                      cwd: str | None = None) -> dict[str, Any]:
        """Restore a prior version of a note (itself undoable)."""
        return await crib.restore(relpath, version, project, cwd=_cwd(cwd))

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
        return await crib.import_docs(project, cwd=_cwd(cwd))

    @mcp.tool()
    def projects() -> list[str]:
        """List crib projects (separate memory namespaces). Use to discover
        what's available before a `lookup`/`store` in a specific project."""
        return crib.projects()

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
