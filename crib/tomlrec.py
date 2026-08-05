"""Escaping for the hand-rendered flat-TOML records (symbol_index, keyword_index).

Two stores render their own tiny TOML by hand — deterministic key order and one
array entry per line, so re-serializing identical content never yields a spurious
git diff, and no toml *writer* dependency sits on the hot path. Both need the same
escape/unescape pair, and the two copies drifted: neither escaped `\\n`, so one
embedded newline in an LLM description truncated its own record and made the
following lines parse as bogus keys.

One codec, used by both. `esc` emits a TOML *basic-string body* (no surrounding
quotes) — every character that TOML forbids raw is escaped, so `"{esc(s)}"` is
always valid TOML and `tomllib` can be the reader. `unesc` is its exact inverse:
`unesc(esc(s)) == s` for ANY string (the round-trip property test is the contract).
"""
from __future__ import annotations

import os
from pathlib import Path

# The named escapes, in TOML's own spelling. A char-wise pass applies them, which
# is the order-independent form of "backslash first, then quote, then \n \r \t" —
# an escape we emit can never be re-escaped by a later rule.
_ESC = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t",
        "\b": "\\b", "\f": "\\f"}
_UNESC = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t",
          "b": "\b", "f": "\f"}


def esc(s: str) -> str:
    """`s` as a TOML basic-string body (quotes NOT included). Control characters
    with no named escape go out as `\\uXXXX` — TOML rejects them raw, and a stray
    ESC/NUL in generated text would otherwise poison the whole record."""
    out: list[str] = []
    for ch in s:
        named = _ESC.get(ch)
        if named is not None:
            out.append(named)
        elif ch < " " or ch == "\x7f":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def unesc(s: str) -> str:
    """Exact inverse of `esc`. Scans left to right so a backslash consumes the char
    that follows it — a naive reverse-order `str.replace` chain does NOT invert
    (`\\\\n` would come back as backslash+newline instead of backslash+`n`). An
    unrecognized escape is left verbatim, so legacy/foreign content survives."""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt in _UNESC:
                out.append(_UNESC[nxt])
                i += 2
                continue
            if nxt == "u" and i + 6 <= n:
                try:
                    out.append(chr(int(s[i + 2:i + 6], 16)))
                except ValueError:
                    out.append(ch)
                    i += 1
                    continue
                i += 6
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def write_atomic(path: Path, text: str) -> None:
    """Write via a sibling temp + `os.replace` (`notes.save_atomic`'s pattern). A
    reader — a concurrent query, `git`, the merge driver — never observes half a
    record, and a crash mid-write leaves the previous one intact. Truncation is the
    one corruption these stores cannot cheaply undo: the reader can only mark the
    record dirty and wait for a regeneration, so don't create it. The temp name is
    dot-prefixed and keeps its own suffix, so it never matches a `*.toml` glob."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def unquote(v: str) -> str:
    """Strip exactly one pair of delimiter quotes (not `.strip('"')`, which over-eats
    a trailing escaped quote) and un-escape the contents."""
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
    return unesc(v)
