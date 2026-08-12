"""Errors that are ANSWERS, not crashes.

A `CribUserError` says the caller asked for something impossible or
under-specified — an ambiguous symbol, an unknown ref, a heading that isn't in the
doc. Its MESSAGE is the product: it names what was wrong and what to do instead,
because that text is what a human reads on stderr and what a model reads out of a
tool result. Neither is served by a traceback, and on the daemon path the traceback
is client plumbing anyway — the real stack lives in the daemon's log.

So the two surfaces treat this base class as a delivery instruction:

- the CLI prints the message and exits non-zero, instead of dumping frames
- the MCP layer re-raises it as `ToolError`, which FastMCP renders verbatim
  (anything else it is free to mask, which would drop exactly the part that
  mattered — a candidate list nobody can see is the same defect as no candidates)

Subclass this for any condition the caller can FIX by asking differently. Leave
genuine bugs as plain exceptions: those want the traceback, and treating them as
answers is how a broken tool starts reporting itself as a bad question.

Subclasses ValueError so existing `except ValueError` handling keeps working.
"""

from __future__ import annotations


class CribUserError(ValueError):
    """The caller can fix this by asking differently. The message says how."""
