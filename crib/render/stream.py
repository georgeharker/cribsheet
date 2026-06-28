"""Reusable streaming-Markdown renderer that mirrors textual's API.

Interface parallels textual's ``Markdown.get_stream()`` so callers can
swap the target (rich Console vs textual Markdown widget) without
restructuring their code:

    # textual side
    stream = Markdown.get_stream(markdown_widget)
    try:
        async for chunk in source: await stream.write(chunk)
    finally:
        await stream.stop()

    # rich side (this module)
    stream = LiveMarkdownStream.get_stream(Console())
    try:
        for chunk in source: stream.write(chunk)
    finally:
        stream.stop()

The append mechanism mirrors textual too. From reading
``textual.widgets._markdown.Markdown.append``:

  1. Track ``_last_parsed_line`` — the source line up to which we've
     committed rendered output that won't change.
  2. On write: parse ONLY the new source (from _last_parsed_line on)
     with markdown-it-py, not the whole document.
  3. Advance _last_parsed_line to the START of the LAST top-level
     token. Everything before that is "complete" and gets frozen.
     The trailing block is potentially incomplete (more chunks may
     extend it) so it stays mutable.
  4. Render the newly-frozen prefix once, append to a cached ANSI
     string. The mutable trailing block is re-rendered each tick
     into ``Text.from_ansi(frozen + fresh_trailing)``.

Cost per write: parse only new source (typically a few lines).
Cost per render: re-render only the trailing block (small).
Cost per stop: one final commit + render.

This is the same shape as textual's mount-new-blocks pattern adapted
for rich's flat ANSI-string output instead of a widget tree.
"""

from __future__ import annotations

import io
import time
from types import TracebackType
from typing import Literal, Optional, Type

from markdown_it import MarkdownIt
from rich.console import Console, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text
from rich.theme import Theme

Mode = Literal["markdown", "raw"]


class LiveMarkdownStream:
    """Streaming Markdown writer backed by ``rich.live.Live``.

    Don't instantiate directly — use :meth:`get_stream`, which mirrors
    ``textual.widgets.Markdown.get_stream(widget)``.

    Caveats:
      Live repaints in place. Documents that grow past the visible
      terminal area will overflow / scroll oddly — for long content
      use a scrollable widget (textual MarkdownViewer) instead.
    """

    # ── factory (mirrors textual's Markdown.get_stream) ─────────────────
    @classmethod
    def get_stream(
        cls,
        console: Console,
        *,
        mode: Mode = "markdown",
        debounce: float = 0.1,
        theme: Optional[Theme] = None,
        code_theme: str = "monokai",
    ) -> "LiveMarkdownStream":
        """Create a started stream against ``console``. Caller is
        responsible for ``stop()`` (or use the returned object as a
        context manager)."""
        stream = cls(
            console=console,
            mode=mode,
            debounce=debounce,
            theme=theme,
            code_theme=code_theme,
        )
        stream.start()
        return stream

    def __init__(
        self,
        console: Console,
        mode: Mode = "markdown",
        debounce: float = 0.1,
        theme: Optional[Theme] = None,
        code_theme: str = "monokai",
    ) -> None:
        self.console = console
        self.mode: Mode = mode
        self.debounce = debounce
        self._theme = theme
        self._code_theme = code_theme
        self._buf = ""
        self._last_render = 0.0
        self._live: Optional[Live] = None
        self._stopped = False
        # ── incremental-parse state (markdown mode only) ────────────────
        # _last_parsed_line: line index in _buf up to which we've
        #   committed rendered output (won't change on further appends).
        # _committed_ansi: rendered ANSI for lines [0 : _last_parsed_line].
        # _parser: shared MarkdownIt instance — re-using across writes
        #   is fine since parse() is stateless.
        self._last_parsed_line = 0
        self._committed_ansi = ""
        self._parser = MarkdownIt("gfm-like") if mode == "markdown" else None

    # ── public API (mirrors textual's MarkdownStream) ───────────────────
    def start(self) -> None:
        """Open the underlying Live context."""
        if self._live is not None:
            return
        self._live = Live(
            self._build_renderable(),
            console=self.console,
            auto_refresh=False,
            transient=False,
        )
        self._live.__enter__()

    def write(self, chunk: str) -> None:
        """Append a markdown fragment. Commits any newly-complete
        blocks to the cached prefix; re-renders the live view if
        the debounce window has elapsed. Empty chunks are no-ops."""
        if self._stopped:
            raise RuntimeError("Can't write to the stream after it has stopped.")
        if not chunk:
            return
        self._buf += chunk
        self._commit_complete_blocks()
        now = time.monotonic()
        if now - self._last_render >= self.debounce:
            self._refresh()
            self._last_render = now

    def stop(self) -> None:
        """Final commit + render + close the Live context."""
        if self._stopped:
            return
        self._stopped = True
        if self._live is not None:
            try:
                self._commit_complete_blocks()
                self._refresh()
            finally:
                self._live.__exit__(None, None, None)
                self._live = None

    @property
    def buffer(self) -> str:
        """The accumulated text seen so far."""
        return self._buf

    # ── context-manager sugar ───────────────────────────────────────────
    def __enter__(self) -> "LiveMarkdownStream":
        self.start()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self.stop()

    # ── incremental-parse internals ─────────────────────────────────────
    def _commit_complete_blocks(self) -> None:
        """Mirrors textual's Markdown.append's commit logic. Parses only
        the source from ``_last_parsed_line`` onward, finds the start
        of the LAST top-level token, and freezes everything before it
        into ``_committed_ansi``. The trailing block remains mutable —
        future writes may extend it."""
        if self._parser is None:  # raw mode: nothing to commit
            return
        lines = self._buf.splitlines(keepends=True)
        if self._last_parsed_line >= len(lines):
            return
        new_source = "".join(lines[self._last_parsed_line :])
        if not new_source:
            return
        tokens = self._parser.parse(new_source)
        # Find the start line of the last top-level token. That's the
        # boundary: everything before is "complete enough" to commit.
        last_block_start: Optional[int] = None
        for token in reversed(tokens):
            if token.map is not None and token.level == 0:
                last_block_start = token.map[0]
                break
        if last_block_start is None or last_block_start == 0:
            # No fully-completed blocks since the last commit.
            return
        committed_source = "".join(
            lines[self._last_parsed_line : self._last_parsed_line + last_block_start]
        )
        if committed_source:
            self._committed_ansi += self._render_to_ansi(committed_source)
        self._last_parsed_line += last_block_start

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._build_renderable(), refresh=True)

    def _build_renderable(self) -> RenderableType:
        """Compose the current frame: cached committed-ANSI prefix +
        freshly-rendered trailing (incomplete) block."""
        if self.mode == "raw":
            return Text.from_ansi(self._buf)
        # markdown mode: prefix + trailing render
        lines = self._buf.splitlines(keepends=True)
        trailing_source = "".join(lines[self._last_parsed_line :])
        trailing_ansi = self._render_to_ansi(trailing_source) if trailing_source else ""
        return Text.from_ansi(self._committed_ansi + trailing_ansi)

    def _render_to_ansi(self, markdown_source: str) -> str:
        """Render a markdown fragment to a captured ANSI string at the
        Console's current width. Used for both the frozen-prefix cache
        and the trailing-block per-tick render."""
        sink = io.StringIO()
        Console(
            file=sink,
            force_terminal=True,
            color_system="truecolor",
            width=self.console.size.width,
            legacy_windows=False,
            theme=self._theme,
        ).print(Markdown(markdown_source, code_theme=self._code_theme), end="")
        return sink.getvalue()
