"""The facet registry (crib.facets): the built-in specs, what derives from them,
and the checks that refuse a bad spec."""

from __future__ import annotations

import pytest

from crib import chunk, facets, items, notestore, watch
from crib.facets import FacetSpecError, check, load, parse


def test_builtins_are_the_four_pillars_in_order():
    assert facets.nouns() == ("note", "design", "plan", "learning")
    assert facets.stores() == ("notes", "design", "plans", "learnings")
    assert [s.plural for s in facets.FACETS] == [
        "notes",
        "designs",
        "plans",
        "learnings",
    ]
    assert all(s.builtin for s in facets.FACETS)


def test_builtin_aspects():
    a = {s.noun: s.aspects for s in facets.FACETS}
    assert a["note"] == {"core", "enriched"}
    assert a["design"] == {"core", "links", "status", "supersede", "sources"}
    assert a["plan"] == {"core", "links", "status", "progress", "ordered", "sources"}
    assert a["learning"] == {"core", "anchored"}


def test_derived_constants_match_the_old_hand_written_ones():
    # What the registry replaced, spelled out once: moving the built-ins onto
    # it must not change any of them.
    assert chunk.PILLAR_KIND == {
        "notes": "note",
        "design": "design",
        "plans": "plan",
        "learnings": "learning",
    }
    assert chunk.KIND_PILLAR == {
        "note": "notes",
        "design": "design",
        "plan": "plans",
        "learning": "learnings",
        "doc": "notes",
    }
    assert watch.PILLAR_SEGMENTS == ("notes", "design", "plans", "learnings")
    assert notestore.NOTES_SPEC == notestore.StoreSpec(
        "notes",
        "notes",
        reserved=("design/", "plans/", "learnings/", "code-learnings/"),
    )
    assert notestore.DESIGN_SPEC == notestore.StoreSpec(
        "design", "design", facet="design"
    )
    assert notestore.PLANS_SPEC == notestore.StoreSpec("plans", "plans", facet="plan")
    assert notestore.LEARNINGS_SPEC == notestore.StoreSpec(
        "learnings", "learnings", facet="learning"
    )
    assert dict(items.FACETS["note"]) == {
        "source": "VARCHAR",
        "tags": "VARCHAR[]",
        "source_path": "VARCHAR",
    }
    assert list(items.FACETS["plan"]) == [
        "status",
        "rank",
        "trigger",
        "reviewed",
        "created",
        "updated",
        "body_hash",
    ]
    assert dict(items.FACETS["learning"]) == {
        "symbol_ref": "VARCHAR",
        "lang": "VARCHAR",
        "file": "VARCHAR",
        "signature": "VARCHAR",
        "symbol_was": "VARCHAR[]",
    }
    # crib's own non-pillar kinds stay beside them
    assert {"keywords", "summary", "symbol"} <= set(items.FACETS)


def test_lookup_helpers():
    assert facets.by_noun("plan").store == "plans"
    learnings = facets.by_store("learnings")
    assert learnings is not None and learnings.noun == "learning"
    assert facets.by_store("nope") is None
    with pytest.raises(KeyError, match="no facet 'routine'"):
        facets.by_noun("routine")


def test_parse_defaults_store_to_plural_and_always_has_core():
    s = parse(
        "routine",
        {
            "plural": "routines",
            "aspects": ["enriched"],
            "fields": {"schedule": "VARCHAR", "timeout": "BIGINT"},
        },
    )
    assert s.store == "routines"
    assert s.aspects == {"core", "enriched"}
    assert list(s.fields) == ["schedule", "timeout"]
    assert not s.builtin


@pytest.mark.parametrize(
    ("noun", "raw", "match"),
    [
        ("Routine", {"plural": "routines"}, "lowercase identifier"),
        ("routine", {}, "plural must be"),
        ("routine", {"plural": "routines", "color": "red"}, "unknown keys"),
        ("routine", {"plural": "routines", "aspects": ["magic"]}, "unknown aspects"),
        (
            "routine",
            {"plural": "routines", "aspects": ["progress"]},
            "needs \\['status'\\]",
        ),
        (
            "routine",
            {"plural": "routines", "aspects": ["supersede", "status"]},
            "needs \\['links'\\]",
        ),
        ("routine", {"plural": "routines", "fields": {"n": "INT128"}}, "has type"),
        ("routine", {"plural": "routines", "store": "a/b"}, "directory name"),
        ("routine", {"plural": "routines", "legacy_stores": "old"}, "legacy_stores"),
    ],
)
def test_parse_refuses_a_bad_spec(noun, raw, match):
    with pytest.raises(FacetSpecError, match=match):
        parse(noun, raw)


def _with(*extra):
    return (*facets.BUILTINS, *extra)


def test_check_refuses_reserved_and_taken_names():
    with pytest.raises(FacetSpecError, match="reserved by crib"):
        check(_with(parse("code", {"plural": "codes"})))
    with pytest.raises(FacetSpecError, match="reserved by crib"):
        check(_with(parse("routine", {"plural": "projects"})))
    with pytest.raises(FacetSpecError, match="already 'plan'"):
        check(_with(parse("routine", {"plural": "plans"})))
    with pytest.raises(FacetSpecError, match="store 'learnings'"):
        check(_with(parse("routine", {"plural": "routines", "store": "learnings"})))
    with pytest.raises(FacetSpecError, match="store 'code-learnings'"):
        check(
            _with(parse("routine", {"plural": "routines", "store": "code-learnings"}))
        )


def test_check_needs_exactly_one_home_facet():
    without_notes = [s for s in facets.BUILTINS if s.noun != facets.HOME]
    with pytest.raises(FacetSpecError, match="exactly one 'note'"):
        check(without_notes)


def test_check_accepts_a_declared_facet_and_notes_reserve_its_store():
    reg = check(_with(parse("routine", {"plural": "routines"})))
    assert [s.noun for s in reg][-1] == "routine"


def test_load_reads_file_order_and_rejects_bad_toml():
    specs = load('[b]\nplural = "bs"\n[a]\nplural = "as"\n')
    assert [s.noun for s in specs] == ["b", "a"]
    with pytest.raises(FacetSpecError, match="not valid TOML"):
        load("[a\n")
    with pytest.raises(FacetSpecError, match="must be a table"):
        load("a = 1\n")


def test_only_builtins_may_rename_verbs():
    with pytest.raises(FacetSpecError, match="only built-in facets may rename verbs"):
        parse("routine", {"plural": "routines", "verbs": {"add": "store"}})
    s = parse("thing", {"plural": "things", "verbs": {"add": "store"}}, builtin=True)
    assert dict(s.verb_names) == {"add": "store"}
    assert dict(facets.by_noun("note").verb_names) == {"add": "store"}
