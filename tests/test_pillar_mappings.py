"""Locations phase 3: a built-in pillar mapped into its repo on its own.

`.crib` `facets: {design: {dir: decisions}}` keeps a project's decisions in the
repo while its notes stay where they are (store selection per pillar). A
pillar with items still under crib's data dir is PENDING until `crib project
adopt` moves them (ids and history kept); `release` moves them back.
"""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.config import Config, CribLink
from crib.notestore import PendingMove
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


def _repo(tmp_path, crib_text):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".crib").write_text(crib_text)
    return repo


def _decision(crib):
    a = run(
        crib.design_add("Keys are serials", "Widgets are keyed by serial.", project="p")
    )
    run(
        crib.design_edit(
            a["relpath"], "Widgets are keyed by serial, always.", project="p"
        )
    )
    return a


def test_a_mapped_pillar_with_items_is_pending_until_adopt(crib, tmp_path):
    a = _decision(crib)
    note = run(crib.store_note("notes stay put", "Note", "p"))
    repo = _repo(tmp_path, "project: p\nfacets:\n  design: {dir: decisions}\n")
    home = crib.project_paths("p").home_dir("design")
    assert (home / a["relpath"]).exists()

    run(crib._sync_mappings("p", CribLink.find(repo)))
    why = crib.designstore.pending_move("p")
    assert why and "crib project adopt" in why
    with pytest.raises(PendingMove, match="crib project adopt"):
        crib.design_read(a["relpath"], "p")
    assert run(crib.designstore.reindex("p"))["pending_move"] == why
    assert "notes stay put" in crib.read_note(note["relpath"], "p")  # notes unaffected
    found = crib.project_doctor("p")["findings"]
    assert [f["check"] for f in found if f["kind"] == "design"] == ["waiting to be moved"]

    res = run(crib.project_adopt(cwd=repo))
    assert res["mapped"]["design"]["notes_moved"] == 1
    assert res["mapped"]["design"]["versions_moved"] >= 1
    moved = repo / "decisions" / a["relpath"]
    assert moved.exists() and not (home / a["relpath"]).exists()
    assert crib.designstore.pending_move("p") is None
    d = crib.design_read(a["relpath"], "p")
    assert d["id"] == a["id"] and "always" in d["body"]
    assert crib.designstore.list_versions("p", a["relpath"])  # history came along
    assert (repo / "decisions" / ".versions" / ".gitignore").exists()
    assert (crib.project_paths("p").home_dir("notes") / note["relpath"]).exists()


def test_release_moves_a_mapped_pillar_back(crib, tmp_path):
    a = _decision(crib)
    repo = _repo(tmp_path, "project: p\nfacets:\n  design: {dir: decisions}\n")
    run(crib.project_adopt(cwd=repo))
    res = run(crib.project_release("p"))
    assert res["mapped"]["design"]["notes_moved"] == 1
    assert "remove their `facets:`" in res["message"]
    home = crib.project_paths("p").home_dir("design")
    assert (home / a["relpath"]).exists()
    assert not (repo / "decisions" / a["relpath"]).exists()
    assert crib.project_paths("p").mappings == {}
    assert crib.design_read(a["relpath"], "p")["id"] == a["id"]


def test_store_and_a_pillar_mapping_split_the_project_between_them(crib, tmp_path):
    a = _decision(crib)
    note = run(crib.store_note("travels with the store", "Note", "p"))
    repo = _repo(
        tmp_path, "project: p\nstore: crib-store\nfacets:\n  design: {dir: decisions}\n"
    )
    res = run(crib.project_adopt(cwd=repo))
    assert res["mapped"]["design"]["notes_moved"] == 1
    assert (repo / "decisions" / a["relpath"]).exists()  # its own mapping
    assert not (repo / "crib-store" / "design" / a["relpath"]).exists()  # not the store
    assert (repo / "crib-store" / "notes" / note["relpath"]).exists()
    assert crib.design_read(a["relpath"], "p")["id"] == a["id"]
    assert "travels with the store" in crib.read_note(note["relpath"], "p")


def test_adopt_refuses_to_merge_into_a_mapping_that_has_items(crib, tmp_path):
    _decision(crib)
    repo = _repo(tmp_path, "project: p\nfacets:\n  design: {dir: decisions}\n")
    (repo / "decisions").mkdir()
    (repo / "decisions" / "other.md").write_text("---\ntitle: Other\n---\nx\n")
    with pytest.raises(Exception, match="refusing to merge"):
        run(crib.project_adopt(cwd=repo))


def test_a_move_that_fails_part_way_moves_nothing(crib, tmp_path, monkeypatch):
    """Adopt moves all or nothing: an OSError on the second file (a read-only
    checkout, a full disk) puts the first back and refuses (review fix 1)."""
    import shutil

    a = _decision(crib)
    b = run(crib.design_add("Second", "Another decision.", project="p"))
    repo = _repo(tmp_path, "project: p\nfacets:\n  design: {dir: decisions}\n")
    real, calls = shutil.move, []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise PermissionError(13, "Permission denied", dst)
        return real(src, dst)

    monkeypatch.setattr(shutil, "move", flaky)
    with pytest.raises(Exception, match="nothing was moved"):
        run(crib.project_adopt(cwd=repo))
    monkeypatch.setattr(shutil, "move", real)
    home = crib.project_paths("p").home_dir("design")
    assert (home / a["relpath"]).exists() and (home / b["relpath"]).exists()
    assert not list((repo / "decisions").glob("*.md"))
    res = run(crib.project_adopt(cwd=repo))  # and it can simply be re-run
    assert res["mapped"]["design"]["notes_moved"] == 2


@pytest.mark.parametrize(
    "spec", ["{dir: decisions, read_only: true}", "{dir: decisions, ids: derived}"]
)
def test_a_builtin_pillar_maps_writable_with_stamped_ids(tmp_path, spec, capsys):
    repo = _repo(tmp_path, f"project: p\nfacets:\n  design: {spec}\n")
    link = CribLink.find(repo)
    assert link is not None and "design" not in link.facets
    assert "writable, with stamped ids" in capsys.readouterr().err
