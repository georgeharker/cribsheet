"""Crib's note methods route to the facet they're given (facet registry step 2a).

Notes stay the default, so every existing caller is unchanged; a `facet=` names
another facet's store. A built-in facet stands in here for the declared ones
that come later (tests/test_declared_facets.py exercises a real new facet).
"""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.config import Config
from crib.errors import CribUserError
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


def test_writes_reads_and_history_land_in_the_named_facets_store(crib):
    f = "learning"
    res = run(crib.store_note("first body", "Routing probe", "p", facet=f))
    rel = res["relpath"]
    path = crib.pillars["learnings"].root("p") / rel
    assert path.exists()
    assert not (crib.pillars["notes"].root("p") / rel).exists()
    assert crib.locate(rel, "p", facet=f) == str(path)
    assert "first body" in crib.read_note(rel, "p", facet=f)
    with pytest.raises((CribUserError, FileNotFoundError, OSError)):
        crib.read_note(rel, "p")  # not a note

    run(crib.append_note(rel, "more", "Extra", "p", facet=f))
    run(crib.edit_note(rel, "rewritten", "p", facet=f))
    assert "rewritten" in crib.read_note(rel, "p", facet=f)
    versions = crib.list_versions(rel, "p", facet=f)
    assert len(versions) >= 2
    run(crib.restore(rel, versions[-1]["version"], "p", facet=f))
    assert "first body" in crib.read_note(rel, "p", facet=f)

    moved = run(crib.move_note(rel, "q", "moved.md", "p", facet=f))
    assert moved["to"] == {"project": "q", "relpath": "moved.md"}
    assert (crib.pillars["learnings"].root("q") / "moved.md").exists()
    run(crib.forget("moved.md", "q", facet=f))
    assert not (crib.pillars["learnings"].root("q") / "moved.md").exists()


def test_import_and_reindex_use_the_facets_store(crib, tmp_path):
    src = tmp_path / "src.md"
    src.write_text("imported body\n")
    run(crib.import_files([str(src)], "p", facet="learning"))
    assert (crib.pillars["learnings"].root("p") / "imported" / "src.md").exists()
    r = run(crib.reindex(None, "p", facet="learning"))
    assert r["files"] >= 1


def test_unknown_facet_is_refused(crib):
    with pytest.raises(KeyError, match="no facet"):
        crib.read_note("x.md", "p", facet="routine")


# ── locations phase 2: `docs:` is the notes facet's prefixed mapping ──────────

from crib.config import CribLink  # noqa: E402


def _crib_file(tmp_path, text) -> CribLink:
    (tmp_path / ".crib").write_text(text)
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "guide.md").write_text("# Guide\n")
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "main.py").write_text("x = 1\n")
    link = CribLink.find(tmp_path)
    assert link is not None
    return link


def test_docs_is_the_notes_prefixed_mapping(tmp_path):
    link = _crib_file(
        tmp_path, 'project: p\ndocs:\n  - "README.md"\n  - "docs/**/*.md"\n'
    )
    m = link.facets["note"]
    assert (m.read_only, m.ids, m.prefix) == (
        True,
        "derived",
        f"sources/{tmp_path.name}/",
    )
    assert link.doc_patterns == ["README.md", "docs/**/*.md"]


def test_the_explicit_note_mapping_means_the_same_as_docs(tmp_path):
    link = _crib_file(
        tmp_path, "project: p\nfacets:\n  note: {dir: docs, glob: '**/*.md'}\n"
    )
    m = link.facets["note"]
    assert (m.read_only, m.ids, m.prefix) == (
        True,
        "derived",
        f"sources/{tmp_path.name}/",
    )
    assert link.doc_patterns == [
        "docs/**/*.md"
    ]  # repo-root-relative, as docs: globs are


def test_a_docs_mapping_owns_nothing(tmp_path):
    # its dir is the repo itself: owning it would hide every code file
    link = _crib_file(tmp_path, 'project: p\ndocs: ["**/*.md"]\n')
    assert not link.in_store(tmp_path / "src" / "main.py")
    assert not link.in_store(tmp_path / "docs" / "guide.md")


@pytest.mark.parametrize(
    "spec", ["{dir: docs, read_only: false}", "{dir: docs, ids: stamp}"]
)
def test_notes_map_only_in_the_docs_form(tmp_path, spec, capsys):
    link = _crib_file(tmp_path, f"project: p\nfacets:\n  note: {spec}\n")
    assert "note" not in link.facets
    assert "read-only with derived ids" in capsys.readouterr().err


def test_docs_and_an_explicit_note_mapping_together_warn(tmp_path, capsys):
    link = _crib_file(
        tmp_path, 'project: p\ndocs: ["README.md"]\nfacets:\n  note: {dir: docs}\n'
    )
    assert link.doc_patterns == ["docs/**/*.md"]  # the explicit one wins
    assert "`docs:` ignored" in capsys.readouterr().err


@pytest.mark.parametrize("d", [".", "./", "''"])
def test_a_mapping_cannot_claim_the_whole_repo(tmp_path, d, capsys):
    """`dir: .` would make every markdown file in the repo the facet's (stamped,
    and moved by release) and hide all code from the sweeps (review fix 5)."""
    link = _crib_file(tmp_path, f"project: p\nfacets:\n  design: {{dir: {d}}}\n")
    assert "design" not in link.facets
    err = capsys.readouterr().err
    assert "whole repo" in err or "empty path" in err
    assert not link.in_store(tmp_path / "src" / "main.py")


@pytest.mark.parametrize(
    "facets_yaml, dropped, why",
    [
        ("  design: {dir: d}\n  plan: {dir: d}\n", "plan", "overlaps facets.design"),
        ("  design: {dir: d}\n  plan: {dir: d/sub}\n", "plan", "overlaps facets.design"),
        ("  design: {dir: d/sub}\n  plan: {dir: d}\n", "plan", "overlaps facets.design"),
        ("  design: {dir: .git/x}\n", "design", "inside .git"),
    ],
)
def test_mappings_cannot_overlap(tmp_path, facets_yaml, dropped, why, capsys):
    link = _crib_file(tmp_path, "project: p\nfacets:\n" + facets_yaml)
    assert dropped not in link.facets
    assert why in capsys.readouterr().err
    kept = {"design", "plan"} - {dropped}
    if ".git" not in facets_yaml:
        assert kept <= set(link.facets)  # the earlier one stands


@pytest.mark.parametrize("d", ["crib-store", "crib-store/design", "."])
def test_a_mapping_cannot_overlap_the_store(tmp_path, d, capsys):
    link = _crib_file(
        tmp_path, f"project: p\nstore: crib-store\nfacets:\n  design: {{dir: '{d}'}}\n"
    )
    assert "design" not in link.facets
    assert link.store == "crib-store"  # the store stands
    err = capsys.readouterr().err
    assert "overlaps `store: crib-store`" in err or "whole repo" in err


def test_sibling_mappings_and_a_store_coexist(tmp_path, capsys):
    link = _crib_file(
        tmp_path,
        "project: p\nstore: crib-store\nfacets:\n  design: {dir: decisions}\n"
        "  plan: {dir: plans}\n",
    )
    assert set(link.facets) == {"design", "plan"}
    assert "ignoring" not in capsys.readouterr().err
