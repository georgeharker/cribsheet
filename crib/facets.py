"""The facet registry: every kind of item crib stores in a pillar is a spec.

A facet is DATA, not code (design: "Facet registry: every facet, built-in or
declared, is a declarative spec composed from engine aspects"). A spec names
the facet (its noun, its plural, its pillar store) and lists the ASPECTS it
has: behaviours the engine implements once, each bringing its own data and
verbs. A spec selects and parameterises them; it never adds behaviour.

crib's own pillars (notes, decisions, plans, learnings) are specs like any
other, shipped as `facets.toml` beside this module and loaded once per process.
Everything that used to list the pillars by hand (the chunk kind map, the
per-kind store tables, the note store's reserved prefixes, the watcher's
segments) derives from here.
"""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any

# The engine's aspects, and the ones each needs alongside it. `core` (body,
# title, tags, id, versions, and the verbs over them) every facet has.
ASPECTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "core": frozenset(),
        "links": frozenset(),  # deps, their kinds, and staleness through them
        "status": frozenset(),  # a status vocabulary with named transitions
        "progress": frozenset({"status"}),  # done statuses; next, review
        "ordered": frozenset(),  # a rank; move
        "supersede": frozenset({"status", "links"}),
        "sources": frozenset(),  # doc-section citations; import, export
        # LLM-made summaries and keywords (derived indexes); summarize,
        # elaborate, and distill (which rewrites the item: a write verb)
        "enriched": frozenset(),
        "anchored": frozenset(),  # the code symbol an item is about
    }
)

# The link kinds (what a dependency MEANS; the engine gives each its gating):
# follows (ordering: waits until done), rests_on (grounding: blocked while
# its target is gated or stale), informed_by (context: never blocks).
LINK_KINDS = ("follows", "rests_on", "informed_by")

# The built-in graph facets' shared default link kinds: what a link means
# when its SOURCE isn't known (and the rule every built-in source follows).
_DEFAULT_EDGES = MappingProxyType(
    {"plan": "follows", "design": "rests_on", "*": "informed_by"})

# The roles a graph facet's statuses play (the engine implements each):
#   open     live; may be the initial status
#   claimed  someone is on it: listed first in next
#   gated    blessed by nobody yet: its changes taint nothing, and what
#            rests_on it is blocked
#   paused   carries a revisit trigger; visible, never "pick me"
#   dropped  carries a revisit trigger; satisfies nothing, hidden by default
#   done     satisfies follows; a moved cited source gives `revisit`
#   retired  superseded: dependents go stale (dep-superseded)
ROLES = ("open", "claimed", "gated", "paused", "dropped", "done", "retired")

# The store column types a facet field may have.
FIELD_TYPES = frozenset({"VARCHAR", "VARCHAR[]", "BIGINT", "DOUBLE", "BOOLEAN", "JSON"})

# A declared facet's own field types, and the store column each is kept in.
# A date is ISO `YYYY-MM-DD` text: it sorts and compares correctly as text.
PARAM_TYPES: Mapping[str, str] = MappingProxyType(
    {"int": "BIGINT", "str": "VARCHAR", "date": "VARCHAR"}
)

# Names a declared field may not take: the generated verbs' own parameters, and
# the frontmatter keys crib writes itself.
RESERVED_FIELDS = frozenset(
    {
        "relpath", "content", "title", "project", "project_path", "tags", "heading",
        "new_content", "query", "k", "hints", "hint_weight", "keyword_labels",
        "keyword_weight", "summary_labels", "summary_weight", "rerank", "version",
        "paths", "to_project", "to_relpath", "label", "overwrite", "symbol", "text",
        "path", "scope", "lang", "apply", "orphans_only", "old_fqn", "new_fqn",
        "where", "ctx", "id", "type", "kind", "source", "source_path", "imported",
        "created", "updated", "status", "deps", "content_hash", "body_hash",
        # the graph verbs' parameters
        "after", "all", "batch", "before", "by_ref", "dep_ref", "depth", "direction",
        "follows", "force", "informed_by", "items", "proposed", "ref", "rests_on",
        "sources", "tainted", "triggers", "why",
        # frontmatter crib reads or writes beyond those (graph state, anchoring)
        "checked", "dep_kinds", "superseded_by", "trigger", "reviewed", "rank",
        "reaffirmed", "symbol_ref", "symbol_was", "file", "signature",
    }
)

# Directory (and file) names crib keeps inside a project besides its facets'
# stores: no facet's `store` may take one (tests/test_facets.py holds this
# against crib.watch.RECORD_TIERS).
RESERVED_STORES = frozenset({
    "symbol_index", "keyword_index", "summary_index", "migrations", "sources",
    "doc-sources.json",
})

# THE verb catalogue: which verbs each family of facet has, the aspect that
# brings each, and any condition beyond that aspect ("fields": the facet
# declares fields; "taint": it reports taint), in tool-registration order. A
# note-like facet's family is core + enriched (list included: every facet can
# be listed without a query, by its fields when it has some); an anchored
# one's items are named by symbol, so its family REPLACES the relpath-addressed
# ones; a graph facet (links + status) has its own, then one verb per status
# transition. Everything that asks "which verbs does this facet have" reads
# this (`verb_names`); crib.verbs holds one template per entry (a test keeps
# the two in step).


@dataclass(frozen=True)
class VerbDecl:
    verb: str
    aspect: str
    needs: str = ""  # "" | "fields" | "taint"


def _decls(aspect: str, *verbs: str, needs: str = "") -> tuple[VerbDecl, ...]:
    return tuple(VerbDecl(v, aspect, needs) for v in verbs)


