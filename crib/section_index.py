"""Section-addressed LLM index assets (§3, §3.1).

Two kinds of generated enrichment, one shape:

- **keyword_index** — search terms/phrases per section, tokenized into the BM25
  corpus (sparse side). `crib elaborate <label>`.
- **summary_index** — LLM rephrasings per section, embedded as alias vectors on
  the dense side so a paraphrased query matches with zero shared tokens.
  `crib summarize <label>`.

Both store a git-tracked, section-addressed TOML per `(label, section)`:
`<project>/<root>/<label>/<section_hash>.toml` with a `terms = [...]` list — so an
asset recomputes only when the section changes, survives an index rebuild, never
merge-conflicts (same section+label → same path → byte-identical), and is
window-invariant (keyed by section, not chunk). See docs/retrieval-and-adoption.md.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

# Built-in keyword_index prompts (`crib elaborate`). A `[elaborate.<label>].prompt`
# config entry overrides one of these, or defines a new label.
KEYWORD_PROMPTS: dict[str, str] = {
    "keywords": (
        "You expand a documentation section for keyword search. List the search "
        "terms, synonyms, and short phrases a user might type to find THIS "
        "section — especially words that are NOT already in the text (the "
        "vocabulary a searcher would reach for). One term per line, lowercase, "
        "no numbering, no prose, no preamble. 8-15 lines."
    ),
    "questions": (
        "List the natural-language questions this section answers. One question "
        "per line, no numbering, no preamble. 5-10 lines."
    ),
    "phrase": (
        "State, in one short line, the single topic this section is about — the "
        "canonical phrase a reader would use to name its subject. One line only, "
        "no preamble."
    ),
}

# Built-in summary_index prompts (`crib summarize`). Each output LINE becomes one
# alias vector on the dense side, so lines should be shaped like *queries* a user
# would actually type (doc2query) — not descriptions, which cluster together and
# match everything loosely. Specific to THIS section, in the querier's words.
SUMMARY_PROMPTS: dict[str, str] = {
    "summary": (
        "You generate search queries for one documentation section. Write 4 short, "
        "SPECIFIC questions or search phrases a user would type when THIS section — "
        "and not a generic overview — is the answer they need. Anchor each to the "
        "section's distinctive content: its named mechanisms, decisions, parameters, "
        "or terms. Do NOT describe or summarize the section; do NOT write generic "
        "phrases that would fit the whole project. Vary the wording across the four. "
        "One query per line, lowercase, no numbering, no preamble."
    ),
}

_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


def parse_terms(text: str) -> list[str]:
    """Free-text LLM output → a clean, de-duplicated list of lines. Strips
    bullets, numbering, code fences, quotes, blanks. Plain text + trivial parse
    beats brittle JSON-in-prose for a flat list."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        term = _BULLET.sub("", line).strip().strip('"').strip("'").strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return out


def resolve_prompt(label: str, config_table: dict, builtins: dict[str, str]
                   ) -> str | None:
    """Prompt for a label: a `[<verb>.<label>].prompt` config entry wins, else the
    builtin for that kind. None if neither defines it."""
    entry = config_table.get(label) or {}
    if entry.get("prompt"):
        return str(entry["prompt"])
    return builtins.get(label)


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _render_toml(section_hash: str, label: str, terms: list[str],
                 relpath: str, heading: str, model: str) -> str:
    """Deterministic TOML — stable key order, one entry per array line — so
    re-serializing identical content never yields a spurious git diff."""
    lines = [f'section_hash = "{_esc(section_hash)}"', f'label = "{_esc(label)}"']
    if relpath:
        lines.append(f'relpath = "{_esc(relpath)}"')
    if heading:
        lines.append(f'heading = "{_esc(heading)}"')
    if model:
        lines.append(f'model = "{_esc(model)}"')
    lines.append("terms = [")
    lines.extend(f'  "{_esc(t)}",' for t in terms)
    lines.append("]")
    return "\n".join(lines) + "\n"


class SectionIndex:
    """Section-addressed TOML store rooted at ``<project_dir>/<root_name>/``.

    Sibling to ``notes/`` so the watcher/indexer never treat it as notes;
    git-tracked (expensive LLM output travels with the notes, §14). Used for both
    ``keyword_index`` and ``summary_index`` — same on-disk shape, different root.
    """

    def __init__(self, project_dir: Path, root_name: str = "keyword_index") -> None:
        self.root = project_dir / root_name

    def path(self, label: str, section_hash: str) -> Path:
        return self.root / label / f"{section_hash}.toml"

    def has(self, label: str, section_hash: str) -> bool:
        return self.path(label, section_hash).exists()

    def read_terms(self, label: str, section_hash: str) -> list[str]:
        p = self.path(label, section_hash)
        if not p.exists():
            return []
        try:
            data = tomllib.loads(p.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            return []
        terms = data.get("terms")
        return [str(t) for t in terms] if isinstance(terms, list) else []

    def terms_for(self, section_hash: str, labels: list[str]) -> list[str]:
        """Union of entries across `labels` for one section."""
        out: list[str] = []
        for label in labels:
            out.extend(self.read_terms(label, section_hash))
        return out

    def write(self, label: str, section_hash: str, terms: list[str], *,
              relpath: str = "", heading: str = "", model: str = "") -> Path:
        p = self.path(label, section_hash)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_render_toml(section_hash, label, terms, relpath, heading, model))
        return p

    def prune(self, label: str, live: set[str], before: float | None = None) -> int:
        """GC entries whose section_hash is no longer live. The store is
        content-addressed and never overwrites in place — an edited section gets a
        NEW hash, orphaning the old entry — so orphans accumulate until a pass that
        knows the FULL live set (a project-wide elaborate/summarize) prunes them.
        `before` (epoch seconds) spares entries written after the caller snapshotted
        `live`, so a note stored concurrently with the pass can't lose fresh terms."""
        d = self.root / label
        n = 0
        for p in (d.glob("*.toml") if d.is_dir() else []):
            if p.stem in live:
                continue
            try:
                if before is not None and p.stat().st_mtime >= before:
                    continue
                p.unlink()
                n += 1
            except OSError:  # noqa: PERF203 — vanished/locked file: skip, next pass gets it
                pass
        return n

    def labels(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(d.name for d in self.root.iterdir() if d.is_dir())
