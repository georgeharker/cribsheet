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

# The store column types a facet field may have.
FIELD_TYPES = frozenset({"VARCHAR", "VARCHAR[]", "BIGINT", "DOUBLE", "BOOLEAN", "JSON"})

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

_KEYS = frozenset({"plural", "store", "aspects", "fields", "legacy_stores", "verbs"})


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
    for name, sqltype in fields.items():
        _ident(name, f"facet {noun!r}: field name")
        if sqltype not in FIELD_TYPES:
            raise FacetSpecError(
                f"facet {noun!r}: field {name!r} has type {sqltype!r} "
                f"(allowed: {sorted(FIELD_TYPES)})"
            )
    brought: dict[str, str] = {}
    for a in sorted(have):
        brought.update(ASPECT_FIELDS.get(a, {}))
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
    return FacetSpec(
        noun=noun,
        plural=plural,
        store=store,
        aspects=frozenset(have),
        fields=MappingProxyType(dict(fields)),
        legacy_stores=tuple(legacy),
        verb_names=MappingProxyType(dict(verbs)),
        builtin=builtin,
    )


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
