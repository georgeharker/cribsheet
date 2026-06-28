"""`import` ingests local docs declared in a code repo's `.crib` (DESIGN §6)."""

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
    paths = Paths.resolve().ensure()
    return Crib(paths, Config(), InMemoryStore())


def _repo(tmp_path):
    repo = tmp_path / "myrepo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "arch.md").write_text("# Arch\nThe system uses a hash gate.")
    (repo / "README.md").write_text("# myrepo\nTop level readme.")
    (repo / ".crib").write_text(
        "project: notes\nimport:\n  - docs/**/*.md\n  - README.md\n")
    return repo


def test_import_pulls_and_stamps_provenance(crib, tmp_path):
    repo = _repo(tmp_path)
    out = asyncio.run(crib.import_docs(cwd=repo))
    assert out["imported"] == 2
    assert out["project"] == "notes"

    text = crib.read_note("imported/myrepo/docs/arch.md", project="notes")
    assert "source: imported" in text
    assert "source_path: docs/arch.md" in text
    assert "hash gate" in text

    hits = crib.lookup("hash gate", project="notes")
    assert hits and "arch" in hits[0].relpath


def test_reimport_preserves_note_id(crib, tmp_path):
    repo = _repo(tmp_path)
    asyncio.run(crib.import_docs(cwd=repo))
    id1 = _id_of(crib, "imported/myrepo/README.md")

    (repo / "README.md").write_text("# myrepo\nUpdated readme text.")
    asyncio.run(crib.import_docs(cwd=repo))
    id2 = _id_of(crib, "imported/myrepo/README.md")

    assert id1 == id2  # identity survives re-import (version-ring continuity)
    assert "Updated readme" in crib.read_note("imported/myrepo/README.md", "notes")


def _id_of(crib, relpath):
    from crib import notes
    return notes.load(crib.abspath("notes", relpath)).id
