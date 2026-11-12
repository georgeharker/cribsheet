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
    # A code symbol's record (crib.codeindex), minus its edges (calls,
    # called_by, references and membership live in crib_edges).
    "symbol": {
        "symbol_ref": "VARCHAR",
        "fqn": "VARCHAR",
        "name": "VARCHAR",
        "sym_kind": "VARCHAR",
        "lang": "VARCHAR",
        "file": "VARCHAR",
        "line": "BIGINT",
        "signature": "VARCHAR",
        "description": "VARCHAR",
        "file_hash": "VARCHAR",
        "symbol_content_hash": "VARCHAR",
        "container": "VARCHAR[]",
        "scope": "VARCHAR[]",
        "symbol_was": "VARCHAR[]",
        "keywords": "VARCHAR[]",
        "name_terms": "VARCHAR[]",
        "schema": "BIGINT",
        "mtime": "BIGINT",
    },
}
_DEFAULTS: Mapping[str, Mapping[str, Any]] = {
    "design": {"status": "active"},
    "plan": {"status": "todo"},
}


def facet_table(kind: str) -> str:
    return f"crib_{kind}_meta"


@dataclass(frozen=True)
class Edge:
    """A link from an item. `kind` is a dependency kind (DEP_KINDS), `dep` (a
    dependency written without one: its kind is the target's default, resolved
    when read), `drawn_from` (a doc section, `ref#heading`), `superseded_by`,
    or `learns` (`symbol:<symbol_ref>`). `recorded_hash` is what the source
    recorded of the target (the dep's body hash, the section's hash), for the
    edges that are checked."""

    dst: str
    kind: str
    recorded_hash: str | None = None


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
    edges: tuple[Edge, ...] = ()
    # The item's lexical postings, (term, frequency), sorted: what BM25 scores it
    # on. Only search candidates have any (see crib.retrieve.code_candidate).
    terms: tuple[tuple[str, float], ...] = ()


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


# What a dependency MEANS, each with its own gating (taint flows down every one):
#   follows      ordering: blocked until the target is done
#   rests_on     grounding: blocked while the target is stale (or proposed)
#   informed_by  context: checked for change, never blocks
DEP_KINDS = ("follows", "rests_on", "informed_by")


def dep_kind_default(target_kind: str | None) -> str:
    """The kind a dependency gets when none is written: a plan target is
    ordering, a decision is grounding, anything else (a note) is context."""
    return {"plan": "follows", "design": "rests_on"}.get(target_kind or "", "informed_by")


def frontmatter_deps(fm: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """(every dep id in order, {id: kind} for the ones written with a kind).

    `deps:` holds dependencies of the DEFAULT kind for their target; a
    dependency of any other kind is written under its kind's own key
    (`follows:` / `rests_on:` / `informed_by:`), so the common case keeps the
    one-list format and a non-default choice is visible in the file."""
    ids: list[str] = []
    explicit: dict[str, str] = {}
    for d in fm.get("deps") or []:
        if str(d) not in ids:
            ids.append(str(d))
    for kind in DEP_KINDS:
        for d in fm.get(kind) or []:
            d = str(d)
            explicit[d] = kind
            if d not in ids:
                ids.append(d)
    return ids, explicit


def parse_sources(raw: Any) -> list[dict[str, Any]]:
    """`sources:` frontmatter → `[{ref, heading, hash}]`, tolerantly.

    A hand-written entry may be the bare citation string (`docs/DESIGN.md#4.
    Coordination`) rather than the mapping the verbs write — accepted, because a
    source is exactly the kind of thing a human adds to a note by hand. READING is
    deliberately more permissive than writing: `_resolve_source` refuses a
    citation that names no section, but a file already on disk is parsed as it
    stands rather than dropped."""
    out: list[dict[str, Any]] = []
    for item in raw or []:
        if isinstance(item, str):
            ref, _, heading = item.partition("#")
            item = {"ref": ref, "heading": heading}
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "").strip()
        if not ref:
            continue
        heading = str(item.get("heading") or "").strip()
        out.append(
            {
                "ref": ref,
                "heading": heading or None,
                "hash": str(item.get("hash") or ""),
            }
        )
    return out


