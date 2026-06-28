"""mdrender — render markdown from a file or stdin through rich to stdout.

A standalone CLI that mirrors the rendering path used by
``bin/mdview``. Two roles:

  1. Validation tool: lets us see exactly how the rich pipeline
     renders a markdown stream, headlessly, without firing the LLM
     or the viewer's modal UI.
  2. Drop-in candidate for the question-mode ``formatter`` zstyle —
     replaces mdansi / glow / mdcat with the same rich renderer.

Two modes:

  --no-stream (default): consume all stdin, render once at EOF, emit
                         to stdout. Best for piped one-shot use.
  --stream             : re-render incrementally as stdin arrives
                         (rich.live.Live + Markdown). Best for live
                         use with a streaming producer such as the
                         bridge. Re-render is debounced so frequent
                         small chunks don't thrash the terminal.
                         Note: Live repaints in place, so very long
                         documents may overflow visible area — use
                         bin/mdview (alt-screen + scroll) for
                         documents larger than one screenful.

Theming: ``--theme-file`` points at an INI theme (see themes/*.ini).
Rich has no global theme config, so the file is loaded explicitly here
and applied to this Console only. Without it, Rich's stock markdown
styling is used.
"""

from __future__ import annotations

import argparse
import configparser
import os
import select
import sys
from typing import List, Optional, Tuple, cast

from rich.console import Console
from rich.markdown import Markdown
from rich.style import Style
from rich.text import Text
from rich.theme import Theme

from .stream import LiveMarkdownStream, Mode

# Rich's default Pygments style for fenced code blocks.
_DEFAULT_CODE_THEME = "monokai"


def _load_theme(path: Optional[str]) -> Tuple[Optional[Theme], str]:
    """Load a markdown Theme + Pygments code-theme name from an INI file.

    The INI has a ``[styles]`` section (Rich style names → style specs,
    layered onto Rich's defaults via ``inherit=True``) and an optional
    ``[code]`` section with ``theme = <pygments style>`` for fenced code
    blocks. Returns ``(None, "monokai")`` — Rich's stock look — when
    *path* is falsy or the file can't be read.
    """
    if not path:
        return None, _DEFAULT_CODE_THEME
    cfg = configparser.ConfigParser()
    if not cfg.read(path):
        return None, _DEFAULT_CODE_THEME
    theme: Optional[Theme] = None
    if cfg.has_section("styles"):
        styles = {name: Style.parse(value) for name, value in cfg.items("styles")}
        if styles:
            theme = Theme(styles, inherit=True)
    code_theme = cfg.get("code", "theme", fallback=_DEFAULT_CODE_THEME)
    return theme, code_theme


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="mdrender",
        description="Render markdown from a file or stdin through rich to stdout.",
    )
    p.add_argument(
        "--render",
        choices=["markdown", "raw"],
        default="markdown",
        help="Render mode (default: markdown). raw = ANSI passthrough.",
    )
    p.add_argument(
        "--stream",
        action="store_true",
        help="Re-render incrementally as stdin arrives (uses rich.live). "
        "Default: consume all stdin, render once at EOF.",
    )
    p.add_argument(
        "--debounce",
        type=float,
        default=0.1,
        help="Min seconds between re-renders in --stream mode (default 0.1).",
    )
    p.add_argument(
        "--color",
        choices=["always", "auto", "never"],
        default="auto",
        help="When to emit ANSI color (default: auto — colorized only "
        "if stdout is a tty). Use 'always' when piping to a pager "
        "or capturing to a file you'll view later.",
    )
    p.add_argument(
        "--theme-file",
        default=None,
        help="Path to an INI theme file (see themes/*.ini). Without it, "
        "Rich's default markdown styling is used.",
    )
    p.add_argument(
        "--pager",
        action="store_true",
        help="Pipe output through $PAGER (or `less -R` if unset). "
        "Implies --color always. Incompatible with --stream.",
    )
    p.add_argument(
        "file",
        nargs="?",
        default=None,
        help="Markdown source file. If omitted (or '-'), read from stdin.",
    )
    args = p.parse_args(argv)

    if args.pager and args.stream:
        print("mdrender: --pager and --stream are incompatible", file=sys.stderr)
        return 2

    theme, code_theme = _load_theme(args.theme_file)

    # Color resolution. rich's Console respects:
    #   force_terminal=True       — emit colors regardless of stdout tty
    #   no_color=True             — strip colors regardless of tty
    #   neither set               — auto-detect via isatty()
    force_terminal: Optional[bool] = None
    no_color: Optional[bool] = None
    if args.color == "always" or args.pager:
        force_terminal = True
    elif args.color == "never":
        no_color = True
    console = Console(force_terminal=force_terminal, no_color=no_color, theme=theme)

    # "-" is the conventional stdin sentinel; treat it like no file.
    source: Optional[str] = args.file if args.file not in (None, "-") else None

    try:
        mode = cast(Mode, args.render)
        if args.pager:
            # console.pager(styles=True) spawns $PAGER (or `less -RFX`)
            # and pipes our captured output through it on context exit.
            # styles=True ensures ANSI is preserved through the pipe.
            with console.pager(styles=True):
                return _run_oneshot(console, mode, code_theme, source)
        if args.stream:
            return _run_stream(console, mode, args.debounce, theme, code_theme, source)
        return _run_oneshot(console, mode, code_theme, source)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except OSError as e:
        print(f"mdrender: {e}", file=sys.stderr)
        return 1


def _run_oneshot(
    console: Console, mode: str, code_theme: str, source: Optional[str]
) -> int:
    if source is None:
        text = sys.stdin.read()
    else:
        with open(source, encoding="utf-8", errors="replace") as f:
            text = f.read()
    if mode == "markdown":
        console.print(Markdown(text, code_theme=code_theme))
    else:
        console.print(Text.from_ansi(text), end="")
    return 0


def _run_stream(
    console: Console,
    mode: Mode,
    debounce: float,
    theme: Optional[Theme],
    code_theme: str,
    source: Optional[str],
) -> int:
    """Read the source (stdin or a file) as a byte stream, feeding chunks
    to LiveMarkdownStream.

    Interface parallels textual's:

        stream = Markdown.get_stream(widget)
        for chunk in source: await stream.write(chunk)
        await stream.stop()

    We poll the raw fd with select() + non-blocking os.read() — using
    ``for line in sys.stdin`` would buffer ~4 KB at a pipe before
    yielding, killing the live-update feel through slow producers. A
    file (or fifo) path is opened directly; the same loop drains it.
    """
    if source is None:
        fd = sys.stdin.fileno()
        close_fd = False
    else:
        fd = os.open(source, os.O_RDONLY)
        close_fd = True
    stream = LiveMarkdownStream.get_stream(
        console, mode=mode, debounce=debounce, theme=theme, code_theme=code_theme
    )
    try:
        while True:
            select.select([fd], [], [], debounce)
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            stream.write(chunk.decode("utf-8", errors="replace"))
    finally:
        stream.stop()
        if close_fd:
            os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
