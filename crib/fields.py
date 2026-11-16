"""A declared facet's own fields (crib.facets FacetSpec.params): typed values
set through its verbs, and filters over them.

A field is `int`, `str` or `date` (ISO `YYYY-MM-DD`). Setting one coerces the
value to its type, or refuses it naming the type. A filter is a plain value
(equality, as `tags` filters notes) or a `where` condition
`{field, op, value}`: int and date take `== != < <= > >=`, str takes
`== != re` (a regular expression, searched). An item without the field
matches no condition. Design: the facet registry decision's appendix on
declared fields.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .errors import CribUserError
from .facets import FacetSpec

OPS: Mapping[str, tuple[str, ...]] = {
    "int": ("==", "!=", "<", "<=", ">", ">="),
    "date": ("==", "!=", "<", "<=", ">", ">="),
    "str": ("==", "!=", "re"),
}

# `severity>=3`, `changeset_id==abc`, `title re ^Foo` (the CLI's --where).
_EXPR = re.compile(r"^\s*([A-Za-z_]\w*)\s*(==|!=|<=|>=|<|>|\s+re\s+)\s*(.*?)\s*$")

Cond = tuple[str, str, Any]  # (field, op, value), value already coerced


def _type(spec: FacetSpec, name: str) -> str:
    if name not in spec.params:
        known = ", ".join(sorted(spec.params)) or "none"
        raise CribUserError(f"{spec.noun} has no field {name!r} (its fields: {known})")
    return spec.params[name]


def coerce(spec: FacetSpec, name: str, value: Any) -> int | str:
    """`value` as field `name`'s type, or a refusal naming the type."""
    t = _type(spec, name)
    try:
        if t == "int":
            if isinstance(value, bool):
                raise ValueError
            if isinstance(value, int):
                return value
            return int(str(value).strip())
        if t == "date":
            if isinstance(value, datetime.datetime):
                return value.date().isoformat()
            if isinstance(value, datetime.date):
                return value.isoformat()
            return datetime.date.fromisoformat(str(value).strip()).isoformat()
        return str(value)
    except ValueError:
        what = "an ISO date (YYYY-MM-DD)" if t == "date" else f"an {t}"
        raise CribUserError(
            f"{spec.noun} field {name!r} takes {what}, not {value!r}"
        ) from None


def require(spec: FacetSpec, values: Mapping[str, Any] | None,
            have: Mapping[str, Any] | None = None) -> None:
    """Refuse a new item missing a required field (a declared field the spec
    doesn't mark optional): not given now, and not already on the item."""
    given = {k for k, v in (values or {}).items() if v is not None}
    there = {k for k, v in (have or {}).items() if v is not None}
    if missing := [n for n in spec.required if n not in given | there]:
        names = ", ".join(f"`{n}`" for n in missing)
        raise CribUserError(
            f"a {spec.noun} needs {names} (required fields; the facet's spec can "
            f"mark a field optional with `optional = [...]`)")


def setting(spec: FacetSpec, values: Mapping[str, Any] | None) -> dict[str, Any]:
    """The fields a write sets: the given ones (None = leave alone), coerced."""
    return {k: coerce(spec, k, v) for k, v in (values or {}).items() if v is not None}


def conditions(
    spec: FacetSpec,
    equal: Mapping[str, Any] | None = None,
    where: Iterable[Mapping[str, Any]] | None = None,
) -> list[Cond]:
    """Plain equality values plus `where` conditions, checked against the
    fields' types."""
    out: list[Cond] = [
        (k, "==", coerce(spec, k, v)) for k, v in (equal or {}).items() if v is not None
    ]
    for c in where or []:
        if (
            not isinstance(c, Mapping)
            or set(c) - {"field", "op", "value"}
            or not {"field", "op", "value"} <= set(c)
        ):
            raise CribUserError(
                f"a where condition is {{field, op, value}}, not {dict(c) if isinstance(c, Mapping) else c!r}"
            )
        name, op = str(c["field"]), str(c["op"]).strip()
        t = _type(spec, name)
        if op not in OPS[t]:
            raise CribUserError(
                f"{spec.noun} field {name!r} is {t}: op {op!r} isn't one of {list(OPS[t])}"
            )
        if op == "re":
            try:
                out.append((name, op, re.compile(str(c["value"]))))
            except re.error as e:
                raise CribUserError(
                    f"bad regular expression for {name!r}: {e}"
                ) from None
        else:
            out.append((name, op, coerce(spec, name, c["value"])))
    return out


def parse_expr(expr: str) -> dict[str, Any]:
    """A CLI `--where` expression as a condition: `severity>=3`, `title re ^Foo`."""
    m = _EXPR.match(expr)
    if not m:
        raise CribUserError(
            f"--where {expr!r}: write it as FIELD OP VALUE (ops == != < <= > >= re)"
        )
    return {"field": m.group(1), "op": m.group(2).strip(), "value": m.group(3)}


def _compare(op: str, have: Any, want: Any) -> bool:
    """`have op want` for two values of one type (both int, or both str)."""
    if op == "==":
        return have == want
    if op == "!=":
        return have != want
    if op == "<":
        return have < want
    if op == "<=":
        return have <= want
    if op == ">":
        return have > want
    return have >= want


def matches(conds: Iterable[Cond], facet: Mapping[str, Any]) -> bool:
    for name, op, want in conds:
        have = facet.get(name)
        if have is None:
            return False
        if op == "re":
            if not want.search(str(have)):
                return False
            continue
        if isinstance(want, int):
            try:
                ok = _compare(op, int(have), want)
            except (TypeError, ValueError):
                return False
        else:
            ok = _compare(op, str(have), str(want))
        if not ok:
            return False
    return True
