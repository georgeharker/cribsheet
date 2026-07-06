"""`import` copies EXPLICIT files into memory as crib-owned notes — manual only.
(Repo `.crib` docs are indexed in-situ instead; see test_insitu_docs.py.)"""

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


def _files(tmp_path):
    d = tmp_path / "src"
    d.mkdir(parents=True)
    (d / "arch.md").write_text("# Arch\nThe system uses a hash gate.")
    (d / "README.md").write_text("# myrepo\nTop level readme.")
    return d


def test_import_copies_and_stamps_provenance(crib, tmp_path):
    d = _files(tmp_path)
    out = asyncio.run(crib.import_files(
        [str(d / "arch.md"), str(d / "README.md")], project="notes"))
    assert out["imported"] == 2 and out["project"] == "notes"

    text = crib.read_note("imported/arch.md", project="notes")
    assert "source: imported" in text
    assert "hash gate" in text
    # It is a COPY under the crib tree (crib-owned), not source-anchored.
    assert crib.abspath("notes", "imported/arch.md") == \
        crib.notes_dir("notes") / "imported/arch.md"

    hits = crib.lookup("hash gate", project="notes")
    assert hits and "arch" in hits[0].relpath


def test_reimport_preserves_note_id(crib, tmp_path):
    d = _files(tmp_path)
    asyncio.run(crib.import_files([str(d / "README.md")], project="notes"))
    id1 = _id_of(crib, "imported/README.md")

    (d / "README.md").write_text("# myrepo\nUpdated readme text.")
    asyncio.run(crib.import_files([str(d / "README.md")], project="notes"))
    id2 = _id_of(crib, "imported/README.md")

    assert id1 == id2
    assert "Updated readme" in crib.read_note("imported/README.md", "notes")


def test_import_pins_first_import_date(crib, tmp_path):
    from crib import notes
    d = _files(tmp_path)
    asyncio.run(crib.import_files([str(d / "README.md")], project="notes"))
    rp = "imported/README.md"
    note = notes.load(crib.abspath("notes", rp))
    note.frontmatter["imported"] = "2020-01-01"
    notes.save_atomic(note)

    (d / "README.md").write_text("# myrepo\nEdited.")
    asyncio.run(crib.import_files([str(d / "README.md")], project="notes"))
    fm = notes.load(crib.abspath("notes", rp)).frontmatter
    assert fm["imported"] == "2020-01-01"


def test_import_id_is_deterministic_across_machines(tmp_path, monkeypatch):
    def fresh(slot):
        monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / slot / "config"))
        monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / slot / "data"))
        monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / slot / "index"))
        return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())

    d = _files(tmp_path)
    a, b = fresh("a"), fresh("b")
    asyncio.run(a.import_files([str(d / "arch.md")], project="notes"))
    asyncio.run(b.import_files([str(d / "arch.md")], project="notes"))
    assert _id_of(a, "imported/arch.md") == _id_of(b, "imported/arch.md")


def test_import_rejects_non_file(crib, tmp_path):
    with pytest.raises(ValueError, match="not a file"):
        asyncio.run(crib.import_files([str(tmp_path / "nope.md")], project="notes"))


def _id_of(crib, relpath):
    from crib import notes
    return notes.load(crib.abspath("notes", relpath)).id
