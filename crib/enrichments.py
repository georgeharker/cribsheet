"""The enrichments facet: keyword_index and summary_index in the store.

The LLM enrichments (crib.section_index) are section-addressed TOMLs under a
project's global dir, `<tier>/<label>/<section_hash>.toml`, each a `terms`
list. They stay the git-synced truth. This facet maps them into the store
(design: "Unified item and edge tables in the DuckDB cache", Layering):

- `keywords`: one item per (label, section) carrying its terms, which the
  notes BM25 folds in (crib.retrieve.LexicalCache);
- `summary`: one item per alias line, whose `embed_text` is the line, so the
  vector step embeds each alias once and the vector persists across restarts
  (crib.retrieve.SummaryVectorCache scores them).

Freshness is the shared change detection (crib.filesync): a read refreshes
from the files whose stamp moved, and the watcher routes record changes here
(crib.watch.RECORD_TIERS).
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .embed import embed_batch
from .filesync import changes
from .items import Item
from .util import sha1_hex

# tier directory -> item kind
TIERS = {"keyword_index": "keywords", "summary_index": "summary"}


def _terms(path: Path) -> list[str]:
    """The `terms` of one enrichment TOML, as SectionIndex.read_terms reads it
    (a broken or term-less file has none)."""
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return []
    terms = data.get("terms")
    return [str(t) for t in terms] if isinstance(terms, list) else []


def _items(project: str, kind: str, name: str, terms: list[str]) -> list[Item]:
    """The rows one TOML (`<label>/<section_hash>.toml`) maps to."""
    label, _, file = name.replace("\\", "/").partition("/")
    section = file.removesuffix(".toml")
    if not label or not section or "/" in file:
        return []
    if kind == "keywords":
        return [
            Item(
                id=f"keywords:{project}:{label}:{section}",
                kind="keywords",
                project=project,
                relpath=name,
                title=label,
                content_hash=sha1_hex("\n".join(terms)),
                facet={"label": label, "section_hash": section, "terms": terms},
            )
        ]
    return [
        Item(
            id=f"summary:{project}:{label}:{section}:{n}",
            kind="summary",
            project=project,
            relpath=name,
            title=label,
            content_hash=sha1_hex(text),
            embed_text=text,
            facet={"label": label, "section_hash": section, "n": n},
        )
        for n, text in enumerate(terms)
    ]


class Enrichments:
    def __init__(
        self, store: Any, embedder: Any, project_dir: Callable[[str], Path]
    ) -> None:
        self.store = store
        self.embedder = embedder
        self._project_dir = project_dir

    def refresh(
        self, project: str, kind: str | None = None, only: list[str] | None = None
    ) -> bool:
        """Map the enrichment TOMLs that changed since the store last saw them
        (all tiers, or `kind`'s; `only`: just those `<label>/<hash>.toml`
        names). Returns whether anything changed."""
        moved = False
        for tier, k in TIERS.items():
            if kind is not None and k != kind:
                continue
            root = self._project_dir(project) / tier
            ch = changes(self.store, k, project, root, ".toml", only, recursive=True)
            if not ch:
                continue
            files = [
                (name, mtime, size, _items(project, k, name, _terms(root / name)))
                for name, (mtime, size) in sorted(ch.changed.items())
            ]
            self.store.replace_files(k, project, files, ch.removed)
            moved = True
        return moved

    def fill_vectors(self, project: str) -> None:
        """The vector step for alias lines with no vector yet (one batch of
        distinct texts, the embedding cache answering repeats)."""
        missing = self.store.missing_body_vectors(
            {"project": project, "kind": "summary"}
        )
        if not missing:
            return
        texts = list(dict.fromkeys(missing.values()))
        vec = dict(zip(texts, embed_batch(self.embedder, texts)))
        self.store.set_body_vectors({i: list(vec[t]) for i, t in missing.items()})

    def keyword_map(
        self, project: str, labels: tuple[str, ...]
    ) -> dict[str, list[str]]:
        """{section_hash: terms} for `labels`, each section's terms in label
        order (SectionIndex.terms_for's union)."""
        self.refresh(project, "keywords")
        rows = self.store.facet_values(
            {"project": project, "kind": "keywords"}, ["label", "section_hash", "terms"]
        )
        by_label: dict[str, dict[str, list[str]]] = {}
        for r in rows:
            by_label.setdefault(r["label"], {})[r["section_hash"]] = list(
                r["terms"] or []
            )
        out: dict[str, list[str]] = {}
        for label in labels:
            for section, terms in by_label.get(label, {}).items():
                out.setdefault(section, []).extend(terms)
        return out

    def alias_vectors(
        self, project: str, labels: tuple[str, ...]
    ) -> dict[tuple[str, str], list[list[float]]]:
        """{(section_hash, label): alias vectors in line order} for `labels`."""
        self.refresh(project, "summary")
        self.fill_vectors(project)
        items = self.store.items(
            {"project": project, "kind": "summary"},
            facet={"label": {"$in": list(labels)}},
        )
        rows: dict[tuple[str, str], list[tuple[int, list[float]]]] = {}
        for it in items.values():
            if it.body_vec is None:
                continue
            f = it.facet
            rows.setdefault((f["section_hash"], f["label"]), []).append(
                (int(f["n"]), list(it.body_vec))
            )
        return {k: [v for _n, v in sorted(rs)] for k, rs in rows.items()}
