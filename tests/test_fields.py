"""A declared facet's own fields (crib.fields): coercion, filters, matching."""

from __future__ import annotations

import datetime
import re

import pytest

from crib.errors import CribUserError
from crib.facets import FacetSpecError, parse
from crib.fields import coerce, conditions, matches, parse_expr, setting

SPEC = parse(
    "ticket",
    {
        "plural": "tickets",
        "fields": {"changeset_id": "str", "severity": "int", "due": "date"},
    },
)


def test_declared_types_become_params_and_store_columns():
    assert dict(SPEC.params) == {
        "changeset_id": "str",
        "severity": "int",
        "due": "date",
    }
    assert dict(SPEC.fields) == {
        "changeset_id": "VARCHAR",
        "severity": "BIGINT",
        "due": "VARCHAR",
    }


@pytest.mark.parametrize("bad", [{"x": "float"}, {"x": "VARCHAR"}, {"x": "list"}])
def test_only_int_str_date(bad):
    with pytest.raises(FacetSpecError, match="allowed: \\['date', 'int', 'str'\\]"):
        parse("t", {"plural": "ts", "fields": bad})


@pytest.mark.parametrize(
    "name", ["relpath", "title", "where", "project", "symbol", "id"]
)
def test_a_field_cannot_take_a_verb_parameter_or_crib_frontmatter_name(name):
    with pytest.raises(FacetSpecError, match="reserved"):
        parse("t", {"plural": "ts", "fields": {name: "str"}})


def test_coerce():
    assert coerce(SPEC, "severity", "3") == 3
    assert coerce(SPEC, "severity", 4) == 4
    assert coerce(SPEC, "due", "2026-12-01") == "2026-12-01"
    assert coerce(SPEC, "due", datetime.date(2026, 12, 1)) == "2026-12-01"
    assert coerce(SPEC, "changeset_id", 17) == "17"
    with pytest.raises(CribUserError, match="takes an int, not 'high'"):
        coerce(SPEC, "severity", "high")
    with pytest.raises(CribUserError, match="takes an int"):
        coerce(SPEC, "severity", True)
    with pytest.raises(CribUserError, match="ISO date"):
        coerce(SPEC, "due", "next week")
    with pytest.raises(
        CribUserError, match="no field 'colour'.*changeset_id, due, severity"
    ):
        coerce(SPEC, "colour", "red")


def test_setting_drops_unset_and_coerces():
    assert setting(SPEC, {"severity": "2", "due": None}) == {"severity": 2}
    assert setting(SPEC, None) == {}


def test_conditions_check_ops_against_types():
    c = conditions(
        SPEC,
        {"changeset_id": "abc"},
        [
            {"field": "severity", "op": ">=", "value": "2"},
            {"field": "changeset_id", "op": "re", "value": "^a"},
        ],
    )
    assert c[0] == ("changeset_id", "==", "abc")
    assert c[1] == ("severity", ">=", 2)
    assert isinstance(c[2][2], re.Pattern)
    with pytest.raises(CribUserError, match="op '<' isn't one of"):
        conditions(SPEC, None, [{"field": "changeset_id", "op": "<", "value": "b"}])
    with pytest.raises(CribUserError, match="op 're' isn't one of"):
        conditions(SPEC, None, [{"field": "severity", "op": "re", "value": "1"}])
    with pytest.raises(CribUserError, match="field, op, value"):
        conditions(SPEC, None, [{"field": "severity", "value": 1}])
    with pytest.raises(CribUserError, match="bad regular expression"):
        conditions(SPEC, None, [{"field": "changeset_id", "op": "re", "value": "("}])


def test_parse_expr():
    assert parse_expr("severity>=3") == {"field": "severity", "op": ">=", "value": "3"}
    assert parse_expr("changeset_id == abc") == {
        "field": "changeset_id",
        "op": "==",
        "value": "abc",
    }
    assert parse_expr("changeset_id re ^ab+c$") == {
        "field": "changeset_id",
        "op": "re",
        "value": "^ab+c$",
    }
    with pytest.raises(CribUserError, match="FIELD OP VALUE"):
        parse_expr("severity is high")


def test_matches():
    item = {"changeset_id": "abc123", "severity": 3, "due": "2026-12-01"}

    def m(where, equal=None):
        return matches(conditions(SPEC, equal, where), item)

    assert m([], {"changeset_id": "abc123"})
    assert not m([], {"changeset_id": "abc"})
    assert m([{"field": "severity", "op": ">", "value": 2}])
    assert not m([{"field": "severity", "op": "<", "value": 3}])
    assert m([{"field": "severity", "op": "!=", "value": 4}])
    assert m([{"field": "due", "op": "<", "value": "2027-01-01"}])
    assert not m([{"field": "due", "op": ">=", "value": "2026-12-02"}])
    assert m([{"field": "changeset_id", "op": "re", "value": "c1"}])
    # stored as text (an InMemoryStore row) still compares as an int
    assert matches(
        conditions(SPEC, None, [{"field": "severity", "op": ">=", "value": 10}]),
        {"severity": "12"},
    )
    # an item without the field matches no condition, != included
    assert not matches(
        conditions(SPEC, None, [{"field": "severity", "op": "!=", "value": 1}]), {}
    )
