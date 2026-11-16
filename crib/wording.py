"""The wording of crib's own graph facets (crib/wording.toml): each verb's MCP
tool description and its CLI help, keyed by facet and canonical verb.

Data, not code, and in one place: tests/test_tool_surface_snapshot.py holds
every text to the byte. A declared facet has none of its own here; it gets the
templates' general wording (crib.verbs, crib.cli)."""

from __future__ import annotations

import functools
import tomllib
from importlib import resources
from typing import Any


@functools.cache
def _table() -> dict[str, Any]:
    return tomllib.loads(resources.files(__package__).joinpath("wording.toml").read_text())


def mcp(noun: str, verb: str) -> str | None:
    """`noun`'s MCP description for `verb`, or None (none of its own)."""
    return _table().get(noun, {}).get(verb, {}).get("mcp")


def cli(noun: str, key: str) -> str | None:
    """`noun`'s CLI help for `key`: a verb (`add`) or a verb's argument
    (`add.body`), or None."""
    verb, _, arg = key.partition(".")
    return _table().get(noun, {}).get(verb, {}).get(f"cli_{arg}" if arg else "cli")
