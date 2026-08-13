"""The symbol entry's SHAPE and its spelling rules, enforced mechanically.

Both checks exist because the manual version failed. `scope` was added to the entry
dict and not to the persist allow-list, and nothing said so: every reader does
`e.get(...)`, so the field was simply absent 2220 times. And the edge/separator
conventions were found by grepping for one spelling of them, which finds only the
sites written that way — `drop_file`'s `endswith(tag)`, `match_meta`'s
`endswith("." + k)` and `learning_rehome`'s bare-name match all hid from it.

The corpus golden (`test_corpus_goldens.py`) would catch a shape change too, but it
clones repos and runs an LSP, so it is opt-in and does not run by default. These are
the always-on floor.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from crib.codeindex import _ARRAYS, _SCALARS, _render

_CRIB = Path(__file__).resolve().parent.parent / "crib"

# Every field a stored entry may carry. Adding one to the extractor without adding
# it here (and to _SCALARS/_ARRAYS) is the failure this pins.
ENTRY_FIELDS = {
    "fqname", "name", "kind", "lang", "module", "scope", "parent", "container",
    "content_hash", "file", "file_hash", "line", "mtime", "signature",
    "description", "keywords", "calls", "called_by", "references", "name_terms",
}


def test_the_persist_allow_list_covers_every_declared_field():
    persisted = set(_SCALARS) | set(_ARRAYS) | {"line", "mtime"}
    assert persisted == ENTRY_FIELDS, (
        "the entry shape and what _render persists disagree; a field in one and not "
        "the other is written to memory and lost on disk, silently")


def test_a_rendered_entry_round_trips_every_field():
    entry = {k: "" for k in _SCALARS}
    entry.update(fqname="a.b.C.d", name="d", lang="python", file="a/b.py",
                 line=7, mtime=3, container=["C"], scope=["a", "b", "C"],
                 calls=["e [a/c.py]"], called_by=[], references=[],
                 name_terms=["d"], keywords=["k"])
    from crib.codeindex import _parse
    back = _parse(_render(entry))
    for key in ("fqname", "name", "lang", "file", "container", "scope", "calls"):
        assert back.get(key) == entry[key], f"{key} did not survive the round trip"


@pytest.mark.parametrize("pattern, what", [
    (r'partition\(" \["\)', "parsing an edge ref by hand"),
    (r'\.endswith\(f?"\[', "testing an edge ref's origin by hand"),
    (r'\.endswith\("\." \+ ', "matching a qualified-name suffix by hand"),
    (r'\.endswith\("::" \+ ', "matching a qualified-name suffix by hand"),
    (r're\.compile\(r"::\|', "restating the separator rule"),
])
def test_no_module_but_symbols_knows_how_a_symbol_is_spelled(pattern, what):
    """`crib/symbols.py` owns every spelling convention. A second copy anywhere else
    is the drift that made `by_fqname` blind to Rust while `_qualify` rendered it."""
    offenders = [p.name for p in sorted(_CRIB.glob("*.py"))
                 if p.name != "symbols.py" and re.search(pattern, p.read_text())]
    assert not offenders, f"{what} outside symbols.py: {offenders}"


def test_a_read_verb_never_mutates_the_resident_cache():
    """Entries from the resident cache are the CALLER's. `code_xref` stamps a
    project onto them, `learnings.attach` adds the pinned note, `code_dossier`
    structures the edge lists — all in place. While those writes were idempotent it
    only smelled; the moment one changed a field's TYPE, the next reader in the
    process got a list it could not decode twice."""
    from crib.codestore import _ResidentCode
    entries = [{"fqname": "a.b", "name": "b", "file": "a.py", "lang": "python",
                "calls": ["c [a.py]"], "description": ""}]
    rc = _ResidentCode(tok=1, entries=entries, emb={})
    got = rc.by_fqname("b")
    got[0]["project"] = "somewhere"
    got[0]["calls"] = [{"symbol": "a.c"}]
    assert entries[0] == {"fqname": "a.b", "name": "b", "file": "a.py",
                          "lang": "python", "calls": ["c [a.py]"], "description": ""}
