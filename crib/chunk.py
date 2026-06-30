"""Chunking: per-heading sections with a windowed fallback (DESIGN §3)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .util import sha1_hex

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")

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


@dataclass
class Chunk:
    project: str
    relpath: str
    note_id: str
    heading_path: list[str]
    window_idx: int
    text: str

    @property
    def chunk_id(self) -> str:
        return sha1_hex(
            self.project, self.relpath, "/".join(self.heading_path),
            str(self.window_idx),
        )

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
                 mtime: float) -> dict:
        return {
            "project": self.project,
            "relpath": self.relpath,
            "note_id": self.note_id,
            "title": title or "",
            "tags": ",".join(tags),
            "heading_path": "/".join(self.heading_path),
            "window_idx": self.window_idx,
            "content_hash": self.content_hash,
            "source": source,
            "file_mtime": mtime,
        }


def _split_sections(body: str) -> list[tuple[list[str], str]]:
    """Split markdown into (heading_path, section_text) by heading lines."""
    sections: list[tuple[list[str], str]] = []
    stack: list[tuple[int, str]] = []   # (level, title)
    cur: list[str] = []
    heading_path: list[str] = []

    def flush():
        text = "\n".join(cur).strip()
        if text:
            sections.append((list(heading_path), text))

    for line in body.splitlines():
        m = _HEADING.match(line)
        if m:
            flush()
            cur = []
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            heading_path = [t for _, t in stack]
        else:
            cur.append(line)
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
        sections = [([], stripped)] if stripped else []

    chunks: list[Chunk] = []
    for heading_path, text in sections:
        for i, win in enumerate(_window(text, window_words, overlap)):
            chunks.append(Chunk(project, relpath, note_id, heading_path, i, win))
    return chunks


def section_line_map(text: str) -> dict[str, tuple[int, int]]:
    """Map each section's heading_path key -> (start_line, end_line) as 1-based
    file lines, computed from the raw file on disk (frontmatter skipped).

    Keys are "/".join(heading_path) — the same value stored in chunk metadata —
    so a lookup hit resolves to its span in the *current* file. Computed at query
    time rather than indexed, so the lines never go stale when edits above a
    section shift it (the hash gate leaves such chunks untouched). The start line
    is the heading itself (or the first body line for the pre-heading section);
    the end is the line before the next heading. A long, windowed section reports
    one span for all its windows — its full extent.
    """
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":           # skip YAML frontmatter
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                start = j + 1
                break

    out: dict[str, tuple[int, int]] = {}
    stack: list[tuple[int, str]] = []
    key = ""                       # the pre-heading section
    sec_start = start              # 0-based line where the current section opens
    has_content = False

    def close(end_idx: int) -> None:
        if has_content and key not in out and end_idx >= sec_start:
            out[key] = (sec_start + 1, end_idx + 1)

    for idx in range(start, len(lines)):
        m = _HEADING.match(lines[idx])
        if m:
            close(idx - 1)
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
            key = "/".join(t for _, t in stack)
            sec_start = idx
            has_content = True     # the heading line itself anchors the section
        elif lines[idx].strip():
            has_content = True
    close(len(lines) - 1)
    return out