def edges_of(kind: str, fm: Mapping[str, Any]) -> tuple[Edge, ...]:
    """The links an item's frontmatter declares (see `Edge`)."""
    out: list[Edge] = []
    checked = {str(k): str(v) for k, v in (fm.get("checked") or {}).items()}
    ids, explicit = frontmatter_deps(dict(fm))
    for dst in ids:
        out.append(Edge(dst, explicit.get(dst, "dep"), checked.get(dst)))
    for src in parse_sources(fm.get("sources")):
        dst = f"{src['ref']}#{src['heading']}" if src["heading"] else src["ref"]
        out.append(Edge(dst, "drawn_from", src["hash"] or None))
    if by := fm.get("superseded_by"):
        out.append(Edge(str(by), "superseded_by"))
    if kind == "learning" and (ref := fm.get("symbol_ref")):
        out.append(Edge(f"symbol:{ref}", "learns"))
    return tuple(out)


# A symbol facet column that holds a record field of another name.
_SYMBOL_FIELD = {"sym_kind": "kind", "symbol_content_hash": "content_hash"}
# The record's edge lists, as edges of the same kind, in the record's order.
_SYMBOL_EDGE_LISTS = ("calls", "called_by", "references")


def symbol_item(project: str, entry: Mapping[str, Any]) -> Item:
    """A code symbol's record (one symbol_index TOML) as an item: id
    `symbol:<symbol_ref>`, its scalar and list fields in the symbol facet, and
    its calls / called_by / references (deferred `name [file]` refs, resolved
    when read, in the record's order) and its container (`member_of`) as edges.
    No vectors yet."""
    import json

    from .symbols import key as symbol_key
    from .symbols import symbol_ref

    ref = symbol_key(dict(entry))
    file = str(entry.get("file") or "")
    lang = str(entry.get("lang") or "")
    container = [str(c) for c in entry.get("container") or []]
    facet: dict[str, Any] = {}
    for name, sqltype in FACETS["symbol"].items():
        src = _SYMBOL_FIELD.get(name, name)
        value = ref if name == "symbol_ref" else entry.get(src)
        if sqltype == "VARCHAR[]":
            # absent stays absent (None), unlike the authored facets: a code hit
            # returns the record's own field, and `[]` is not "missing"
            facet[name] = [str(v) for v in value] if isinstance(value, list) else None
        elif sqltype == "BIGINT":
            facet[name] = int(value) if isinstance(value, int) else None
        else:
            facet[name] = None if value is None else str(value)
    edges = [Edge(str(c), "calls") for c in entry.get("calls") or []]
    edges += [Edge(str(r), "references") for r in entry.get("references") or []]
    # As recorded, not derived: the language server's incoming calls are not
    # always `calls` read backwards (329 of cribsheet's 2,217 records differ).
    edges += [Edge(str(c), "called_by") for c in entry.get("called_by") or []]
    if container:
        parent = symbol_ref(file, container[:-1], container[-1], lang)
        edges.append(Edge(f"symbol:{parent}", "member_of"))
    from collections import Counter

    from .retrieve import code_candidate, code_field_terms

    # A candidate whose field is empty still counts in the BM25 corpus (it moves
    # every idf), so it keeps one placeholder posting: the empty term, which no
    # query contains, at frequency 0.
    terms = (
        tuple(sorted((t, float(n)) for t, n in Counter(code_field_terms(dict(entry))).items()))
        or (("", 0.0),)
        if code_candidate(dict(entry))
        else ()
    )
    # Hash what the store HOLDS (facet and edges), not the dict handed in: the
    # mirror sees the entry as written, the full sync the record parsed back from
    # its TOML, and the two must agree on whether anything changed.
    stable = {"facet": facet, "edges": [(e.dst, e.kind) for e in edges]}
    return Item(
        id=f"symbol:{ref}",
        kind="symbol",
        project=project,
        relpath=file,
        title=str(entry.get("fqn") or entry.get("name") or ref),
        content_hash=sha1_hex(json.dumps(stable, sort_keys=True, default=str)),
        facet=facet,
        edges=tuple(edges),
        terms=terms,
    )


def symbol_record(item: Item) -> dict[str, Any]:
    """A `symbol` item back as its symbol_index record: the facet fields under
    their record names and the edge lists in the record's order. An absent
    optional field (keywords) stays absent."""
    rec: dict[str, Any] = {}
    for name in FACETS["symbol"]:
        value = item.facet.get(name)
        if value is None and name == "keywords":
            continue
        rec[_SYMBOL_FIELD.get(name, name)] = value
    for kind in _SYMBOL_EDGE_LISTS:
        rec[kind] = [e.dst for e in item.edges if e.kind == kind]
    return rec