VERB_CATALOGUE: Mapping[str, tuple[VerbDecl, ...]] = MappingProxyType({
    "note": (
        *_decls("core", "lookup", "apropos", "read", "locate", "add", "append", "edit",
                "forget", "reindex", "versions", "restore"),
        *_decls("enriched", "distill", "elaborate", "summarize"),
        *_decls("core", "import", "move"),
        *_decls("core", "list"),
    ),
    "anchored": (
        *_decls("anchored", "add", "edit", "forget", "read", "reaffirm", "migrate",
                "report", "rehome"),
        *_decls("anchored", "list"),
    ),
    "graph": (
        *_decls("core", "add", "read", "edit", "append", "lookup", "list"),
        *_decls("sources", "export"),
        *_decls("links", "dep_add", "dep_remove"),
        *_decls("core", "forget"),
        *_decls("links", "check", needs="taint"),
        *_decls("links", "reaffirm"),
        *_decls("sources", "extract"),
        *_decls("links", "tree", needs="taint"),
        *_decls("links", "graph"),
        *_decls("supersede", "supersede"),
        *_decls("progress", "status", "review", "review_apply"),
        *_decls("ordered", "move"),
        *_decls("progress", "next"),
    ),
})

# A transition can't take a graph verb's name.
GRAPH_VERB_NAMES = frozenset(d.verb for d in VERB_CATALOGUE["graph"])


def family(aspects: Iterable[str]) -> str:
    """Which verb family a facet with these aspects has: graph, anchored or note."""
    have = set(aspects)
    if {"links", "status"} <= have:
        return "graph"
    return "anchored" if "anchored" in have else "note"


def available_verbs(aspects: Iterable[str], *, fields: bool = False, taint: bool = False,
                    transitions: Iterable[str] = ()) -> tuple[str, ...]:
    """The canonical verbs a facet of this shape gets, before any it disables."""
    have = set(aspects) | {"core"}
    fam = family(have)
    met = {"": True, "fields": fields, "taint": taint}
    out = [d.verb for d in VERB_CATALOGUE[fam] if d.aspect in have and met[d.needs]]
    return (*out, *transitions) if fam == "graph" else tuple(out)


def verb_names(spec: "FacetSpec") -> tuple[str, ...]:
    """The canonical verbs this facet has, in registration order (its
    transitions last), less the ones it disables."""
    return tuple(v for v in available_verbs(
        spec.aspects, fields=bool(spec.params), taint=spec.report_taint,
        transitions=spec.transitions) if v not in spec.disabled)


def graph_fields(aspects: Iterable[str]) -> dict[str, str]:
    """The fields a graph facet (links + status) brings, in the store column
    order the built-ins have always had: its status and rank, a deferral's
    trigger records and the review stamp (progress), its dates, and the body
    hash its dependents' `checked` records compare against."""
    have = set(aspects)
    out = {"status": "VARCHAR", "rank": "VARCHAR"}
    if "progress" in have:
        out |= {"trigger": "JSON", "reviewed": "VARCHAR"}
    return out | {"created": "VARCHAR", "updated": "VARCHAR", "body_hash": "VARCHAR"}


# The fields an aspect brings: every facet with the aspect has them, first and
# in this order, before its own. Anchoring writes the symbol an item is about.
ASPECT_FIELDS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "anchored": MappingProxyType(
            {
                "symbol_ref": "VARCHAR",
                "lang": "VARCHAR",
                "file": "VARCHAR",
                "signature": "VARCHAR",
                "symbol_was": "VARCHAR[]",
            }
        ),
    }
)

# Nouns that are crib's but not pillars: CLI/MCP families, top-level commands
# (a facet's noun is its CLI command and its tools' prefix) and non-pillar item
# kinds. No facet may take one, as noun or plural. tests/test_facets.py checks
# every top-level command and built-in tool prefix is covered.
RESERVED_NAMES = frozenset(
    {
        "bundle",
        "bundles",
        "facet",
        "facets",
        "info",
        "ops",
        "serve",
        "status",
        "code",
        "project",
        "projects",
        "generate",
        "memory",
        "store",
        "system",
        "symbol",
        "symbols",
        "doc",
        "docs",
        "keywords",
        "summary",
        "sources",
    }
)

# The facet every unqualified relpath belongs to: unknown kinds fall back to
# it, in-situ repo docs live in its store, and it refuses every other facet's
# store directory as a relpath prefix.
HOME = "note"

@dataclass(frozen=True)
class Flag:
    """A behaviour switch a facet spec may set: a key of the `[<noun>]` table
    whose value changes how the engine treats the facet's items. Every flag is
    declared here and nowhere else: its type, the aspects it needs (a flag on
    a facet without them would do nothing, so it is refused), and what it
    means. Adding a flag is one entry here plus the engine code that reads
    `spec.flag(name)`; parse, validation, `to_toml` and `crib facets show`
    follow from the entry."""

    name: str
    needs: frozenset[str]
    meaning: str
    type: str = "bool"  # "bool" | "str"


FLAGS: Mapping[str, Flag] = MappingProxyType({f.name: f for f in (
    Flag("requires_body", frozenset({"links", "status"}),
         "an item must have a body: add refuses a title alone (decisions)"),
    Flag("report_taint", frozenset({"links"}),
         "staleness is reported: a marker on hits and exports, the check and "
         "tree verbs, a next step (decisions)"),
    Flag("export_with_deps", frozenset({"links", "sources"}),
         "its export includes the items its own depend on, from other facets "
         "(plans)"),
    Flag("label", frozenset({"sources"}), "the heading of its exported document",
         type="str"),
)})


