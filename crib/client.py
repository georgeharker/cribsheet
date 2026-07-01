"""The CLI as an MCP client of the warm `crib` daemon (DESIGN §10.2).

`crib <verb>` does not pay the cold-start cost (chroma client + embedder
weights) on every call. Instead it attaches — via sharedserver — to the one
long-lived `crib --mcp --http` process (the same one Claude talks to over MCP),
speaks the MCP tool surface over TCP, and detaches. sharedserver's refcount +
grace keep that process warm between invocations; dead-client detection reaps
our refcount if the CLI crashes mid-call.

Requires the `sharedserver` binary on PATH; without it use `--no-daemon` (or
set `[daemon].enabled = false`) to fall back to in-process `Crib.open()`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from . import sharedserver
from .config import DaemonConfig


class DaemonError(RuntimeError):
    pass


def _data(res: Any) -> Any:
    """Unwrap a CallToolResult to plain Python.

    Prefer ``structured_content`` (the faithful JSON dict the daemon emitted) over
    ``.data``: FastMCP's client-side typed reconstruction of ``.data`` collapses a
    list-of-objects return into empty, field-less models — e.g. ``lookup`` came
    back as ``[Root(), Root()]``, serializing to ``[{}, {}]`` and rendering as
    ``Root()`` — because it can't rebuild the item type from the output schema.
    ``.data`` stays correct for scalar/string returns, so it remains the fallback.
    Every crib tool's structured output is the ``{"result": <value>}`` envelope
    FastMCP wraps non-object returns in; unwrap it. Dict-returning tools (writes)
    surface their dict directly (keys != {"result"}), so pass those through as-is."""
    sc = getattr(res, "structured_content", None)
    if isinstance(sc, dict):
        return sc["result"] if set(sc) == {"result"} else sc
    data = getattr(res, "data", None)
    if data is not None:
        return data
    blocks = getattr(res, "content", None) or []
    return "".join(getattr(b, "text", "") for b in blocks)


class DaemonClient:
    """Attach to the warm daemon for the duration of a `with` block.

    `__enter__` increments the sharedserver refcount (starting the daemon if no
    one else has it up); `__exit__` decrements it, leaving the grace period to
    keep it warm for the next call.
    """

    def __init__(self, cfg: DaemonConfig, ready_timeout: float = 30.0) -> None:
        self.cfg = cfg
        self.url = f"http://{cfg.host}:{cfg.port}/mcp"
        self.ready_timeout = ready_timeout

    @property
    def _command(self) -> list[str]:
        # Must match the sharedServer registration so `use` attaches to the same
        # process rather than racing a second one onto the port.
        return ["crib", "--mcp", "--http",
                "--host", self.cfg.host, "--port", str(self.cfg.port)]

    def __enter__(self) -> "DaemonClient":
        sharedserver.use(self.cfg.name, self._command, self.cfg.grace_period)
        return self

    def __exit__(self, *exc: object) -> None:
        sharedserver.unuse(self.cfg.name)

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Call one MCP tool and return its result as plain Python."""
        args = {k: v for k, v in arguments.items() if v is not None}
        return asyncio.run(self._call(tool, args))

    async def _call(self, tool: str, args: dict[str, Any]) -> Any:
        from fastmcp import Client  # lazy: keeps the package importable without it

        await self._wait_ready(Client)
        async with Client(self.url) as client:
            return _data(await client.call_tool(tool, args))

    async def _wait_ready(self, Client: Any) -> None:
        """Poll until the daemon answers — it may still be starting if we (not
        Claude's MCP host) just launched it."""
        deadline = time.monotonic() + self.ready_timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                async with Client(self.url) as client:
                    await client.ping()
                return
            except Exception as e:  # noqa: BLE001 — any error means not-ready-yet
                last = e
                await asyncio.sleep(0.3)
        raise DaemonError(
            f"crib daemon at {self.url} did not become ready in "
            f"{self.ready_timeout:.0f}s: {last}"
        )
