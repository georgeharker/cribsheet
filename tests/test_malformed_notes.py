"""One unreadable note must not take the project down with it (Tier 0.3): the
reconcile sweep skips it and says so, and a write can still repair it."""

from __future__ import annotations

import asyncio

import pytest

from crib import notes
from crib.app import Crib
from crib.config import Config
from crib.notes import NoteParseError
from crib.paths import Paths
from crib.store import InMemoryStore

BAD = """---
id: 01BADBADBADBADBADBADBADBAD
title: [unterminated
---

The frontmatter above is not YAML. Body mentions marmosets.
"""


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def run(coro):
    return asyncio.run(coro)


def test_parse_raises_note_parse_error_naming_the_file():
    with pytest.raises(NoteParseError) as ei:
        notes.parse(BAD, "notes/bad.md")
    assert "notes/bad.md" in str(ei.value)
    assert ei.value.cause is not None


def test_scan_id_recovers_the_id_from_unparseable_frontmatter():
    assert notes.scan_id(BAD) == "01BADBADBADBADBADBADBADBAD"
    assert notes.scan_id("no frontmatter here\n") is None
    assert notes.scan_id("---\ntitle: t\n---\n\nid: not-frontmatter\n") is None


def test_reconcile_indexes_siblings_and_reports_the_skip(crib):
    nd = crib.notes_dir("p")
    nd.mkdir(parents=True, exist_ok=True)
    (nd / "bad.md").write_text(BAD)
    (nd / "good-a.md").write_text("---\ntitle: a\n---\n\nAlpha about turbines.\n")
    (nd / "good-b.md").write_text("---\ntitle: b\n---\n\nBeta about gardening.\n")

    rec = run(crib.reconcile_all())

    assert rec["changed"] == 2                       # both good notes indexed
    assert [s["relpath"] for s in rec["skipped"]] == ["bad.md"]
    assert rec["skipped"][0]["project"] == "p"
    assert "malformed frontmatter" in rec["skipped"][0]["error"]
    assert crib.lookup("turbines", project="p")
    assert crib.lookup("gardening", project="p")


def test_note_edit_repairs_a_corrupt_note_and_keeps_the_old_bytes(crib):
    nd = crib.notes_dir("p")
    nd.mkdir(parents=True, exist_ok=True)
    (nd / "bad.md").write_text(BAD)
    assert run(crib.reindex(project="p"))["skipped"]      # unusable before

    run(crib.edit_note("bad.md", "---\ntitle: fixed\n---\n\nNow about okapis.\n",
                       project="p"))

    assert run(crib.reindex(project="p"))["skipped"] == []
    assert crib.lookup("now about okapis", project="p")
    # the corrupt bytes went to the ring under the id still legible in the header
    ring = crib.versions.list("01BADBADBADBADBADBADBADBAD")
    assert ring and "marmosets" in crib.versions.read(
        "01BADBADBADBADBADBADBADBAD", ring[0].name)


def test_forget_still_recovers_a_corrupt_note(crib):
    nd = crib.notes_dir("p")
    nd.mkdir(parents=True, exist_ok=True)
    (nd / "bad.md").write_text(BAD)

    res = run(crib.forget("bad.md", project="p"))

    assert res["recoverable_id"] == "01BADBADBADBADBADBADBADBAD"
    assert not (nd / "bad.md").exists()
    ring = crib.versions.list(res["recoverable_id"])
    assert ring and "marmosets" in crib.versions.read(
        res["recoverable_id"], ring[0].name)


def test_insitu_sweep_skips_an_unparseable_repo_doc(crib, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / ".crib").write_text("project: proj\ndocs:\n  - '*.md'\n")
    (repo / "README.md").write_text("# MyRepo\n\nThe widget frobnicates gaskets.\n")
    (repo / "CHANGELOG.md").write_text(BAD)          # a `---` header that isn't YAML

    res = run(crib.index_docs_insitu(cwd=repo))

    assert [s["relpath"] for s in res["skipped"]] == ["sources/myrepo/CHANGELOG.md"]
    assert res["changed"] == 1
    assert crib.lookup("frobnicates", project="proj")
