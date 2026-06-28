"""FastMCP server exposing the crib tool surface (DESIGN §5).

Lazy-imports fastmcp so the package stays importable without it.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from .app import Crib


def build_server(crib: Crib | None = None):
    from fastmcp import FastMCP  # lazy

    crib = crib or Crib.open()
    mcp = FastMCP("cribsheet")

    @mcp.tool()
    def lookup(query: str, project: str | None = None, k: int = 8,
               tags: list[str] | None = None) -> list[dict[str, Any]]:
        """Semantic search over memory. Returns ranked note sections."""
        return [vars(h) for h in crib.lookup(query, project, k, tags)]

    @mcp.tool()
    def read(relpath: str, project: str | None = None) -> str:
        """Read a note's full raw markdown (frontmatter + body)."""
        return crib.read_note(relpath, project)

    @mcp.tool()
    def locate(relpath: str, project: str | None = None) -> str:
        """Get the real on-disk path of a note so you can edit it with your own
        file tools. After editing, call `reindex(relpath)` to make it searchable
        now (the watcher would catch it shortly regardless)."""
        return crib.locate(relpath, project)

    @mcp.tool()
    async def store(content: str, title: str | None = None,
                    project: str | None = None,
                    tags: list[str] | None = None) -> dict[str, Any]:
        """Create a new note. Assigns an id, writes markdown, indexes it."""
        return await crib.store_note(content, title, project, tags)

    @mcp.tool()
    async def append(relpath: str, content: str, heading: str | None = None,
                     project: str | None = None) -> dict[str, Any]:
        """Append content to an existing note, optionally under a new heading."""
        return await crib.append_note(relpath, content, heading, project)

    @mcp.tool()
    async def edit(relpath: str, new_content: str,
                   project: str | None = None) -> dict[str, Any]:
        """Replace a note's raw content (for LLM-driven rewrites)."""
        return await crib.edit_note(relpath, new_content, project)

    @mcp.tool()
    async def forget(relpath: str, project: str | None = None) -> dict[str, Any]:
        """Delete a note from disk and the index. Content is stashed to the
        version ring first, so it stays recoverable by id."""
        return await crib.forget(relpath, project)

    @mcp.tool()
    async def reindex(relpath: str | None = None,
                      project: str | None = None) -> dict[str, Any]:
        """Reindex a note (or the whole project). Call after editing a note via
        its raw path. Safe to call redundantly — it no-ops if already current."""
        return await crib.reindex(relpath, project)

    @mcp.tool()
    def versions(relpath: str, project: str | None = None) -> list[dict[str, Any]]:
        """List recoverable prior versions of a note."""
        return crib.list_versions(relpath, project)

    @mcp.tool()
    async def restore(relpath: str, version: str,
                      project: str | None = None) -> dict[str, Any]:
        """Restore a prior version of a note (itself undoable)."""
        return await crib.restore(relpath, version, project)

    @mcp.tool()
    def snapshot(message: str | None = None) -> str:
        """Create a git checkpoint of the data tree (if git is set up)."""
        return crib.snapshot(message)

    @mcp.tool()
    def history(relpath: str | None = None) -> list[str]:
        """Show git commit history for a note or the whole tree."""
        return crib.history(relpath)

    @mcp.tool(name="import")
    async def import_docs(project: str | None = None) -> dict[str, Any]:
        """Ingest local docs declared in the nearest `.crib` into a project."""
        return await crib.import_docs(project)

    @mcp.tool()
    def projects() -> list[str]:
        """List crib projects."""
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
