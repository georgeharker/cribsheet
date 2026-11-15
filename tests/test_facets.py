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
            "fields": {"schedule": "str", "timeout": "int"},
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
        ("my_thing", {"plural": "things"}, "can't contain '_'"),
        (
            "rfc",
            {"plural": "rfcs", "aspects": ["links", "status"],
             "status": {"values": {"open": "open", "done": "done"},
                        "transitions": {"list": "done"}}},
            "name of a graph verb",
        ),
        (
            "rfc",
            {"plural": "rfcs", "aspects": ["links", "status", "supersede"],
             "status": {"values": {"open": "open", "gone": "retired"},
                        "transitions": {"retire": "gone"}}},
            "role retired",
        ),
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


@pytest.mark.parametrize(
    "raw",
    [
        {"plural": "as"},
        {"plural": "bs", "aspects": ["enriched"]},
        {"plural": "cs", "aspects": ["anchored"]},
        {"plural": "ds", "fields": {"n": "int"}},
        {"plural": "es", "aspects": ["anchored"], "fields": {"n": "str"}},
        {"plural": "fs", "fields": {"n": "int"}, "disable": ["move", "list"]},
    ],
)
def test_aspect_verbs_name_exactly_the_templates_a_facet_gets(raw):
    from crib import verbs

    s = parse("thing", raw)
    want = set(facets.verbs_for(s.aspects, bool(s.params))) - s.disabled
    assert {t.verb for t in verbs.verbs_of(s)} == want


def test_aspect_verbs_match_the_builtins_generated_families():
    from crib import verbs

    for s in facets.FACETS:
        if not verbs.generated(s):
            continue
        want = (facets.graph_verbs(s) if s.has("links") and s.has("status")
                else facets.verbs_for(s.aspects, bool(s.params)))
        assert {t.verb for t in verbs.verbs_of(s)} == set(want), s.noun

def test_disable():
    s = parse(
        "t",
        {
            "plural": "ts",
            "aspects": ["enriched"],
            "disable": ["import", "move", "distill"],
        },
    )
    assert s.disabled == {"import", "move", "distill"}
    with pytest.raises(FacetSpecError, match="can't disable \\['reaffirm'\\]"):
        parse("t", {"plural": "ts", "disable": ["reaffirm"]})  # not a core verb
    with pytest.raises(FacetSpecError, match="can't disable \\['list'\\]"):
        parse("t", {"plural": "ts", "disable": ["list"]})  # no fields, no list
    with pytest.raises(FacetSpecError, match="built-in"):
        parse("t", {"plural": "ts", "disable": ["move"]}, builtin=True)
    with pytest.raises(FacetSpecError, match="disables every one"):
        parse("t", {"plural": "ts", "disable": list(facets.verbs_for({"core"}, False))})


def test_to_toml_round_trips_the_builtins():
    again = load(facets.to_toml(facets.BUILTINS), builtin=True)
    assert again == facets.BUILTINS


def test_to_toml_writes_a_declared_facets_own_field_types():
    s = parse(
        "ticket",
        {
            "plural": "tickets",
            "aspects": ["anchored"],
            "fields": {"severity": "int", "due": "date"},
            "disable": ["rehome"],
        },
    )
    text = facets.to_toml([s])
    assert 'severity = "int"' in text and 'due = "date"' in text
    assert "symbol_ref" not in text  # the aspect brings it
    assert load(text) == (s,)


def test_the_starter_declares_nothing_until_an_example_is_uncommented():
    text = facets.starter()
    assert load(text) == ()
    # uncomment the routine example: a valid declared facet
    lines, on = [], False
    for line in text.splitlines():
        if line.startswith("# [routine"):
            on = True
        elif on and not line.startswith("#"):
            on = False
        if on and line.startswith("# "):
            line = line[2:]
        elif on and line == "#":
            line = ""
        lines.append(line)
    (routine,) = load("\n".join(lines))
    assert routine.noun == "routine" and routine.disabled == {"import"}
    assert dict(routine.params) == {
        "schedule": "str",
        "timeout": "int",
        "next_run": "date",
    }


# ── the graph spec: statuses play engine roles (facet registry step 5) ───────


def test_the_builtin_graph_specs_reproduce_the_old_constants():
    from crib import designs

    plan, design = facets.by_noun("plan"), facets.by_noun("design")
    assert designs.PLAN_STATUSES == (
        "todo",
        "in-progress",
        "parked",
        "declined",
        "done",
        "verified",
    )
    assert designs.DESIGN_STATUSES == ("proposed", "active", "superseded")
    assert designs.DONE_STATUSES == ("done", "verified")
    assert (plan.initial, design.initial) == ("todo", "active")
    assert plan.with_role("claimed") == ("in-progress",)
    assert plan.with_role("paused", "dropped") == ("parked", "declined")
    assert design.with_role("gated") == ("proposed",)
    assert design.with_role("retired") == ("superseded",)
    assert dict(plan.transitions) == {"park": "parked", "decline": "declined"}
    assert dict(design.transitions) == {"promote": "active"}
    assert (design.label, plan.label) == ("Design decisions", "Plan items")
    assert design.requires_body and not plan.requires_body
    assert design.report_taint and not plan.report_taint
    assert plan.export_with_deps and not design.export_with_deps
    assert (design.extract, plan.extract) == ("design", "plan")


def test_default_link_kinds_follow_the_source_facets_edges():
    from crib.items import dep_kind_default

    for target, kind in (
        ("plan", "follows"),
        ("design", "rests_on"),
        ("note", "informed_by"),
        (None, "informed_by"),
    ):
        assert dep_kind_default(target) == kind
        assert dep_kind_default(target, "plan") == kind
        assert dep_kind_default(target, "design") == kind


