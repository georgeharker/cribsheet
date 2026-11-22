"""Styling the CLI's human output by ROLE, on a capable terminal only.

Every emitter tags the parts of a line with what they ARE (`paint("path", ...)`,
`paint("rank", ...)`); one theme says how each role looks. Which parts a command
shows, and in which roles, is that command's choice. Design decision: "CLI human
output is styled by role through rich, only on a capable terminal".

Off (the default until `configure` runs, and whenever stdout isn't a colour
terminal), `paint` returns its text UNCHANGED: piped output and `--json` are
byte-identical to unstyled output by construction. On, the text is rendered
through a rich `Style` (ANSI SGR codes, plus an OSC 8 hyperlink when `link` is
given) and nothing else about the line changes.

Colours come from a built-in palette; `$CRIB_THEME_FILE`'s `[styles]` section
(the same INI the markdown renderer reads) overrides any role by name, e.g.
`crib.path = blue underline`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# role -> rich style spec. Plain ANSI colours (not truecolor hex) so the
# terminal's own palette decides the exact shade, light or dark.
DEFAULT_STYLES: dict[str, str] = {
    "crib.rank": "dim",  # [1], [2] ...
    "crib.score": "dim",  # [1.283]
    "crib.kind": "magenta",  # function / class / plan ...
    "crib.name": "bold",  # a symbol's qualified name
    "crib.title": "bold",  # an item's title
    "crib.heading": "italic",  # the matched section of a note
    "crib.path": "cyan",  # file / relpath (+ :line)
    "crib.meta": "dim",  # ids, counts, call-graph arrows
    "crib.header": "bold underline",  # a section of a report ("in progress:")
    "crib.label": "bold",  # a key column (status's `daemon`, `lsp` ...)
    "crib.warn": "yellow",  # stale / tainted / rebuilding
    "crib.error": "red",  # failures, refusals
    "crib.ok": "green",
    # statuses of graph items, by value
    "crib.status.active": "green",
    "crib.status.done": "green",
    "crib.status.in-progress": "yellow",
    "crib.status.todo": "default",
    "crib.status.proposed": "cyan",
    "crib.status.parked": "dim",
    "crib.status.superseded": "dim",
    "crib.status.declined": "red",
    "crib.status.blocked": "red",
    "crib.status.rejected": "red",
}

_state: dict[str, Any] = {"color_system": None, "styles": {}}


def configure(mode: str = "auto", stream: Any = None) -> None:
    """Decide once, for this process, whether output is styled. `mode` is the
    global `--color`: `always`, `never`, or `auto` (styled when `stream`, stdout
    by default, is a terminal rich would colour: it honours NO_COLOR,
    FORCE_COLOR and TERM=dumb)."""
    import sys

    from rich.console import Console

    _state["color_system"] = None
    if mode == "never":
        return
    stream = stream if stream is not None else sys.stdout
    if mode == "always":
        console = Console(file=stream, force_terminal=True)
        system = console.color_system or "standard"
    else:
        if os.environ.get("NO_COLOR"):
            return
        console = Console(file=stream)
        if not console.is_terminal or console.is_dumb_terminal:
            return
        system = console.color_system
    if not system:
        return
    from rich.color import ColorSystem

    _state["color_system"] = {
        "standard": ColorSystem.STANDARD, "256": ColorSystem.EIGHT_BIT,
        "truecolor": ColorSystem.TRUECOLOR, "windows": ColorSystem.WINDOWS,
    }.get(system, ColorSystem.STANDARD)
    _state["styles"] = _load_styles()


def enabled() -> bool:
    return _state["color_system"] is not None


def _load_styles() -> dict[str, Any]:
    from rich.style import Style

    specs = dict(DEFAULT_STYLES)
    if theme := os.environ.get("CRIB_THEME_FILE"):
        import configparser

        cfg = configparser.ConfigParser()
        try:
            if cfg.read(theme) and cfg.has_section("styles"):
                specs.update(
                    {k: v for k, v in cfg.items("styles") if k.startswith("crib.")}
                )
        except configparser.Error:
            pass  # a broken theme file must not take the CLI down
    out: dict[str, Any] = {}
    for role, spec in specs.items():
        try:
            out[role] = Style.parse(spec)
        except Exception:  # noqa: BLE001 — a bad spec falls back to unstyled
            out[role] = Style()
    return out


def paint(role: str, text: Any, link: str | None = None) -> str:
    """`text` styled as `role` (`path`, `rank`, `status.done` ...; the `crib.`
    prefix is implied), linked to `link` when given. Unchanged when styling is
    off, or the text is empty."""
    text = str(text)
    if _state["color_system"] is None or not text:
        return text
    from rich.style import Style

    style = _state["styles"].get(f"crib.{role}")
    if style is None and role.startswith("status."):
        style = Style()
    if style is None:
        return text
    if link:
        style = style + Style(link=link)
    return style.render(text, color_system=_state["color_system"])


def status(value: Any) -> str:
    """A graph status, coloured by its value."""
    return paint(f"status.{value}", value)


def file_link(path: str | Path | None) -> str | None:
    """A `file://` URL for an absolute path, or None when the path isn't
    absolute. Only built while styling is on. No `#L<line>` fragment: terminals
    hand the URL to the OS as is, and a fragment can stop the file opening."""
    if _state["color_system"] is None or not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        return None
    return p.as_uri()
