"""Frontmatter-aware git merge driver (DESIGN §14).

Registered as `merge=cribnote` via `.gitattributes`, so `git pull` resolves note
*headers* deterministically — provenance never conflicts — while still surfacing
genuine *body* conflicts. The merged-clean header is written to the working file
even when the body conflicts, so a surfaced note already has a resolved header and
only body `<<<<<<<` markers for the user to settle.

This runs inside `git pull`, so it stays light: yaml + `git merge-file`, no index.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

from . import notes

_MISSING: Any = object()


def _canon(v: Any) -> str:
    """Order-independent comparison key for a frontmatter value."""
    return yaml.safe_dump(v, sort_keys=True, allow_unicode=True)


def _resolve(o: Any, a: Any, b: Any) -> Any:
    """3-way resolve one key. Symmetric in (a, b) — both machines must land on the
    same value regardless of which side git labels 'ours' — so the result never
    re-conflicts on the next sync."""
    if a == b:
        return a                    # agree (incl. both absent)
    if a == o:
        return b                    # ours unchanged from base → take theirs
    if b == o:
        return a                    # theirs unchanged from base → take ours
    if a is _MISSING:
        return b                    # modify/delete → keep the surviving value
    if b is _MISSING:
        return a
    # both sides changed to different values → deterministic, symmetric pick
    # (for id/date fields this is the lexical min, i.e. earliest/first-import)
    return a if _canon(a) <= _canon(b) else b


def merge_frontmatter(base: dict, ours: dict, theirs: dict) -> dict:
    """Union the three headers, resolving each key deterministically. Key order is
    stable across machines: id first, then base order, then any new keys sorted."""
    keys = set(base) | set(ours) | set(theirs)
    merged: dict = {}
    for k in keys:
        v = _resolve(base.get(k, _MISSING), ours.get(k, _MISSING),
                     theirs.get(k, _MISSING))
        if v is not _MISSING:
            merged[k] = v
    out: dict = {}
    if "id" in merged:
        out["id"] = merged["id"]
    for k in base:                  # base is common to both machines → stable order
        if k in merged and k not in out:
            out[k] = merged[k]
    for k in sorted(merged):
        if k not in out:
            out[k] = merged[k]
    return out


def _merge_body(base: str, ours: str, theirs: str) -> tuple[str, bool]:
    """3-way merge bodies via `git merge-file`. Returns (text, conflicted)."""
    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        (dp / "ours").write_text(ours)
        (dp / "base").write_text(base)
        (dp / "theirs").write_text(theirs)
        r = subprocess.run(
            ["git", "merge-file", "-p",
             "-L", "ours", "-L", "base", "-L", "theirs",
             str(dp / "ours"), str(dp / "base"), str(dp / "theirs")],
            capture_output=True, text=True)
    # returncode: 0 = clean, >0 = number of conflict hunks, <0 = error
    return r.stdout, r.returncode != 0


def merge_note_texts(base: str, ours: str, theirs: str) -> tuple[str, bool]:
    """Merge two note versions: header deterministically, body 3-way. Returns
    (merged_text, body_conflicted)."""
    base_fm, base_body = notes.parse(base)
    ours_fm, ours_body = notes.parse(ours)
    theirs_fm, theirs_body = notes.parse(theirs)
    fm = merge_frontmatter(base_fm, ours_fm, theirs_fm)
    body, conflicted = _merge_body(base_body, ours_body, theirs_body)
    return notes.serialize(fm, body), conflicted


def run_driver(base_path: str, current_path: str, other_path: str) -> int:
    """git merge-driver entry: merge into %A (current) in place, exit 0 when the
    body merged clean (git marks it resolved) or 1 when a body conflict remains
    (git leaves it unmerged → surfaced). On any error, leave %A untouched and
    report a conflict so a bad merge is surfaced, never silently lost."""
    current = Path(current_path)
    try:
        merged, conflicted = merge_note_texts(
            Path(base_path).read_text(),
            current.read_text(),
            Path(other_path).read_text(),
        )
    except Exception:
        return 1
    current.write_text(merged)
    return 1 if conflicted else 0
