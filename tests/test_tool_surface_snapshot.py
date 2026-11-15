"""The surface agents and people read, frozen: every MCP tool's name,
description, input schema and annotations, and every CLI verb's --help text.

The guard for generating verb families from facet specs (facet registry step 2
and on): a generated family must reproduce the hand-written one exactly, and
wording is the part that drifts silently. A deliberate change regenerates the
golden and shows in review:

    CRIB_UPDATE_SURFACE=1 pytest tests/test_tool_surface_snapshot.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from crib.app import Crib
from crib.cli import VERBS, build_parser
from crib.config import Config
from crib.paths import Paths
from crib.server import build_server
from crib.store import InMemoryStore

GOLDEN = Path(__file__).resolve().parent / "goldens" / "tool_surface.json"


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def _mcp_surface(crib: Crib) -> dict[str, Any]:
    tools = asyncio.run(build_server(crib).list_tools())
    out = {}
    for t in sorted(tools, key=lambda t: t.name):
        out[t.name] = {
            "description": t.description,
            "inputSchema": t.parameters,
            "annotations": t.annotations.model_dump(exclude_none=True)
            if t.annotations
            else None,
        }
    return out


def _subparsers(p: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            return dict(a.choices)
    return {}


def _cli_surface() -> dict[str, str | None]:
    """Each VERBS row's --help; None for a row with no CLI subcommand (an
    MCP-only verb such as `plan review`)."""
    out: dict[str, str | None] = {}
    for key in sorted(VERBS):
        parser: argparse.ArgumentParser | None = build_parser()
        for word in key.split(" "):
            parser = _subparsers(parser).get(word) if parser else None
        out[key] = parser.format_help() if parser else None
    return out


def test_tool_surface_matches_the_golden(crib, monkeypatch):
    # argparse wraps --help to the terminal width: pin it
    monkeypatch.setenv("COLUMNS", "100")
    surface = {"mcp": _mcp_surface(crib), "cli": _cli_surface()}
    if os.environ.get("CRIB_UPDATE_SURFACE"):
        GOLDEN.write_text(json.dumps(surface, indent=1, sort_keys=True) + "\n")
    golden = json.loads(GOLDEN.read_text())
    for face in ("mcp", "cli"):
        assert sorted(surface[face]) == sorted(golden[face]), (
            f"{face}: verbs added or removed"
        )
        for name in sorted(golden[face]):
            assert surface[face][name] == golden[face][name], (
                f"{face} {name!r} changed; if deliberate, regenerate with "
                "CRIB_UPDATE_SURFACE=1"
            )
