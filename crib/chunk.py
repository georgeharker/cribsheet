"""Chunking: per-heading sections with a windowed fallback (DESIGN §3)."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from .util import sha1_hex

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")

# Identity scheme of the chunks this module emits. Bump whenever a change here
# alters `chunk_id` for unchanged text — the store compares its recorded version
# against this and reconciles (re-embeds under the new ids, deletes the stale
# ones) when they differ. v2: duplicate heading paths within one note get a
# `#<n>` occurrence disambiguator in the id (v1 collided, later section winning).
CHUNK_SCHEMA_VERSION = 2

# Windowing is measured in whitespace words, but the cap is set so a window
# stays under the embedding models' 512-*token* limit (bge et al.) — markdown
# and code run well above one token per word, so a 512-word window would be
# silently truncated by the model, dropping the tail from the index. ~320 words
# (~480-510 tokens at typical prose/code density) keeps the whole window
# embeddable; smaller windows also sharpen per-section relevance.
# NOTE: changing these re-chunks notes — run `crib reindex` to apply to existing
# docs (new/edited notes pick it up automatically; the hash gate makes it safe).
WINDOW_WORDS = 320
WINDOW_OVERLAP = 64


def section_key(heading_path: Sequence[str] | str, occurrence: int = 1) -> str:
    """Identity key of a section: its heading breadcrumb, with a `#<n>` suffix
    for the 2nd+ section in a note whose *effective* heading path is identical
    (a repeated `### Notes`, or two nestings that flatten to the same stack).

    Without it those sections share a `chunk_id` and the later one overwrites the
    earlier (and `section_line_map` kept only the first span). Occurrence is
    counted in document order, so the key is deterministic across reindexes.
    First occurrence keeps the bare breadcrumb, so ids of the overwhelmingly
    common (unique-heading) case are unchanged, as are all display paths — the
    disambiguator lives in identity only, never in the visible `heading_path`.
    """
    key = heading_path if isinstance(heading_path, str) else "/".join(heading_path)
    return key if occurrence <= 1 else f"{key}#{occurrence}"


@dataclass
class Chunk:
    project: str
    relpath: str
    note_id: str
    heading_path: list[str]
    window_idx: int
    text: str
    # Full-section text this window came from — set by `chunk_note`. Identity for
    # section-level LLM elaborations: hashing it (not the window) keeps those
    # invariant to re-windowing, so an expensive asset isn't regenerated when the
    # window size changes. Falls back to `text` for a one-window section.
    section_text: str = ""
    # 1-based count of sections before this one (inclusive) sharing its heading
    # path — see `section_key`. Set by `chunk_note`; 1 for a unique heading.
    occurrence: int = 1

    @property
    def chunk_id(self) -> str:
        return sha1_hex(
            self.project, self.relpath,
            section_key(self.heading_path, self.occurrence),
            str(self.window_idx),
        )

    @property
    def section_hash(self) -> str:
        """Stable id of the whole section (heading + full body), invariant to how
        the section is windowed — the key elaborations are stored under."""
        return sha1_hex("/".join(self.heading_path), self.section_text or self.text)

    @property
    def index_text(self) -> str:
        """Text fed to the embedder and BM25 — the heading breadcrumb (a free,
        authored topic phrase) prepended to the section body, so a section's
        *subject* (often named only in its heading, absent from its prose) is
        searchable. The stored `document` stays the clean body; this shapes
        retrieval only. See docs/retrieval-and-adoption.md §3."""
        if not self.heading_path:
            return self.text
        return " › ".join(self.heading_path) + "\n\n" + self.text

    @property
    def content_hash(self) -> str:
        # Hash the index text, not the bare body, so changing the enrichment
        # scheme (or a heading) re-embeds existing chunks on the next reindex.
        return sha1_hex(self.index_text)

    def metadata(self, title: str | None, tags: list[str], source: str,
                 mtime: float, note_type: str = "") -> dict:
        return {
            "project": self.project,
            "relpath": self.relpath,
            "note_id": self.note_id,
            "title": title or "",
            "tags": ",".join(tags),
            # The note's frontmatter `type` (e.g. design/plan), so a facet's notes
            # are filterable at query time (`note_lookup(tags=["design"])` matches
            # it like a tag). Metadata only — `chunk_id` is (project, relpath,
            # section_key, window_idx), so adding a field re-stamps metadata on the
            # next reindex without changing an id: no CHUNK_SCHEMA_VERSION bump.
            "type": note_type or "",
            # Display/lookup key stays the clean breadcrumb; `occurrence` carries
            # the disambiguator so a consumer can rebuild the identity key with
            # `section_key(heading_path, occurrence)` (e.g. to hit the right span
            # in `section_line_map`).
            "heading_path": "/".join(self.heading_path),
            "occurrence": self.occurrence,
            "window_idx": self.window_idx,
            "content_hash": self.content_hash,
            "section_hash": self.section_hash,
            "source": source,
            "file_mtime": mtime,
        }


class _Line(NamedTuple):
    """One scanned line. `heading_path` is None unless the line is a markdown
    heading *outside* a fenced code block — i.e. one that opens a section."""
    idx: int                            # 0-based index into the scanned lines
    text: str
    heading_path: list[str] | None
    occurrence: int                     # 1-based, per heading_path key


def _scan(lines: Sequence[str], start: int = 0) -> Iterator[_Line]:
    """Single pass over markdown lines, yielding every line and — for heading
    lines — the heading stack it opens plus that path's occurrence count.

    The one place fenced code blocks, heading nesting and the occurrence counter
    are tracked, so `_split_sections` (chunk identity) and `section_line_map`
    (line spans) cannot drift apart: they consumed hand-duplicated copies of this
    state machine before, and a divergent counter would point a chunk at another
    section's lines.
    """
    stack: list[tuple[int, str]] = []    # (level, title)
    seen: dict[str, int] = {}
    in_fence = False
    fence = ""                           # the ``` or ~~~ run that opened it
    for idx in range(start, len(lines)):
        line = lines[idx]
        stripped = line.lstrip()
        # Track fenced code blocks so `#`-comments inside them aren't parsed as
        # markdown headings (config-heavy docs otherwise get bogus sections).
        if not in_fence and (stripped.startswith("```") or stripped.startswith("~~~")):
            in_fence, fence = True, stripped[:3]
            yield _Line(idx, line, None, 0)
            continue
        if in_fence:
            if stripped.startswith(fence):
                in_fence = False
            yield _Line(idx, line, None, 0)
            continue
        m = _HEADING.match(line)
        if not m:
            yield _Line(idx, line, None, 0)
            continue
        level = len(m.group(1))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, m.group(2).strip()))
        path = [t for _, t in stack]
        key = "/".join(path)
        seen[key] = seen.get(key, 0) + 1
        yield _Line(idx, line, path, seen[key])


class Section(NamedTuple):
    heading_path: list[str]
    occurrence: int
    text: str


def _split_sections(body: str) -> list[Section]:
    """Split markdown into (heading_path, occurrence, section_text) by heading
    lines. `occurrence` disambiguates repeated heading paths — see `section_key`."""
    sections: list[Section] = []
    cur: list[str] = []
    heading_path: list[str] = []
    occurrence = 1

    def flush():
        text = "\n".join(cur).strip()
        if text:
            sections.append(Section(list(heading_path), occurrence, text))

    for ln in _scan(body.splitlines()):
        if ln.heading_path is None:
            cur.append(ln.text)
            continue
        flush()
        cur = []
        heading_path, occurrence = ln.heading_path, ln.occurrence
    flush()
    return sections


def _window(text: str, window_words: int = WINDOW_WORDS,
            overlap: int = WINDOW_OVERLAP) -> list[str]:
    words = text.split()
    if len(words) <= window_words:
        return [text]
    out, start = [], 0
    step = max(1, window_words - overlap)   # guard: overlap < window keeps step > 0
    while start < len(words):
        out.append(" ".join(words[start:start + window_words]))
        start += step
    return out


def chunk_note(project: str, relpath: str, note_id: str, body: str,
               window_words: int = WINDOW_WORDS,
               overlap: int = WINDOW_OVERLAP) -> list[Chunk]:
    """Per-heading sections, windowed if long; whole-body fallback otherwise."""
    sections = _split_sections(body)
    if not sections:
        stripped = body.strip()
        sections = [Section([], 1, stripped)] if stripped else []

    chunks: list[Chunk] = []
    for heading_path, occurrence, text in sections:
        # `text` is the full section; each window carries it so `section_hash` is
        # window-invariant.
        for i, win in enumerate(_window(text, window_words, overlap)):
            chunks.append(Chunk(project, relpath, note_id, heading_path, i, win,
                                section_text=text, occurrence=occurrence))
    return chunks


def section_line_map(text: str) -> dict[str, tuple[int, int]]:
    """Map each section's heading_path key -> (start_line, end_line) as 1-based
    file lines, computed from the raw file on disk (frontmatter skipped).

    Keys are `section_key(heading_path, occurrence)` — the same identity key the
    `chunk_id` is built from, which for the usual unique heading is exactly the
    "/".join(heading_path) stored in chunk metadata, so a lookup hit resolves to
    its span in the *current* file. A note that repeats a heading path gets one
    span per occurrence (`A/Notes`, `A/Notes#2`, …) rather than the first one
    only; rebuild the key from metadata with
    `section_key(meta["heading_path"], meta.get("occurrence", 1))`.

    Computed at query time rather than indexed, so the lines never go stale when
    edits above a section shift it (the hash gate leaves such chunks untouched).
    The start line is the heading itself (or the first body line for the
    pre-heading section); the end is the line before the next heading. A long,
    windowed section reports one span for all its windows — its full extent.
    """
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":           # skip YAML frontmatter
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                start = j + 1
                break

    out: dict[str, tuple[int, int]] = {}
    key = ""                       # the pre-heading section
    sec_start = start              # 0-based line where the current section opens
    has_content = False

    def close(end_idx: int) -> None:
        # `key not in out`: keys are unique by construction (the occurrence
        # counter), so this only bites the pathological case of a literal heading
        # spelled like a disambiguator ("Notes#2" alongside a repeated "Notes").
        if has_content and key not in out and end_idx >= sec_start:
            out[key] = (sec_start + 1, end_idx + 1)

    # Same `_scan` as `_split_sections`, so headings inside fenced code blocks are
    # ignored identically and the occurrence numbering matches the chunk ids.
    for ln in _scan(lines, start):
        if ln.heading_path is None:
            if ln.text.strip():
                has_content = True
            continue
        close(ln.idx - 1)
        key = section_key(ln.heading_path, ln.occurrence)
        sec_start = ln.idx
        has_content = True         # the heading line itself anchors the section
    close(len(lines) - 1)
    return out
