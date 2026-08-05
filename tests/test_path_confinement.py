"""Every relpath/project/version name is an LLM-supplied argument, so the joins
that turn them into on-disk paths are confined (Tier 0.1) — and `sources/…`
relpaths, whose bytes belong to a source repo, are read-only (Tier 0.2)."""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.config import Config
from crib.paths import Paths, check_project_name, check_relpath, confine
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def run(coro):
    return asyncio.run(coro)


# --- the helper itself ------------------------------------------------------

def test_confine_allows_nested_relpaths(tmp_path):
    assert confine(tmp_path, "a/b.md") == tmp_path / "a" / "b.md"
    assert confine(tmp_path, "id", "000001-ab.md") == tmp_path / "id" / "000001-ab.md"


@pytest.mark.parametrize("bad", ["../x.md", "a/../../x.md", "..", ""])
def test_confine_rejects_escapes(tmp_path, bad):
    with pytest.raises(ValueError):
        confine(tmp_path, bad)


def test_confine_rejects_absolute(tmp_path):
    # the sharp one: `base / "/etc/passwd"` silently discards the base
    with pytest.raises(ValueError, match="absolute"):
        confine(tmp_path, "/etc/passwd")


def test_confine_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    base = tmp_path / "base"
    base.mkdir()
    (base / "link").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        confine(base, "link/secret.md")


@pytest.mark.parametrize("bad", ["../evil", "a/b", "", ".", "..", "with space", "/abs"])
def test_check_project_name_rejects_paths(bad):
    with pytest.raises(ValueError, match="invalid project name"):
        check_project_name(bad)


def test_check_project_name_allows_ordinary_names():
    for ok in ("crib", "my-proj", "my_proj", "v1.2", "A9"):
        assert check_project_name(ok) == ok


def test_check_relpath_returns_input():
    assert check_relpath("notes/a.md") == "notes/a.md"


# --- the seams --------------------------------------------------------------

def test_read_and_locate_refuse_to_escape_the_notes_tree(crib, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("classified\n")
    for rel in ("../../secret.md", str(secret)):
        with pytest.raises(ValueError):
            crib.read_note(rel, project="p")
        with pytest.raises(ValueError):
            crib.locate(rel, project="p")


def test_forget_cannot_unlink_outside_the_notes_tree(crib, tmp_path):
    victim = tmp_path / "victim.md"
    victim.write_text("please keep me\n")
    rel = "../../../../victim.md"
    with pytest.raises(ValueError, match="escapes"):
        run(crib.forget(rel, project="p"))
    with pytest.raises(ValueError, match="absolute"):
        run(crib.forget(str(victim), project="p"))
    assert victim.read_text() == "please keep me\n"     # filesystem untouched


def test_write_verbs_refuse_a_traversing_relpath(crib, tmp_path):
    outside = tmp_path / "planted.md"
    for verb in (lambda: crib.edit_note("../../planted.md", "x", project="p"),
                 lambda: crib.append_note("../../planted.md", "x", project="p")):
        with pytest.raises(ValueError, match="escapes"):
            run(verb())
    assert not outside.exists()


def test_project_name_with_a_separator_is_refused(crib, tmp_path):
    with pytest.raises(ValueError, match="invalid project name"):
        run(crib.store_note("content", title="t", project="../escaped"))
    assert not (tmp_path / "data" / "escaped").exists()
    assert not (tmp_path / "escaped").exists()


def test_restore_refuses_a_traversing_version_name(crib, tmp_path):
    out = run(crib.store_note("first body", title="v", project="p"))
    rel = out["relpath"]
    run(crib.edit_note(rel, "second body", project="p"))   # so a ring entry exists
    with pytest.raises(ValueError, match="escapes"):
        run(crib.restore(rel, "../../../../etc/passwd", project="p"))
    assert crib.read_note(rel, project="p").strip().endswith("second body")


# --- 0.2: source-anchored docs are read-only ---------------------------------

def _repo(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".crib").write_text("project: proj\ndocs:\n  - README.md\n")
    (repo / "README.md").write_text("# MyRepo\n\nThe widget frobnicates gaskets.\n")
    return repo


def test_source_relpaths_are_not_writable_through_note_verbs(crib, tmp_path):
    repo = _repo(tmp_path)
    run(crib.index_docs_insitu(cwd=repo))
    rel = "sources/myrepo/README.md"
    before = (repo / "README.md").read_bytes()

    with pytest.raises(ValueError, match="indexed in place"):
        run(crib.edit_note(rel, "# Hijacked\n", project="proj"))
    with pytest.raises(ValueError, match="indexed in place"):
        run(crib.append_note(rel, "appended", project="proj"))
    with pytest.raises(ValueError, match="indexed in place"):
        run(crib.forget(rel, project="proj"))
    with pytest.raises(ValueError, match="indexed in place"):
        run(crib.move_note(rel, to_relpath="stolen.md", project="proj"))
    with pytest.raises(ValueError):          # no id in the repo's README to restore
        run(crib.restore(rel, "000001-abc.md", project="proj"))

    assert (repo / "README.md").read_bytes() == before     # byte-identical
    assert (repo / "README.md").exists()
    # and it's still indexed — the guard refuses the write, it doesn't unindex
    assert crib.read_note(rel, project="proj").startswith("# MyRepo")
