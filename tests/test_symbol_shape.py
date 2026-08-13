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
def _entry_fields() -> set[str]:
    """The keys the extractor actually puts in an entry, READ FROM THE SOURCE.

    This used to be a hand-written set compared against a hand-written allow-list —
    two lists maintained by the same person at the same moment, so they agreed with
    each other and neither had to agree with the code. Adding `schema` to the entry
    proved it: both lists stayed stale and the test stayed green. Deriving one side
    is what makes this a check rather than a restatement.
    """
    import ast

    src = ast.parse((_CRIB / "codeindex.py").read_text())
    for node in ast.walk(src):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if {"symbol_ref", "name", "kind", "file"} <= keys:   # the entry literal
            # `_body` is the raw source text, carried in memory for hashing and
            # describing and deliberately never written — the underscore is the
            # convention that says so.
            return {k for k in keys if not k.startswith("_")}
    raise AssertionError("could not find the entry dict in codeindex.py")


def test_the_persist_allow_list_covers_every_declared_field():
    # `line`, `mtime` and `schema` are rendered explicitly (they are ints, not the
    # quoted scalars) — persisted, just not via the allow-lists.
    persisted = set(_SCALARS) | set(_ARRAYS) | {"line", "mtime", "schema"}
    # The semantic facet is attached AFTER extraction, by the describe pass, so it is
    # persisted without appearing in the entry literal. Both are real fields; the
    # asymmetry is in when they are bound, not whether they are stored.
    late_bound = {"description", "keywords"}
    assert persisted - late_bound == _entry_fields(), (
        "the entry shape and what _render persists disagree; a field in one and not "
        "the other is written to memory and lost on disk, silently")


def test_a_rendered_entry_round_trips_every_field():
    entry = {k: "" for k in _SCALARS}
    entry.update(symbol_ref="a/b.py#C.d", fqn="a.b.C.d", name="d", lang="python",
                 file="a/b.py", line=7, mtime=3, container=["C"],
                 scope=["a", "b", "C"], symbol_was=["a.b.C.d"],
                 calls=["e [a/c.py]"], called_by=[], references=[],
                 name_terms=["d"], keywords=["k"])
    from crib.codeindex import _parse
    back = _parse(_render(entry))
    for key in ("symbol_ref", "fqn", "symbol_was", "name", "lang", "file",
                "container", "scope", "calls"):
        assert back.get(key) == entry[key], f"{key} did not survive the round trip"


# The code-family modules — where a symbol spelling could plausibly be re-derived.
# `designs.py` is exempt on purpose: its `doc#heading` citations are a DIFFERENT
# convention that module owns, not a symbol reference.
_CODE_FAMILY = ("codeindex.py", "codeindexer.py", "codestore.py", "codequery.py",
                "learnings.py", "refs.py", "symconvert.py")


def test_no_code_module_splits_a_reference_by_hand():
    """`symbols.id_parts` / `match_entry` own the `#` convention. A hand
    `partition("#")` beside them is the second-copy drift this file exists to pin —
    and `id_parts` already passes a #-less input through, so there is no legitimate
    reason for a caller to pre-test with `"#" in …` either."""
    offenders = [f"{name}: {ln.strip()[:60]}"
                 for name in _CODE_FAMILY
                 for n, ln in enumerate((_CRIB / name).read_text().splitlines(), 1)
                 if ('partition("#")' in ln or 'split("#")' in ln)
                 and "id_parts" not in ln]
    assert not offenders, f"reference split by hand outside symbols.py: {offenders}"


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


def test_only_the_full_sweep_stamps_the_store():
    """`record_schema` may be called ONLY from the whole-project sweep. One file is
    not the store, so a single-file write must not claim the store's shape: the
    stamp's whole job is to make a mixed store visible, and a marker written on the
    evidence of one entry asserts a shape the other entries may not have.

    This is a regression test, and it is a SOURCE-level one on purpose. The bug lived
    in `CodeIndexer`, not in `SymbolIndex.write` — a behavioural test that wrote one
    entry through the store would have passed both before and after the fix, because
    the store never stamped itself. What went wrong was a CALL SITE, so the call
    sites are what this pins.

    History: `record_schema`'s own docstring said "called when a FULL sweep
    completes, never per file", and the single-file path called it anyway, justified
    by a comment claiming the stale-gate had "proved the store was not another one".
    It had not — the gate admits an UNSTAMPED store because stamp 0 means shape
    UNKNOWN, which is the opposite of proof. The watcher then left three real stores
    mixed AND mislabeled: music-llm 1120 of 3798 entries in the new shape,
    mcp-companion 57 of 1853, dotfiles 10 of 95.
    """
    callers = {}
    for src in sorted(_CRIB.glob("*.py")):
        if src.name == "codeindex.py":       # where it is DEFINED
            continue
        for n, line in enumerate(src.read_text().splitlines(), 1):
            if re.search(r"\.record_schema\s*\(", line):
                callers[f"{src.name}:{n}"] = line.strip()

    # Exactly the passes that SEE EVERY RECORD may stamp: the full sweep
    # (codeindexer) and the converter (app.code_convert), which walks the whole
    # store and stamps only when nothing was skipped. Anything else appearing here
    # is the watcher bug pattern again.
    assert len(callers) == 2, (
        "record_schema may be called from exactly TWO sites — the full sweep and "
        "the converter. Found:\n"
        + "\n".join(f"  {k}  {v}" for k, v in callers.items()))
    files = {k.split(":")[0] for k in callers}
    assert files == {"codeindexer.py", "app.py"}, (
        f"record_schema called from {sorted(files)}; only a pass that saw every "
        f"record may stamp")