_GRAPH = {
    "plural": "incidents",
    "aspects": ["links", "status", "progress"],
    "status": {
        "values": {
            "open": "open",
            "mitigated": "claimed",
            "resolved": "done",
            "wontfix": "dropped",
        },
        "initial": "open",
        "transitions": {"resolve": "resolved"},
    },
    "edges": {"plan": "follows", "*": "informed_by"},
}


def test_a_declared_graph_spec_parses():
    s = parse("incident", _GRAPH)
    assert s.role("mitigated") == "claimed" and s.with_role("done") == ("resolved",)
    assert s.initial == "open" and dict(s.transitions) == {"resolve": "resolved"}
    assert facets.to_toml([s]) and load(facets.to_toml([s])) == (s,)


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"status": {"values": {"open": "busy"}}}, "unknown status roles"),
        ({"status": {"values": {"open": "open"}, "initial": "nope"}}, "initial status"),
        (
            {
                "status": {
                    "values": {"open": "open", "x": "done"},
                    "transitions": {"go": "gone"},
                }
            },
            "not one of its statuses",
        ),
        (
            {"status": {"values": {"open": "open"}}},
            "progress aspect needs a status with role done",
        ),
        ({"edges": {"plan": "blocks"}}, "link kind"),
        ({"aspects": ["links", "progress"]}, "needs \\['status'\\]"),
        ({"fields": {"severity": "int"}}, "can't declare fields yet"),
        ({"aspects": ["links", "status", "enriched"]}, "can't also be \\['enriched'\\]"),
        ({"aspects": ["links", "status", "anchored"]}, "can't also be \\['anchored'\\]"),
        ({"disable": ["locate"]}, "can't disable \\['locate'\\]"),
    ],
)
def test_a_bad_graph_spec_is_refused(patch, match):
    with pytest.raises(FacetSpecError, match=match):
        parse("incident", {**_GRAPH, **patch})


def test_status_and_edges_need_their_aspects():
    with pytest.raises(FacetSpecError, match="needs the status aspect"):
        parse("t", {"plural": "ts", "status": {"values": {"a": "open"}}})
    with pytest.raises(FacetSpecError, match="needs the links aspect"):
        parse("t", {"plural": "ts", "edges": {"*": "informed_by"}})



@pytest.mark.parametrize("noun", ["status", "serve", "info", "ops", "facets", "store"])
def test_a_facet_cannot_take_a_top_level_command_name(noun):
    with pytest.raises(FacetSpecError, match="reserved by crib"):
        check(_with(parse(noun, {"plural": noun + "x"})))


def test_every_top_level_command_and_tool_prefix_is_reserved():
    """A facet's noun becomes a top-level command and its tools' prefix: every
    one crib already has must be reserved, or a declared facet crashes the CLI
    (conflicting subparser) or steals tools into its set (review fix 4)."""
    import argparse
    import json
    from pathlib import Path

    from crib import cli

    builtin = {s.noun for s in facets.BUILTINS}
    sp = next(a for a in cli.build_parser()._actions
              if isinstance(a, argparse._SubParsersAction))
    commands = {c for c in sp.choices if c.isidentifier()}
    golden = Path(__file__).parent / "goldens" / "tool_surface.json"
    prefixes = {n.split("_", 1)[0] for n in json.loads(golden.read_text())["mcp"]}
    missing = sorted((commands | prefixes) - builtin - facets.RESERVED_NAMES)
    assert missing == [], f"reserve these in crib.facets.RESERVED_NAMES: {missing}"



def test_a_graph_facet_disables_its_own_verbs():
    """disable is checked against the verbs a graph facet really has (graph
    verbs and transitions), not the core ones (review fix 6)."""
    s = parse("incident", {**_GRAPH, "disable": ["review", "review_apply", "resolve"]})
    assert s.disabled == {"review", "review_apply", "resolve"}
    from crib import verbs

    names = {t.verb for t in verbs.verbs_of(s)}
    assert not names & {"review", "review_apply", "resolve"}
    assert {"next", "status", "add"} <= names


@pytest.mark.parametrize(
    ("aspects", "match"),
    [
        (["links"], "together with 'status'"),
        (["status"], "together with 'links'"),
        (["ordered"], "belong to a graph facet"),
        (["sources"], "belong to a graph facet"),
    ],
)
def test_half_a_graph_facet_is_refused(aspects, match):
    raw = {"plural": "ts", "aspects": aspects}
    if "status" in aspects:
        raw["status"] = {"values": {"open": "open"}}
    with pytest.raises(FacetSpecError, match=match):
        parse("t", raw)



def test_a_graph_facet_without_edges_gets_defaults_for_its_own_links():
    """Without an [edges] table a progress facet's own links are follows and a
    decision-like one's rests_on; other targets as crib's own (review fix 7)."""
    st = {"values": {"todo": "open", "done": "done"}}
    task = parse("task", {"plural": "tasks", "aspects": ["links", "status", "progress"],
                          "status": st})
    assert task.edges["task"] == "follows" and task.edges["*"] == "informed_by"
    rfc = parse("rfc", {"plural": "rfcs", "aspects": ["links", "status"], "status": st})
    assert rfc.edges["rfc"] == "rests_on" and rfc.edges["design"] == "rests_on"
    told = parse("rfc", {"plural": "rfcs", "aspects": ["links", "status"], "status": st,
                         "edges": {"*": "informed_by"}})
    assert dict(told.edges) == {"*": "informed_by"}  # a written table is as written