# The working-set bands a progress facet's list groups its items into, in
# their rendering order, and the names crib has always shown for them (the
# plan's). A facet may name its own (`[<noun>.bands]`); the rest keep these.
BANDS = ("claimed", "ready", "blocked", "paused", "dropped", "done")
BAND_NAMES: Mapping[str, str] = MappingProxyType({
    "claimed": "in-progress", "ready": "ready", "blocked": "blocked",
    "paused": "parked", "dropped": "declined", "done": "done",
})

_KEYS = frozenset(
    {"plural", "store", "aspects", "fields", "legacy_stores", "verbs", "disable",
     "status", "edges", "about", "when", "title_from", "optional", "bands", *FLAGS}
)


class FacetSpecError(ValueError):
    """A facet spec that cannot be loaded, with the reason."""


@dataclass(frozen=True)
class FacetSpec:
    """One facet: its names, its aspects and its own typed fields."""

    noun: str
    plural: str
    store: str
    aspects: frozenset[str]
    fields: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    legacy_stores: tuple[str, ...] = ()
    # A built-in's own names for verbs whose canonical name differs (notes'
    # `note_store` for `add`). Declared facets always use the canonical names.
    verb_names: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    # A declared facet's own fields by their declared type (int | str | date):
    # each is a typed parameter on add/edit and a filter on lookup/list.
    params: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    # Canonical verbs a declared facet turned off: never registered, no CLI
    # subcommand, in no tool set.
    disabled: frozenset[str] = frozenset()
    # The status aspect: each status name's role (in order), the initial one,
    # and the transition verbs (verb -> status).
    statuses: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    initial: str | None = None
    transitions: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    # The links aspect: the default link kind toward each target facet ("*").
    edges: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    # The flags it sets (FLAGS: name -> value); unset ones read as their
    # default (False, or None for a string).
    flags: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def flag(self, name: str) -> Any:
        """A flag's value on this facet (FLAGS[name]); unset: False / None."""
        if name not in FLAGS:
            raise KeyError(f"no such facet flag {name!r} (flags: {sorted(FLAGS)})")
        return self.flags.get(name, False if FLAGS[name].type == "bool" else None)

    @property
    def requires_body(self) -> bool:
        return bool(self.flag("requires_body"))

    @property
    def report_taint(self) -> bool:
        return bool(self.flag("report_taint"))

    @property
    def export_with_deps(self) -> bool:
        return bool(self.flag("export_with_deps"))

    @property
    def label(self) -> str | None:
        return self.flag("label")

    def role(self, status: str | None) -> str | None:
        """The role `status` plays, or None (not one of this facet's)."""
        return self.statuses.get(status or "")

    def with_role(self, *roles: str) -> tuple[str, ...]:
        """This facet's statuses playing any of `roles`, in declared order."""
        return tuple(s for s, r in self.statuses.items() if r in roles)

    # Wording an agent reads in every one of this facet's tool descriptions,
    # after the template's: what one of these items is, and when to reach for
    # this family (rather than notes). `when` adds wording to one verb's tool.
    about: str = ""
    when: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    # Where an item's title comes from, tried in order (item_title): its
    # frontmatter `title`, its first `heading`, its `filename`, or a named
    # frontmatter key (toucan's `name`). None found: no title (doctor says so).
    title_from: tuple[str, ...] = ("title",)
    # A declared field is REQUIRED unless named here: `add` refuses an item
    # without it, `crib project doctor` names files missing it.
    optional: frozenset[str] = frozenset()
    # Its own names for working-set bands (BANDS -> name); unnamed: BAND_NAMES.
    bands: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    builtin: bool = False

    def band_name(self, band: str) -> str:
        """What this facet calls working-set `band` (BANDS)."""
        return self.bands.get(band, BAND_NAMES[band])

    @property
    def required(self) -> tuple[str, ...]:
        """Its declared fields an item must carry (all but the optional ones)."""
        return tuple(n for n in self.params if n not in self.optional)

    def has(self, aspect: str) -> bool:
        return aspect in self.aspects


