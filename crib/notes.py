"""Note files: frontmatter parse/serialize, atomic save, id assignment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .util import new_ulid

_FENCE = "---"


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


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown string into (frontmatter dict, body)."""
    if text.startswith(_FENCE + "\n") or text.startswith(_FENCE + "\r\n"):
        lines = text.splitlines(keepends=True)
        for i in range(1, len(lines)):
            if lines[i].strip() == _FENCE:
                fm_text = "".join(lines[1:i])
                body = "".join(lines[i + 1:])
                fm = yaml.safe_load(fm_text) or {}
                if not isinstance(fm, dict):
                    fm = {}
                return fm, body.lstrip("\n")
    return {}, text


def serialize(frontmatter: dict[str, Any], body: str) -> str:
    if not frontmatter:
        return body if body.endswith("\n") else body + "\n"
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).rstrip("\n")
    return f"{_FENCE}\n{fm}\n{_FENCE}\n\n{body.lstrip(chr(10))}".rstrip("\n") + "\n"


def load(path: Path) -> Note:
    fm, body = parse(path.read_text())
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
