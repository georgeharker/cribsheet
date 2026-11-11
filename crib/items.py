"""Items: one row per thing crib stores, whatever its kind.

A note, a design decision, a plan item, a learning and an in-repo doc are each
one item: the core fields every kind shares (`Item`), plus that kind's own
fields (`Item.facet`), which the DuckDB store keeps in one table per kind
(`FACETS`) and joins on the item id. The files stay the truth; items are a
rebuildable cache the indexer refreshes in the same transaction as the item's
chunks. (Design: "Unified item and edge tables in the DuckDB cache".)

Ids are the stable identity: the frontmatter ULID for authored items, the
derived id for in-repo docs. The kind-qualified docref (`design:foo.md`) is the
address. Links between items (deps, sources, supersession) are not facet fields:
they belong to the edges table.

Each item carries two vectors: its TITLE and the opening of its BODY, embedded
separately, so a near-duplicate check can tell "the titles collide" from "the
bodies say the same thing".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .util import sha1_hex

# How much of the body the body vector sees. The embedder truncates at its own
# model limit anyway; this keeps the text it is handed bounded.
BODY_CHARS = 2000

# Each kind's own fields: name -> DuckDB column type. A kind with no entry (doc)
# has only the core row. Defaults match what the facets assume when a field is
# missing from frontmatter.
FACETS: Mapping[str, Mapping[str, str]] = {
    "note": {"source": "VARCHAR", "tags": "VARCHAR[]", "source_path": "VARCHAR"},
    "design": {"status": "VARCHAR", "created": "VARCHAR", "updated": "VARCHAR"},
    "plan": {
        "status": "VARCHAR",
        "rank": "VARCHAR",
        "trigger": "VARCHAR",
        "reviewed": "VARCHAR",
        "created": "VARCHAR",
        "updated": "VARCHAR",
    },
    "learning": {
        "symbol_ref": "VARCHAR",
        "lang": "VARCHAR",
        "file": "VARCHAR",
        "signature": "VARCHAR",
        "symbol_was": "VARCHAR[]",
    },
}
_DEFAULTS: Mapping[str, Mapping[str, Any]] = {
    "design": {"status": "active"},
    "plan": {"status": "todo"},
}


def facet_table(kind: str) -> str:
    return f"crib_{kind}_meta"


@dataclass(frozen=True)
class Item:
    id: str
    kind: str
    project: str
    relpath: str
    title: str
    content_hash: str  # of the title and body: unchanged hash, no re-embed
    title_vec: list[float] | None = None
    body_vec: list[float] | None = None
    facet: dict[str, Any] = field(default_factory=dict)


def body_text(body: str) -> str:
    return body.strip()[:BODY_CHARS]


def item_hash(title: str, body: str) -> str:
    return sha1_hex(title, body_text(body))


def facet_of(kind: str, frontmatter: Mapping[str, Any]) -> dict[str, Any]:
    """`kind`'s own fields from a note's frontmatter, typed for its table: list
    columns get lists of strings, everything else a string or None."""
    out: dict[str, Any] = {}
    defaults = _DEFAULTS.get(kind, {})
    for name, sqltype in FACETS.get(kind, {}).items():
        value = frontmatter.get(name, defaults.get(name))
        if sqltype == "VARCHAR[]":
            out[name] = [str(v) for v in value] if isinstance(value, list) else []
        else:
            out[name] = None if value is None else str(value)
    return out
