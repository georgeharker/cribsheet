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
    }
)

# The fields an aspect brings: every facet with the aspect has them, first and
# in this order, before its own. Anchoring writes the symbol an item is about.
# The verbs each aspect brings (crib.verbs holds their templates; a test keeps
# the two in step). An anchored facet's items are named by symbol, so its
# family REPLACES the relpath-addressed core and enriched verbs. `list` comes
# with declared fields, whatever the aspects.
ASPECT_VERBS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "core": (
            "lookup", "apropos", "read", "locate", "add", "append", "edit", "forget",
            "reindex", "versions", "restore", "import", "move",
        ),
        "enriched": ("distill", "elaborate", "summarize"),
        "anchored": (
            "add", "edit", "forget", "read", "reaffirm", "migrate", "report", "rehome",
        ),
    }
)


def verbs_for(aspects: Iterable[str], has_fields: bool) -> tuple[str, ...]:
    """The canonical verbs a facet with these aspects (and declared fields, or
    not) gets, before any it disables."""
    have = set(aspects)
    if "anchored" in have:
        out = list(ASPECT_VERBS["anchored"])
    else:
        out = [v for a in ("core", "enriched") if a in have | {"core"}
               for v in ASPECT_VERBS[a]]
    return (*out, "list") if has_fields else tuple(out)


def graph_verbs(spec: "FacetSpec") -> tuple[str, ...]:
    """The canonical verbs a graph facet (links + status) gets, before any it
    disables: its core and aspect verbs, the taint-reporting ones if it reports
    taint, and one per status transition (crib.verbs GRAPH_TEMPLATES)."""
    out = ["add", "read", "edit", "append", "lookup", "list", "forget"]
    if spec.has("sources"):
        out += ["export", "extract"]
    out += ["dep_add", "dep_remove", "reaffirm", "graph"]
    if spec.report_taint:
        out += ["check", "tree"]
    if spec.has("supersede"):
        out.append("supersede")
    if spec.has("progress"):
        out += ["status", "review", "review_apply", "next"]
    if spec.has("ordered"):
        out.append("move")
    return (*out, *spec.transitions)


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

# Nouns that are crib's but not pillars: CLI/MCP families and non-pillar item
# kinds. No facet may take one, as noun or plural.
RESERVED_NAMES = frozenset(
    {
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

_KEYS = frozenset(
    {"plural", "store", "aspects", "fields", "legacy_stores", "verbs", "disable",
     "status", "edges", "label", "requires_body", "extract", "report_taint",
     "export_with_deps"}
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
    label: str | None = None  # how a listing of these items is headed
    requires_body: bool = False  # an item must say something (a decision)
    extract: str | None = None  # the import procedure's prompt (design | plan)
    # Its staleness is REPORTED (a `tainted` marker on hits and exports, rows in
    # `check`, a next-step hint), not only felt through blocking (decisions).
    report_taint: bool = False
    # An export includes the items its own items depend on (a plan export
    # carries the decisions it rests on).
    export_with_deps: bool = False

    def role(self, status: str | None) -> str | None:
        """The role `status` plays, or None (not one of this facet's)."""
        return self.statuses.get(status or "")

    def with_role(self, *roles: str) -> tuple[str, ...]:
        """This facet's statuses playing any of `roles`, in declared order."""
        return tuple(s for s, r in self.statuses.items() if r in roles)
    builtin: bool = False

    def has(self, aspect: str) -> bool:
        return aspect in self.aspects


def _ident(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.isidentifier() or value != value.lower():
        raise FacetSpecError(f"{what} must be a lowercase identifier, got {value!r}")
    return value


def parse(noun: str, raw: Mapping[str, Any], *, builtin: bool = False) -> FacetSpec:
    """One `[<noun>]` table of a facets file as a spec, checked on its own."""
    _ident(noun, "a facet name")
    if bad := set(raw) - _KEYS:
        raise FacetSpecError(f"facet {noun!r}: unknown keys {sorted(bad)}")
    plural = _ident(raw.get("plural"), f"facet {noun!r}: plural")
    store = raw.get("store", plural)
    if not isinstance(store, str) or not store or "/" in store or store.startswith("."):
        raise FacetSpecError(
            f"facet {noun!r}: store must be a directory name, got {store!r}"
        )
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
    fields = {**brought, **fields}
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
    available = verbs_for(have, bool(params))
    if bad := sorted(set(disable) - set(available)):
        raise FacetSpecError(
            f"facet {noun!r}: can't disable {bad}: its verbs are {list(available)}")
    if not set(available) - set(disable):
        raise FacetSpecError(f"facet {noun!r}: disables every one of its verbs")
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
        **_graph_keys(noun, raw, have),
        builtin=builtin,
    )


def _graph_keys(noun: str, raw: Mapping[str, Any], have: set[str]) -> dict[str, Any]:
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
    if (label := raw.get("label")) is not None:
        out["label"] = str(label)
    if raw.get("requires_body"):
        out["requires_body"] = True
    for key in ("report_taint", "export_with_deps"):
        if raw.get(key):
            if "links" not in have:
                raise FacetSpecError(f"facet {noun!r}: {key} needs the links aspect")
            out[key] = True
    if (ex := raw.get("extract")) is not None:
        if ex not in ("design", "plan"):
            raise FacetSpecError(f"facet {noun!r}: extract is design or plan, not {ex!r}")
        out["extract"] = ex
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
    """The system facets file: `$CRIB_FACETS_FILE`, else `facets.toml` in the
    config dir (`$CRIB_CONFIG_DIR`, else ~/.config/crib). Separate from
    config.toml: it is read once, at import, before any config is loaded."""
    if f := os.environ.get("CRIB_FACETS_FILE"):
        return Path(f).expanduser()
    base = os.environ.get("CRIB_CONFIG_DIR") or "~/.config/crib"
    return Path(base).expanduser() / "facets.toml"


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
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'

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
        if s.label:
            out.append(f"label = {q(s.label)}")
        if s.requires_body:
            out.append("requires_body = true")
        if s.extract:
            out.append(f"extract = {q(s.extract)}")
        for key in ("report_taint", "export_with_deps"):
            if getattr(s, key):
                out.append(f"{key} = true")
        own = dict(s.params) if s.params else {
            n: t for n, t in s.fields.items() if n not in brought}
        if own:
            out.append(f"\n[{s.noun}.fields]")
            out += [f"{n} = {q(t)}" for n, t in own.items()]
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
#            read, reaffirm, migrate, report, rehome)
#   fields:  int | str | date; set on add/edit, filtered on lookup and list
#            (`where=[{field, op, value}]`: == != < <= > >= for int/date,
#            == != re for str)
#   disable: verbs this facet doesn't want
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


def starter() -> str:
    """The starter system facets file: everything commented, so it declares
    nothing until an example is uncommented."""
    ref = "\n".join(f"# {line}" if line else "#" for line in to_toml(BUILTINS).splitlines())
    return STARTER + ref + "\n"


# The built-in graph facets' shared default link kinds: what a link means
# when its SOURCE isn't known (and the rule every built-in source follows).
_DEFAULT_EDGES = MappingProxyType(
    {"plan": "follows", "design": "rests_on", "*": "informed_by"})


def link_default(source: str | None, target: str | None) -> str:
    """The link kind a dependency gets when none is written: the source facet's
    `edges` table, by target facet (`"*"` for the rest)."""
    spec = _BY_NOUN.get(source or "")
    table = spec.edges if spec is not None and spec.edges else _DEFAULT_EDGES
    return table.get(target or "") or table.get("*") or "informed_by"


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
