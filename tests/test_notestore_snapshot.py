"""Note-store characterization golden.

The code index has git-SHA goldens (scripts/snapshot_harness.py); notes don't — they
ARE the data, with no source to re-derive from. So the note-side gate is this: a FIXED
note scenario exercising the whole write/move/delete surface (store → append → edit →
move → forget) must produce a stable canonical store snapshot — the chunk decomposition
+ deterministic metadata (content_hash is text-derived; vectors and mtimes are
excluded). A NoteStore refactor that changes note behavior breaks this; a pure move
keeps it identical.
"""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def run(coro):
    return asyncio.run(coro)


_STABLE = ("relpath", "heading", "content_hash", "title", "source")


def _snapshot(crib: Crib, project: str) -> list[dict]:
    """Canonical store snapshot: stable chunk metadata only (deterministic — no
    vectors, no file mtimes), sorted so it's order-independent."""
    meta = crib.store.get_meta({"project": project})
    return sorted(({k: m.get(k) for k in _STABLE} for m in meta.values()),
                  key=lambda d: (d["relpath"] or "", d["heading"] or "",
                                 d["content_hash"] or ""))


async def _scenario(crib: Crib) -> None:
    await crib.store_note(
        "# Widgets\n\nThe frobnicator calibrates gaskets.\n\n## Usage\n\nCall `frob()`.",
        title="Widgets", project="p")
    await crib.store_note("# Turbines\n\nSteam drives the rotor.",
                          title="Turbines", project="p")
    await crib.store_note("# Gaskets\n\nSeals under pressure.",
                          title="Gaskets", project="p")
    await crib.append_note("widgets.md", "More on gaskets.", project="p")
    await crib.edit_note("turbines.md", "# Turbines\n\nRewritten: the rotor spins.",
                         project="p")
    await crib.move_note("gaskets.md", to_relpath="archive/gaskets.md", project="p")
    await crib.forget("turbines.md", project="p")


# The frozen canonical snapshot the scenario must reproduce (chunk decomposition +
# text-derived content_hashes). turbines is forgotten (gone); gaskets is moved to
# archive/. Regenerate deliberately only when note behavior changes ON PURPOSE.
GOLDEN = [
    {"relpath": "archive/gaskets.md", "heading": None,
     "content_hash": "f62cfbbf8b7bc6b674361cb0861a3c3b4ec4639d",
     "title": "Gaskets", "source": "manual"},
    {"relpath": "widgets.md", "heading": None,
     "content_hash": "3e38d46b72129a7cafcfaaae5156e246b5f9e724",
     "title": "Widgets", "source": "manual"},
    {"relpath": "widgets.md", "heading": None,
     "content_hash": "eef4ad16e494f6224a59b9f78053c6b137901cb9",
     "title": "Widgets", "source": "manual"},
]


def test_notestore_scenario_snapshot(crib):
    run(_scenario(crib))
    snap = _snapshot(crib, "p")
    assert _snapshot(crib, "p") == snap        # deterministic (pure read, re-snapshot)
    assert snap == GOLDEN                       # the frozen note-store characterization
