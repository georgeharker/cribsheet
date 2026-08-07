"""The pillar-store split: one parameterized NoteStore, four instances
(notes/design/plans/learnings), sibling dirs on disk, isolated indexing."""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.config import Config
from crib.notes import Note
from crib.notestore import NoteStore, StoreSpec
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


def _note(path, body):
    return Note(path=path, frontmatter={}, body=body)


# --- roots -----------------------------------------------------------------

def test_pillar_stores_resolve_sibling_dirs(crib):
    base = crib.paths.projects_dir / "p"
    assert crib.notestore.root("p") == base / "notes"
    assert crib.designstore.root("p") == base / "design"
    assert crib.planstore.root("p") == base / "plans"
    assert crib.learningstore.root("p") == base / "learnings"


def test_facet_write_lands_in_the_sibling_dir_with_store_metadata(crib):
    ds = crib.designstore
    run(ds.write("p", "single-writer.md",
                 _note(ds.abspath("p", "single-writer.md"), "# D\ndecision\n")))
    assert (crib.paths.projects_dir / "p" / "design" / "single-writer.md").exists()
    assert not (crib.paths.projects_dir / "p" / "notes" / "design").exists()
    metas = crib.store.get_meta({"project": "p", "relpath": "single-writer.md"})
    assert metas and all(m["store"] == "design" for m in metas.values())


# --- reserved prefixes -----------------------------------------------------

def test_reserved_prefixes_are_refused_with_the_facet_verb_named(crib):
    spec = StoreSpec("notes", "notes", reserved=("design/", "plans/"))
    ns = NoteStore(crib.paths, crib.store, crib.index, crib.versions,
                   crib.project_paths, spec)
    with pytest.raises(ValueError, match="design_read"):
        ns.abspath("p", "design/x.md")
    with pytest.raises(ValueError, match="own store"):
        ns.abspath("p", "plans/y.md")
    ns.abspath("p", "designed-for-x.md")   # only the dir prefix is reserved


# --- sweep isolation -------------------------------------------------------

def test_notes_full_reindex_never_drops_facet_chunks(crib):
    # The notes sweep's union walk sees "indexed but not on disk" — facet chunks
    # are not on the NOTES disk, so without pillar scoping it would drop them.
    run(crib.store_note("plain note body", title="n", project="p"))
    ds = crib.designstore
    run(ds.write("p", "d.md", _note(ds.abspath("p", "d.md"), "# D\nbody\n")))
    before = {cid for cid, m in crib.store.get_meta({"project": "p"}).items()
              if m.get("store") == "design"}
    assert before
    res = run(crib.notestore.reindex("p"))
    assert res["removed"] == 0
    after = {cid for cid, m in crib.store.get_meta({"project": "p"}).items()
             if m.get("store") == "design"}
    assert after == before


def test_facet_sweep_drops_only_its_own_orphans(crib):
    ds, ps = crib.designstore, crib.planstore
    run(ds.write("p", "d.md", _note(ds.abspath("p", "d.md"), "# D\nbody\n")))
    run(ps.write("p", "d.md", _note(ps.abspath("p", "d.md"), "# P\nitem\n")))
    (crib.paths.projects_dir / "p" / "design" / "d.md").unlink()
    run(ds.reindex("p"))
    metas = crib.store.get_meta({"project": "p"}).values()
    stores = {m.get("store") for m in metas}
    assert stores == {"plans"}     # design orphan gone; same-relpath plan intact


def test_retrieval_scoping_holds_before_and_after_the_schema_marker(crib):
    # Before the v3 sweep marker lands, _retrieve queries project-wide and
    # applies the absence rule in Python; after it, the where-clause itself is
    # store-scoped. Same answers either way — this pins both paths.
    run(crib.store_note("chroma holds the vectors", title="Plain", project="p"))
    ds = crib.designstore
    run(ds.write("p", "vec.md",
                 _note(ds.abspath("p", "vec.md"),
                       "# Vec\nchroma holds the vectors\n")))
    for stamped in (False, True):
        if stamped:
            crib._record_chunk_schema()
        note_hits = crib.lookup("chroma vectors", project="p")
        assert {h.store for h in note_hits} == {"notes"}, f"stamped={stamped}"
        design_hits = crib.lookup("chroma vectors", project="p", store="design")
        assert {h.store for h in design_hits} == {"design"}, f"stamped={stamped}"


def test_crib_reindex_fans_out_over_the_pillars(crib):
    run(crib.store_note("plain note body", title="n", project="p"))
    ds = crib.designstore
    run(ds.write("p", "d.md", _note(ds.abspath("p", "d.md"), "# D\nbody\n")))
    res = run(crib.reindex(project="p"))
    assert res["files"] >= 2       # the notes file AND the design file were swept
