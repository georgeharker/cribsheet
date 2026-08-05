"""Predictable relpaths, created flags, and move/reproject (feedback fixes)."""

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


def test_relpath_is_predictable_then_collision_suffixed(crib):
    a = run(crib.store_note("body one", title="Setup Notes", project="p"))
    assert a["relpath"] == "setup-notes.md"             # no random tail
    b = run(crib.store_note("body two", title="Setup Notes", project="p"))
    assert b["relpath"] == "setup-notes-2.md"           # suffix only on collision


def test_store_reports_created_for_new_project(crib):
    first = run(crib.store_note("x", title="t", project="fresh"))
    assert first["created"] is True
    second = run(crib.store_note("y", title="u", project="fresh"))
    assert second["created"] is False                   # project already existed


def test_move_across_projects_preserves_id(crib):
    out = run(crib.store_note("movable body", title="Movable", project="src"))
    from crib import notes
    id_before = notes.load(crib.abspath("src", out["relpath"])).id

    res = run(crib.move_note(out["relpath"], to_project="dst", project="src"))
    assert res["to"]["project"] == "dst" and res["created"] is True
    assert not crib.abspath("src", out["relpath"]).exists()      # source gone
    moved = crib.abspath("dst", out["relpath"])
    assert moved.exists()
    assert notes.load(moved).id == id_before                     # identity preserved
    # searchable in the new project, gone from the old
    assert any(h.relpath == out["relpath"] for h in crib.lookup("movable", project="dst"))
    assert not crib.lookup("movable", project="src")


def test_move_rejects_clobber_and_noop(crib):
    a = run(crib.store_note("aaa", title="A", project="p"))
    b = run(crib.store_note("bbb", title="B", project="p"))
    with pytest.raises(ValueError):                     # same src/dst
        run(crib.move_note(a["relpath"], project="p"))
    with pytest.raises(ValueError):                     # destination exists
        run(crib.move_note(a["relpath"], to_relpath=b["relpath"], project="p"))


def test_move_onto_itself_is_refused_by_RESOLVED_path(crib):
    # `a.md` and `./a.md` are the same file spelled two ways — comparing the
    # (project, relpath) STRINGS let the second through, and the move then wrote
    # the destination and unlinked it: the note it was asked to preserve, deleted.
    a = run(crib.store_note("aaa", title="A", project="p"))
    with pytest.raises(ValueError, match="same"):
        run(crib.move_note(a["relpath"], to_relpath="./" + a["relpath"], project="p"))
    assert crib.abspath("p", a["relpath"]).exists()      # still there


def test_reconcile_reports_duplicate_ids_from_an_interrupted_move(crib):
    # a crash between "write destination" and "unlink source" leaves two notes with
    # one id — and one version ring between them. The sweep must SAY so.
    a = run(crib.store_note("aaa", title="A", project="p"))
    raw = crib.read_note(a["relpath"], project="p")
    (crib.notes_dir("p") / "copy.md").write_text(raw)     # the half-finished move
    out = run(crib.reindex(project="p"))
    dupes = out["duplicate_ids"]
    assert len(dupes) == 1 and set(dupes[0]["relpaths"]) == {a["relpath"], "copy.md"}


def test_an_id_less_note_still_reaches_the_version_ring(crib):
    # `forget` advertises the delete as recoverable; a note with no `id:` used to
    # skip the ring entirely, so the promise was a lie for exactly those notes.
    (crib.notes_dir("p") / "raw.md").write_text("no frontmatter at all, just prose\n")
    out = run(crib.forget("raw.md", project="p"))
    rid = out["recoverable_id"]
    assert rid
    entries = crib.versions.list(rid)
    assert entries and "just prose" in crib.versions.read(rid, entries[0].name)


def test_a_read_for_a_typod_project_does_not_create_it(crib):
    run(crib.store_note("aaa", title="A", project="real"))
    crib.locate("a.md", project="typo")       # a resolve-only path…
    crib.abspath("typo", "a.md")
    with pytest.raises(OSError):              # …and a read that simply isn't there
        crib.read_note("a.md", project="typo")
    assert crib.projects() == ["real"]        # no phantom namespace planted


def test_similar_field_present_and_excludes_self(crib):
    res = run(crib.store_note("alpha beta gamma", title="First", project="p"))
    # the just-written note must never appear in its own similar list
    assert all(s["relpath"] != res["relpath"] for s in res["similar"])