def _ident(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.isidentifier() or value != value.lower():
        raise FacetSpecError(f"{what} must be a lowercase identifier, got {value!r}")
    return value


def parse(noun: str, raw: Mapping[str, Any], *, builtin: bool = False) -> FacetSpec:
    """One `[<noun>]` table of a facets file as a spec, checked on its own.

    One helper per part of the table, in this order (the order is what a
    broken spec reports first): its names, its aspects, its fields, its verbs
    (renames, disables), its graph keys and flags, then the verbs it ends up
    with, its wording, its title sources and its optional fields."""
    plural, store = _parse_names(noun, raw)
    have = _parse_aspects(noun, raw)
    fields, params = _parse_fields(noun, raw, have, builtin)
    legacy, verbs, disable = _parse_verb_keys(noun, raw, builtin)
    graph = _graph_keys(noun, raw, have, builtin)
    available = available_verbs(have, fields=bool(params),
                                taint=bool(graph["flags"].get("report_taint")),
                                transitions=graph.get("transitions", {}))
    _check_disable(noun, disable, available)
    about, when = _wording(noun, raw, [v for v in available if v not in disable])
    return FacetSpec(
        noun=noun,
        plural=plural,
        store=store,
        aspects=frozenset(have),
        fields=MappingProxyType(dict(fields)),
        legacy_stores=tuple(legacy),
        verb_names=MappingProxyType(dict(verbs)),
        params=MappingProxyType(params),
        disabled=frozenset(str(v) for v in disable),
        about=about,
        when=MappingProxyType(when),
        title_from=_parse_title_from(noun, raw),
        optional=_parse_optional(noun, raw, params),
        bands=MappingProxyType(_parse_bands(noun, raw, have)),
        **graph,
        builtin=builtin,
    )


def _parse_names(noun: str, raw: Mapping[str, Any]) -> tuple[str, str]:
    """The facet's name, its keys, its plural and its store directory."""
    _ident(noun, "a facet name")
    if "_" in noun:
        # tools are `<noun>_<verb>`, and verbs have underscores (dep_add): a
        # noun with one would make a tool's facet ambiguous
        raise FacetSpecError(f"a facet name can't contain '_', got {noun!r}")
    if stray_keys := set(raw) - _KEYS:
        raise FacetSpecError(f"facet {noun!r}: unknown keys {sorted(stray_keys)}")
    plural = _ident(raw.get("plural"), f"facet {noun!r}: plural")
    store = raw.get("store", plural)
    if not isinstance(store, str) or not store or "/" in store or store.startswith("."):
        raise FacetSpecError(
            f"facet {noun!r}: store must be a directory name, got {store!r}"
        )
    if store in RESERVED_STORES:
        raise FacetSpecError(
            f"facet {noun!r}: store {store!r} is a directory crib keeps for itself "
            "in every project; name the store something else")
    return plural, store


def _parse_aspects(noun: str, raw: Mapping[str, Any]) -> set[str]:
    """Its aspects (`core` implied), each with what it needs, as one shape."""
    aspects = raw.get("aspects", ["core"])
    if not isinstance(aspects, list) or not all(isinstance(a, str) for a in aspects):
        raise FacetSpecError(f"facet {noun!r}: aspects must be a list of names")
    if unknown := set(aspects) - set(ASPECTS):
        raise FacetSpecError(
            f"facet {noun!r}: unknown aspects {sorted(unknown)} (known: {sorted(ASPECTS)})"
        )
    have: set[str] = {str(a) for a in aspects} | {"core"}
    for a in sorted(have):
        if missing := ASPECTS[a] - have:
            raise FacetSpecError(
                f"facet {noun!r}: aspect {a!r} needs {sorted(missing)}"
            )
    _check_shape(noun, have, raw)
    return have


def _parse_fields(noun: str, raw: Mapping[str, Any], have: set[str],
                  builtin: bool) -> tuple[dict[str, str], dict[str, str]]:
    """`(fields, params)`: every store column (those its aspects bring, then
    its own) and a declared facet's own typed fields (int | str | date)."""
    fields = raw.get("fields", {})
    if not isinstance(fields, dict):
        raise FacetSpecError(f"facet {noun!r}: fields must be a table of name = type")
    params: dict[str, str] = {}
    for name, sqltype in fields.items():
        _ident(name, f"facet {noun!r}: field name")
        if builtin:  # crib's own: store column types
            if sqltype not in FIELD_TYPES:
                raise FacetSpecError(
                    f"facet {noun!r}: field {name!r} has type {sqltype!r} "
                    f"(allowed: {sorted(FIELD_TYPES)})"
                )
            continue
        if sqltype not in PARAM_TYPES:
            raise FacetSpecError(
                f"facet {noun!r}: field {name!r} has type {sqltype!r} "
                f"(allowed: {sorted(PARAM_TYPES)})"
            )
        if name in RESERVED_FIELDS:
            raise FacetSpecError(
                f"facet {noun!r}: field name {name!r} is reserved (a verb parameter "
                "or a frontmatter key crib writes)"
            )
        params[name] = sqltype
    if params:
        fields = {name: PARAM_TYPES[t] for name, t in params.items()}
    brought: dict[str, str] = {}
    for a in sorted(have):
        brought.update(ASPECT_FIELDS.get(a, {}))
    if {"links", "status"} <= have:
        brought.update(graph_fields(have))
    if clash := set(fields) & set(brought):
        raise FacetSpecError(
            f"facet {noun!r}: fields {sorted(clash)} come with its aspects; "
            "don't declare them")
    return {**brought, **fields}, params


def _parse_verb_keys(noun: str, raw: Mapping[str, Any],
                     builtin: bool) -> tuple[list[str], dict[str, str], list[str]]:
    """`(legacy_stores, verbs, disable)`: the keys about its verbs and stores
    that only crib's own facets (renames) or only declared ones (disables) use."""
    legacy = raw.get("legacy_stores", [])
    if not isinstance(legacy, list) or not all(
        isinstance(s, str) and s for s in legacy
    ):
        raise FacetSpecError(f"facet {noun!r}: legacy_stores must be a list of names")
    verbs = raw.get("verbs", {})
    if verbs and not builtin:
        raise FacetSpecError(
            f"facet {noun!r}: only built-in facets may rename verbs (`verbs`); "
            "a declared facet's verbs have the canonical names")
    if not isinstance(verbs, dict):
        raise FacetSpecError(f"facet {noun!r}: verbs must be a table of verb = name")
    for verb, name in verbs.items():
        _ident(verb, f"facet {noun!r}: verb")
        _ident(name, f"facet {noun!r}: name for verb {verb!r}")
    disable = raw.get("disable", [])
    if disable and builtin:
        raise FacetSpecError(f"facet {noun!r}: a built-in facet's verbs can't be disabled")
    if not isinstance(disable, list) or not all(isinstance(v, str) for v in disable):
        raise FacetSpecError(f"facet {noun!r}: disable must be a list of verb names")
    return legacy, verbs, disable


def _check_disable(noun: str, disable: list[str], available: tuple[str, ...]) -> None:
    """Each disabled verb is one the facet has, and not all of them."""
    if not_its_own := sorted(set(disable) - set(available)):
        raise FacetSpecError(
            f"facet {noun!r}: can't disable {not_its_own}: its verbs are {list(available)}")
    if not set(available) - set(disable):
        raise FacetSpecError(f"facet {noun!r}: disables every one of its verbs")


def _parse_title_from(noun: str, raw: Mapping[str, Any]) -> tuple[str, ...]:
    """Where an item's title comes from (`title_from`, default the title)."""
    title_from = raw.get("title_from", ["title"])
    if isinstance(title_from, str):
        title_from = [title_from]
    if not isinstance(title_from, list) or not title_from or not all(
            isinstance(t, str) and t.strip() for t in title_from):
        raise FacetSpecError(
            f"facet {noun!r}: title_from is a list of sources: \"title\", \"heading\", "
            "\"filename\" or a frontmatter key")
    return tuple(t.strip() for t in title_from)


def _parse_bands(noun: str, raw: Mapping[str, Any], have: set[str]) -> dict[str, str]:
    """Its own names for working-set bands (`[<noun>.bands]`): a progress
    facet's, each a known band, every effective name distinct."""
    bands = raw.get("bands")
    if bands is None:
        return {}
    if "progress" not in have:
        raise FacetSpecError(f"facet {noun!r}: bands needs the progress aspect")
    if not isinstance(bands, dict) or not all(
            isinstance(v, str) and v.strip() for v in bands.values()):
        raise FacetSpecError(f"facet {noun!r}: bands is a table of band = name")
    if stray := sorted(set(bands) - set(BANDS)):
        raise FacetSpecError(
            f"facet {noun!r}: no such band {stray} (bands: {list(BANDS)})")
    names = [str(bands.get(b, BAND_NAMES[b])).strip() for b in BANDS]
    if len(set(names)) != len(names):
        raise FacetSpecError(f"facet {noun!r}: two bands can't share a name ({names})")
    return {b: str(v).strip() for b, v in bands.items()}


def _parse_optional(noun: str, raw: Mapping[str, Any], params: Mapping[str, str]) -> frozenset[str]:
    """The declared fields an item may leave out (the rest are required)."""
    optional = raw.get("optional", [])
    if not isinstance(optional, list) or not all(isinstance(o, str) for o in optional):
        raise FacetSpecError(f"facet {noun!r}: optional must be a list of field names")
    if stray := sorted(set(optional) - set(params)):
        raise FacetSpecError(
            f"facet {noun!r}: optional names {stray}, not among its fields "
            f"{sorted(params)}")
    return frozenset(optional)


def item_title(spec: "FacetSpec | None", fm: Mapping[str, Any], body: str,
               relpath: str) -> str:
    """An item's title, by its facet's `title_from` (default: the frontmatter
    `title` alone): the first source that yields one."""
    import re

    for src in (spec.title_from if spec is not None else ("title",)):
        if src == "heading":
            m = re.search(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", body or "", re.M)
            value: Any = m.group(1) if m else None
        elif src == "filename":
            name = relpath.rsplit("/", 1)[-1]
            value = name[: -len(".md")] if name.endswith(".md") else name
        else:  # "title", or any other frontmatter key
            value = fm.get(src)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _wording(noun: str, raw: Mapping[str, Any],
             verbs: list[str]) -> tuple[str, dict[str, str]]:
    """A spec's `about` (str) and `[<noun>.when]` (verb = wording, one of its
    own verbs)."""
    about = raw.get("about", "")
    if not isinstance(about, str):
        raise FacetSpecError(f"facet {noun!r}: about must be a string")
    when = raw.get("when", {})
    if not isinstance(when, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in when.items()):
        raise FacetSpecError(f"facet {noun!r}: when must be a table of verb = wording")
    if bad := sorted(set(when) - set(verbs)):
        raise FacetSpecError(
            f"facet {noun!r}: [{noun}.when] names {bad}, not among its verbs {verbs}")
    return about.strip(), {k: v.strip() for k, v in when.items()}


def _check_shape(noun: str, have: set[str], raw: Mapping[str, Any]) -> None:
    """Refuse an aspect combination the engine would silently serve nothing
    for. A graph facet is links AND status; ordered and sources are graph
    behaviours; the graph templates have no anchored or enriched verbs, and
    don't (yet) set or filter declared fields."""
    graph = {"links", "status"} <= have
    if ("links" in have) != ("status" in have):
        half = "links" if "links" in have else "status"
        raise FacetSpecError(
            f"facet {noun!r}: aspect {half!r} makes a graph facet together with "
            f"{'status' if half == 'links' else 'links'!r}: add it (or drop {half!r})")
    if not graph and (only := sorted(have & {"ordered", "sources"})):
        raise FacetSpecError(
            f"facet {noun!r}: aspects {only} belong to a graph facet (links + status)")
    if graph and (bad := sorted(have & {"anchored", "enriched"})):
        raise FacetSpecError(
            f"facet {noun!r}: a graph facet (links + status) can't also be {bad}")
    if graph and raw.get("fields"):
        raise FacetSpecError(
            f"facet {noun!r}: a graph facet (links + status) can't declare fields "
            "yet (its verbs don't set or filter them); fields work on note-like "
            "and anchored facets")


def _flags(noun: str, raw: Mapping[str, Any], have: set[str]) -> dict[str, Any]:
    """The FLAGS a spec sets, each checked against its type and the aspects
    it needs. A false bool is the same as not setting it."""
    out: dict[str, Any] = {}
    for name, f in FLAGS.items():
        if (value := raw.get(name)) is None:
            continue
        if f.type == "bool":
            if not isinstance(value, bool):
                raise FacetSpecError(f"facet {noun!r}: {name} is true or false, not {value!r}")
            if not value:
                continue
        elif not isinstance(value, str):
            raise FacetSpecError(f"facet {noun!r}: {name} is a string, not {value!r}")
        if missing := sorted(f.needs - have):
            raise FacetSpecError(
                f"facet {noun!r}: {name} needs the {', '.join(missing)} aspect"
                f"{'s' if len(missing) > 1 else ''} ({f.meaning})")
        out[name] = value
    return out


def _graph_keys(noun: str, raw: Mapping[str, Any], have: set[str],
                builtin: bool = False) -> dict[str, Any]:
    """A spec's status / edges / label keys, checked against its aspects."""
    out: dict[str, Any] = {}
    st = raw.get("status")
    if st is not None and "status" not in have:
        raise FacetSpecError(f"facet {noun!r}: a status table needs the status aspect")
    if "status" in have:
        if not isinstance(st, dict) or not isinstance(st.get("values"), dict) or not st["values"]:
            raise FacetSpecError(
                f"facet {noun!r}: the status aspect needs [{noun}.status] values = "
                "{status = role, ...}")
        if bad := set(st) - {"values", "initial", "transitions"}:
            raise FacetSpecError(f"facet {noun!r}: unknown status keys {sorted(bad)}")
        values = {str(k): str(v) for k, v in st["values"].items()}
        if unknown := sorted(set(values.values()) - set(ROLES)):
            raise FacetSpecError(
                f"facet {noun!r}: unknown status roles {unknown} (roles: {list(ROLES)})")
        initial = st.get("initial") or next(iter(values))
        if initial not in values:
            raise FacetSpecError(f"facet {noun!r}: initial status {initial!r} isn't one of its statuses")
        transitions = st.get("transitions") or {}
        if not isinstance(transitions, dict):
            raise FacetSpecError(f"facet {noun!r}: transitions must be a table of verb = status")
        for verb, to in transitions.items():
            _ident(verb, f"facet {noun!r}: transition verb")
            if to not in values:
                raise FacetSpecError(
                    f"facet {noun!r}: transition {verb!r} goes to {to!r}, not one of its statuses")
            if verb in GRAPH_VERB_NAMES:
                raise FacetSpecError(
                    f"facet {noun!r}: transition {verb!r} is the name of a graph verb "
                    f"({noun}_{verb}); name the transition for the move it makes")
            if values[to] == "retired":
                raise FacetSpecError(
                    f"facet {noun!r}: transition {verb!r} goes to {to!r} (role retired): "
                    "an item is retired by being replaced (the supersede aspect)")
        if "progress" in have and "done" not in values.values():
            raise FacetSpecError(f"facet {noun!r}: the progress aspect needs a status with role done")
        if "supersede" in have and "retired" not in values.values():
            raise FacetSpecError(f"facet {noun!r}: the supersede aspect needs a status with role retired")
        out.update(statuses=MappingProxyType(values), initial=initial,
                   transitions=MappingProxyType(dict(transitions)))
    edges = raw.get("edges")
    if edges is not None:
        if "links" not in have:
            raise FacetSpecError(f"facet {noun!r}: an edges table needs the links aspect")
        if not isinstance(edges, dict) or not all(v in LINK_KINDS for v in edges.values()):
            raise FacetSpecError(
                f"facet {noun!r}: edges are target = link kind (one of {list(LINK_KINDS)})")
        out["edges"] = MappingProxyType({str(k): str(v) for k, v in edges.items()})
    out["flags"] = MappingProxyType(_flags(noun, raw, have))
    if "links" in have and "edges" not in out and not builtin:
        # No edges table: links toward its OWN items mean what the facet is
        # for: ordering when it tracks progress (wait until done), grounding
        # otherwise (blocked while gated or stale). The rest as crib's own.
        own = "follows" if "progress" in have else "rests_on"
        out["edges"] = MappingProxyType({**_DEFAULT_EDGES, noun: own})
    return out



def check(specs: Iterable[FacetSpec]) -> tuple[FacetSpec, ...]:
    """The specs as one registry: names unique and not crib's own nouns, and
    exactly one home facet."""
    out = tuple(specs)
    taken: dict[str, str] = {}
    for s in out:
        for what, name in (("noun", s.noun), ("plural", s.plural)):
            if name in RESERVED_NAMES:
                raise FacetSpecError(
                    f"facet {s.noun!r}: {what} {name!r} is reserved by crib"
                )
            if name in taken and not (what == "plural" and taken[name] == s.noun):
                raise FacetSpecError(
                    f"facet {s.noun!r}: {what} {name!r} is already {taken[name]!r}'s"
                )
            taken[name] = s.noun
    stores: dict[str, str] = {}
    for s in out:
        for name in (s.store, *s.legacy_stores):
            if name in stores:
                raise FacetSpecError(
                    f"facet {s.noun!r}: store {name!r} is already {stores[name]!r}'s"
                )
            stores[name] = s.noun
    if sum(s.noun == HOME for s in out) != 1:
        raise FacetSpecError(f"the registry needs exactly one {HOME!r} facet")
    return out


def load(text: str, *, builtin: bool = False) -> tuple[FacetSpec, ...]:
    """A facets file's specs, in file order (each checked on its own)."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise FacetSpecError(f"facets file is not valid TOML: {e}") from e
    out = []
    for noun, raw in doc.items():
        if not isinstance(raw, dict):
            raise FacetSpecError(f"facet {noun!r} must be a table")
        out.append(parse(noun, raw, builtin=builtin))
    return tuple(out)


BUILTINS = check(
    load(resources.files(__package__).joinpath("facets.toml").read_text(), builtin=True)
)


def system_file() -> Path:
    """The system facets file: `$CRIB_FACETS_FILE`, else `facets.toml` in
    crib's config dir (paths.config_dir: `$CRIB_CONFIG_DIR`, else
    `$XDG_CONFIG_HOME/crib`, else ~/.config/crib), beside config.toml. Separate
    from it: it is read once, at import, before any config is loaded."""
    from .paths import config_dir

    if f := os.environ.get("CRIB_FACETS_FILE"):
        return Path(f).expanduser()
    return config_dir() / "facets.toml"


def declared(text: str) -> tuple[FacetSpec, ...]:
    """The registry with a system facets file's declarations added after the
    built-ins. Only new facets for now: naming a built-in (to turn it off or
    extend it) is refused until that lands."""
    specs = load(text)
    builtin = {s.noun for s in BUILTINS}
    for s in specs:
        if s.noun in builtin:
            raise FacetSpecError(
                f"facet {s.noun!r} is built in: a facets file cannot change "
                "built-in facets yet")
    return check((*BUILTINS, *specs))


def _resolve() -> tuple[tuple[FacetSpec, ...], str | None]:
    """The registry for this process, and why the system file was ignored (if
    it was). A bad file never stops crib: it warns and serves the built-ins,
    the way a malformed lsp.json does."""
    f = system_file()
    try:
        text = f.read_text()
    except FileNotFoundError:
        return BUILTINS, None
    except OSError as e:
        err = f"cannot read {f}: {e}"
    else:
        try:
            return declared(text), None
        except FacetSpecError as e:
            err = f"ignoring {f}: {e}"
    print(f"[crib] {err}", file=sys.stderr)
    return BUILTINS, err


# The registry: the built-ins, then any declared facets. Resolved once per
# process; LOAD_ERROR says why a system facets file was ignored.
FACETS, LOAD_ERROR = _resolve()
_BY_NOUN = MappingProxyType({s.noun: s for s in FACETS})
_BY_STORE = MappingProxyType({s.store: s for s in FACETS})


def fingerprint(spec: FacetSpec) -> str:
    """What a store's table for this facet was built from: its store, aspects
    and columns. A persistent store records it per declared facet; a change is
    conformed explicitly (`crib facets sync`)."""
    import json

    return json.dumps(
        {"store": spec.store, "aspects": sorted(spec.aspects),
         "fields": [[n, t] for n, t in spec.fields.items()]},
        sort_keys=True,
    )


def to_toml(specs: Iterable[FacetSpec]) -> str:
    """Specs as a facets file (the format `load` reads): a built-in's fields
    in its store column types, a declared facet's in its own (int/str/date),
    the aspects' own fields left out (they come with the aspect)."""

    def q(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'

    def arr(vs: Iterable[str]) -> str:
        return "[" + ", ".join(q(v) for v in vs) + "]"

    out: list[str] = []
    for s in specs:
        brought = {n for a in s.aspects for n in ASPECT_FIELDS.get(a, {})}
        if {"links", "status"} <= s.aspects:
            brought |= set(graph_fields(s.aspects))
        out.append(f"[{s.noun}]")
        out.append(f"plural = {q(s.plural)}")
        if s.store != s.plural:
            out.append(f"store = {q(s.store)}")
        out.append(f"aspects = {arr(['core', *sorted(s.aspects - {'core'})])}")
        if s.legacy_stores:
            out.append(f"legacy_stores = {arr(s.legacy_stores)}")
        if s.disabled:
            out.append(f"disable = {arr(sorted(s.disabled))}")
        if s.about:
            out.append(f"about = {q(s.about)}")
        if s.optional:
            out.append(f"optional = {arr([n for n in s.params if n in s.optional])}")
        if s.title_from != ("title",):
            out.append(f"title_from = {arr(s.title_from)}")
        for name, f in FLAGS.items():
            if name in s.flags:
                v = s.flags[name]
                out.append(f"{name} = {'true' if f.type == 'bool' else q(str(v))}")
        own = dict(s.params) if s.params else {
            n: t for n, t in s.fields.items() if n not in brought}
        if own:
            out.append(f"\n[{s.noun}.fields]")
            out += [f"{n} = {q(t)}" for n, t in own.items()]
        if s.when:
            out.append(f"\n[{s.noun}.when]")
            out += [f"{v} = {q(w)}" for v, w in s.when.items()]
        if s.verb_names:
            out.append(f"\n[{s.noun}.verbs]")
            out += [f"{v} = {q(n)}" for v, n in s.verb_names.items()]
        if s.statuses:
            def inline(m: Mapping[str, str]) -> str:
                return "{ " + ", ".join(f"{q(k)} = {q(v)}" for k, v in m.items()) + " }"

            out.append(f"\n[{s.noun}.status]")
            out.append(f"values = {inline(s.statuses)}")
            out.append(f"initial = {q(s.initial or '')}")
            if s.transitions:
                out.append(f"transitions = {inline(s.transitions)}")
        if s.bands:
            out.append(f"\n[{s.noun}.bands]")
            out += [f"{b} = {q(n)}" for b, n in s.bands.items()]
        if s.edges:
            out.append(f"\n[{s.noun}.edges]")
            out += [f"{q(t)} = {q(k)}" for t, k in s.edges.items()]
        out.append("")
    return "\n".join(out)


STARTER = '''# crib's system facets file: declare your own kinds of item (facets).
#
# Every facet is a spec: a noun, a plural (its tool set), its aspects (what
# the engine does for it) and its own typed fields. A declared facet gets the
# verbs of its aspects as MCP tools and CLI commands: `<noun>_add`, `_read`,
# `_lookup`, ... (`crib <noun> ...`).
#
#   aspects: core (always: add, read, edit, append, forget, lookup, apropos,
#            locate, versions, restore, reindex, import, move), enriched
#            (summaries/keywords: summarize, elaborate, distill), anchored
#            (items about a code symbol, like learnings: add, edit, forget,
#            read, reaffirm, migrate, report, rehome); or a GRAPH facet like
#            decisions and plans: links + status, optionally progress,
#            ordered, supersede, sources, with [<noun>.status] values (each
#            status plays a role), initial, transitions, and [<noun>.edges]
#            (see crib's docs/facets.md and the built-ins below)
#   fields:  int | str | date; set on add/edit, filtered on lookup and list
#            (`where=[{field, op, value}]`: == != < <= > >= for int/date,
#            == != re for str). Note-like and anchored facets; not yet on a
#            graph facet
#   disable: verbs this facet doesn't want
#   about:   what one of these items is and when to use this family (rather
#            than notes): added to every one of its tool descriptions
#   [<noun>.when]: verb = extra wording for that one tool
#
# After an edit here: `crib facets sync` shows what the store needs,
# `crib facets sync --apply` does it, then restart the daemon. A repo maps a
# facet onto a directory of its own in its `.crib`:
#   facets:
#     routine: {dir: workflows, glob: "*.md"}    # read_only?, ids: stamp|derived
#
# Uncomment an example to start.

# [routine]
# plural = "routines"
# aspects = ["core", "enriched"]
# about = "A routine is a job an agent runs on a schedule. Use it for recurring work; a one-off fact is a note."
# disable = ["import"]
#
# [routine.fields]
# schedule = "str"
# timeout = "int"
# next_run = "date"

# [review]
# plural = "reviews"
# aspects = ["core", "anchored"]
#
# [review.fields]
# changeset_id = "str"
# severity = "int"

# ── crib's built-in facets, for reference (they can't be changed here yet) ──
'''


def instructions(specs: Iterable[FacetSpec] | None = None,
                 pending: Iterable[str] = ()) -> str:
    """The agent-facing section on THIS machine's declared facets (markdown;
    "" without any): what each is (its `about`), its tool family, its shape
    and fields. crib's own facets are described by the hand-written directive
    (CLAUDE.md.example, shipped in the plugins); a declared facet exists only on
    the machines that declare it, so its guidance is generated where it runs:
    appended to the MCP server's instructions, and printed by `crib facets
    instructions` for the plugins to add to their directive. `pending`: facets
    the store isn't conformed to yet."""
    declared = [s for s in (FACETS if specs is None else specs) if not s.builtin]
    if not declared:
        return ""
    waiting = set(pending)
    out = [
        "### Declared facets on this machine",
        "",
        "Besides crib's own notes, decisions, plans and learnings, this machine "
        "declares these kinds of item. Each has its own store, search scope and "
        "tools: use that family for that kind of item, not `note_store`.",
        "",
    ]
    for s in declared:
        if {"links", "status"} <= s.aspects:
            roles = ", ".join(f"{st} ({r})" for st, r in s.statuses.items())
            shape = f"A graph facet with dependencies and statuses: {roles}"
            if "progress" in s.aspects:
                shape += f"; `{s.noun}_next` lists what is ready"
        elif "anchored" in s.aspects:
            shape = "Each item is about a code symbol; they show on `code_dossier`"
        else:
            shape = "Note-like" + (", with summaries and keywords" if "enriched" in s.aspects else "")
        if s.params:
            shape += "; fields " + ", ".join(f"`{n}` ({t})" for n, t in s.params.items())
        line = f"- **{s.plural}** (`{s.noun}_*`, `crib {s.noun} ...`): "
        line += (s.about.rstrip(".") + ". " if s.about else "") + shape + "."
        if s.noun in waiting:
            line += (" Not ready in this store yet: it waits for "
                     "`crib facets sync --apply` (its tools refuse until then).")
        out.append(line)
    out += ["", "A tool of one of these that refuses with \"isn't ready in this store\" "
            "is waiting for `crib facets sync --apply`: say so rather than work "
            "around it."]
    return "\n".join(out) + "\n"


def starter() -> str:
    """The starter system facets file: everything commented, so it declares
    nothing until an example is uncommented."""
    ref = "\n".join(f"# {line}" if line else "#" for line in to_toml(BUILTINS).splitlines())
    return STARTER + ref + "\n"


def link_default(source: str | None, target: str | None) -> str:
    """The link kind a dependency gets when none is written: the source facet's
    `edges` table, by target facet (`"*"` for the rest)."""
    spec = _BY_NOUN.get(source or "")
    table = spec.edges if spec is not None and spec.edges else _DEFAULT_EDGES
    return table.get(target or "") or table.get("*") or "informed_by"


# The roles whose items are finished business: a near-twin of one is answered
# by that state ("already done"), not by "merge them".
SETTLED_ROLES = frozenset({"done", "dropped", "paused", "retired"})


def item_kinds() -> tuple[str, ...]:
    """The facets whose items stand on their own (every one but the anchored,
    whose items are about a code symbol): what near-duplicate and `similar`
    checks compare, in registry order."""
    return tuple(s.noun for s in FACETS if not s.has("anchored"))


def settled(kind: str, status: str | None) -> bool:
    """Is an item of `kind` with `status` finished business (SETTLED_ROLES)?"""
    spec = _BY_NOUN.get(kind)
    return spec is not None and spec.role(status) in SETTLED_ROLES


def get(noun: str | None) -> FacetSpec | None:
    """The facet named `noun`, or None (a non-facet kind: doc, symbol, ...)."""
    return _BY_NOUN.get(noun or "")


def by_noun(noun: str) -> FacetSpec:
    try:
        return _BY_NOUN[noun]
    except KeyError:
        raise KeyError(f"no facet {noun!r} (facets: {sorted(_BY_NOUN)})") from None


def by_store(store: str) -> FacetSpec | None:
    return _BY_STORE.get(store)


def nouns() -> tuple[str, ...]:
    return tuple(s.noun for s in FACETS)


def stores() -> tuple[str, ...]:
    return tuple(s.store for s in FACETS)
