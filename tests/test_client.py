"""Regression tests for the CLI→daemon response unwrapping (`client._data`).

The bug this pins: FastMCP's typed `CallToolResult.data` reconstructs a
list-of-objects tool return (e.g. `lookup`'s `LookupHit`s) into empty,
field-less models — `[Root(), Root()]` — which serialize to `[{}, {}]` and
render as `Root()`. The daemon itself is fine; only the client-side typed
rebuild is lossy. `_data` must read the faithful `structured_content` instead,
unwrapping the `{"result": ...}` envelope FastMCP adds to non-object returns.
"""

from __future__ import annotations

from types import SimpleNamespace

from crib.client import _data


class _Root:
    """Stand-in for FastMCP's empty typed model — no fields, repr 'Root()'."""

    def __repr__(self) -> str:  # pragma: no cover - clarity only
        return "Root()"


def _res(*, data=None, structured_content=None, content=None):
    return SimpleNamespace(data=data, structured_content=structured_content,
                           content=content)


def test_lookup_prefers_structured_content_over_empty_data():
    # The real failure: .data is a list of empty Root() models, structured_content
    # holds the true hits under "result".
    hits = [{"project": "dotfiler", "relpath": "a.md", "score": 0.7},
            {"project": "dotfiler", "relpath": "b.md", "score": 0.6}]
    res = _res(data=[_Root(), _Root()], structured_content={"result": hits})
    assert _data(res) == hits


def test_scalar_result_envelope_is_unwrapped():
    # locate → a single string under "result".
    res = _res(data="/path/to/note.md", structured_content={"result": "/path/to/note.md"})
    assert _data(res) == "/path/to/note.md"


def test_list_of_scalars_unwrapped():
    res = _res(data=["cribsheet", "default"],
               structured_content={"result": ["cribsheet", "default"]})
    assert _data(res) == ["cribsheet", "default"]


def test_write_dict_passthrough_not_unwrapped():
    # A write result surfaces its dict directly (keys != {"result"}) — must pass
    # through untouched, not get mistaken for the result-envelope.
    d = {"project": "p", "relpath": "n.md", "created": True}
    res = _res(data=d, structured_content=d)
    assert _data(res) == d


def test_falls_back_to_data_when_no_structured_content():
    res = _res(data="plain text", structured_content=None)
    assert _data(res) == "plain text"


def test_falls_back_to_text_content_when_data_absent():
    blocks = [SimpleNamespace(text="hello "), SimpleNamespace(text="world")]
    res = _res(data=None, structured_content=None, content=blocks)
    assert _data(res) == "hello world"
