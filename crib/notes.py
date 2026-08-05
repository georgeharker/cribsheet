"""Note files: frontmatter parse/serialize, atomic save, id assignment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .util import new_ulid

_FENCE = "---"
_KEY_RE = re.compile(r"^([A-Za-z0-9_][\w-]*):")
_ID_RE = re.compile(r"^id:\s*(\S+)\s*$", re.MULTILINE)


class NoteParseError(ValueError):
    """A note's frontmatter isn't parseable YAML — a hand edit, or conflict markers
    left by a merge. Distinguished from any other failure (and carrying the file it
    came from) so a reconcile sweep can skip that ONE note and name it, instead of
    dying on the whole project."""

    def __init__(self, path: str | Path | None, cause: Exception) -> None:
        self.path = str(path) if path is not None else None
        self.cause = cause
        where = f" in {self.path}" if self.path else ""
        super().__init__(f"malformed frontmatter{where}: {cause}")


@dataclass
class Note:
    path: Path
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def id(self) -> str | None:
        return self.frontmatter.get("id")

    @property
    def title(self) -> str | None:
        return self.frontmatter.get("title")

    @property
    def tags(self) -> list[str]:
        t = self.frontmatter.get("tags") or []
        return list(t) if isinstance(t, (list, tuple)) else [t]


def parse(text: str, source: str | Path | None = None) -> tuple[dict[str, Any], str]:
    """Split a markdown string into (frontmatter dict, body). Raises
    `NoteParseError` (naming `source`, when given) if the frontmatter is not YAML."""
    if text.startswith(_FENCE + "\n") or text.startswith(_FENCE + "\r\n"):
        lines = text.splitlines(keepends=True)
        for i in range(1, len(lines)):
            if lines[i].strip() == _FENCE:
                fm_text = "".join(lines[1:i])
                body = "".join(lines[i + 1:])
                try:
                    fm = yaml.safe_load(fm_text) or {}
                except yaml.YAMLError as e:
                    raise NoteParseError(source, e) from e
                if not isinstance(fm, dict):
                    fm = {}
                return fm, body.lstrip("\n")
    return {}, text


def scan_id(text: str) -> str | None:
    """The `id:` value lifted straight out of a note's frontmatter block by regex —
    for notes whose YAML no longer parses, so a repair write can still stash the
    corrupt bytes under the note's real identity."""
    if not text.startswith(_FENCE):
        return None
    end = text.find(f"\n{_FENCE}", len(_FENCE))
    m = _ID_RE.search(text[:end] if end > 0 else text)
    return m.group(1).strip("'\"") if m else None


def serialize(frontmatter: dict[str, Any], body: str) -> str:
    if not frontmatter:
        return body if body.endswith("\n") else body + "\n"
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).rstrip("\n")
    return f"{_FENCE}\n{fm}\n{_FENCE}\n\n{body.lstrip(chr(10))}".rstrip("\n") + "\n"


def _top_level_entries(fm_text: str) -> list[tuple[str, list[str]]]:
    """Split a frontmatter block into ordered (key, raw-lines) entries. A key is
    a line starting in column 0 with `key:`; following indented/blank lines belong
    to it (lists, nested maps). Structure-agnostic — it groups text, doesn't parse
    YAML — so a duplicated key from a botched merge stays detectable."""
    entries: list[tuple[str, list[str]]] = []
    cur: tuple[str, list[str]] | None = None
    for ln in fm_text.splitlines():
        if (m := _KEY_RE.match(ln)):
            if cur:
                entries.append(cur)
            cur = (m.group(1), [ln])
        elif cur:
            cur[1].append(ln)
    if cur:
        entries.append(cur)
    return entries


def normalize_text(text: str) -> tuple[str, bool]:
    """Self-heal duplicate-key frontmatter left by a git merge of two machines'
    provenance (DESIGN §14). A no-op unless a top-level key actually repeats — so
    well-formed notes are never rewritten.

    Resolution is *order-independent* so every machine converges on the same
    bytes: a duplicated scalar collapses to its lexicographic minimum (for `id`
    the earliest, for `imported`/`synced` the first-import date), and a duplicated
    structured value keeps the first occurrence. Returns (text, changed).
    """
    if not (text.startswith(_FENCE + "\n") or text.startswith(_FENCE + "\r\n")):
        return text, False
    lines = text.splitlines(keepends=True)
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == _FENCE), None)
    if end is None:
        return text, False
    fm_text = "".join(lines[1:end])
    body = "".join(lines[end + 1:])

    groups: dict[str, list[list[str]]] = {}
    order: list[str] = []
    for key, elines in _top_level_entries(fm_text):
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(elines)
    if all(len(g) == 1 for g in groups.values()):
        return text, False  # no duplicates → leave the note untouched

    chosen: list[str] = []
    for key in order:
        g = groups[key]
        if len(g) == 1 or not all(len(e) == 1 for e in g):
            chosen.extend(g[0])             # single, or structured → keep first
        else:
            chosen.extend(min(g))           # scalar → deterministic (lexical) min
    try:
        fm = yaml.safe_load("\n".join(chosen)) or {}
    except yaml.YAMLError:
        return text, False                  # don't risk worsening a bad merge
    if not isinstance(fm, dict):
        return text, False
    return serialize(fm, body), True


def heal_file(path: Path) -> bool:
    """Normalize a note's frontmatter on disk if a merge duplicated keys.
    Returns True if the file was rewritten. Idempotent and cheap on clean notes."""
    text = path.read_text()
    healed, changed = normalize_text(text)
    if changed:
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(healed)
        os.replace(tmp, path)
    return changed


def load(path: Path) -> Note:
    fm, body = parse(path.read_text(), path)
    return Note(path=path, frontmatter=fm, body=body)


def save_atomic(note: Note) -> None:
    """Write via temp + os.replace so the watcher sees one atomic change."""
    note.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = note.path.with_name(f".{note.path.name}.tmp")
    tmp.write_text(serialize(note.frontmatter, note.body))
    os.replace(tmp, note.path)


def ensure_id(note: Note) -> bool:
    """Assign a ULID `id` if absent. Returns True if the note was mutated."""
    if not note.frontmatter.get("id"):
        # id first for readability; rebuild dict to keep it at the top
        note.frontmatter = {"id": new_ulid(), **note.frontmatter}
        return True
    return False
