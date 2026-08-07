"""The pre-split → pillar-store layout migration: legacy `notes/design/`,
`notes/plans/`, `notes/code-learnings/` move to the sibling stores, citations
requalify, and the ordinary reindex sweep self-heals stragglers."""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.config import Config
from crib.designs import _body_hash
from crib.paths import Paths
from crib.store import InMemoryStore

ID_BASE = "01MIGRATEBASEAAAAAAAAAAAAA"
ID_LEAF = "01MIGRATELEAFAAAAAAAAAAAAA"
ID_ITEM = "01MIGRATEITEMAAAAAAAAAAAAA"


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def run(coro):
    return asyncio.run(coro)


def _legacy_layout(crib, proj="p"):
    """A realistic pre-split tree: two decisions (leaf depends on base and cites
    it by the OLD spelling), one plan item, one learning — all under notes/,
    indexed the way the pre-split code indexed them (as notes chunks)."""
    nd = crib.notestore.dir(proj)
    (nd / "design").mkdir()
    (nd / "plans").mkdir()
    (nd / "code-learnings").mkdir()
    base_body = "the ground\n"
    (nd / "design" / "base.md").write_text(
        f"---\nid: {ID_BASE}\ntitle: Base\ntype: design\nstatus: active\n"
        f"deps: []\n---\n{base_body}")
    # the citation records base's REAL section hash, as a live capture would —
    # requalifying the ref must leave it matching (no taint from migration)
    from crib.chunk import chunk_note
    base_hash = chunk_note(proj, "base.md", "", base_body)[0].section_hash
    (nd / "design" / "leaf.md").write_text(
        "---\n"
        f"id: {ID_LEAF}\ntitle: Leaf\ntype: design\nstatus: active\n"
        f"deps: [{ID_BASE}]\n"
        f"checked:\n  {ID_BASE}: {_body_hash(base_body)}\n"
        f"sources:\n  - ref: design/base.md\n    heading: null\n"
        f"    hash: {base_hash}\n"
        "---\non the ground\n")
    (nd / "plans" / "item.md").write_text(
        f"---\nid: {ID_ITEM}\ntitle: Item\ntype: plan\nstatus: todo\n"
        f"deps: [{ID_LEAF}]\nrank: m\n---\ndo it\n")
    (nd / "code-learnings" / "pkg.f.md").write_text(
        "---\ntitle: pkg.f\nkind: code-learning\nsymbol: pkg.f\n---\ninsight\n")
    # indexed as the pre-split code indexed them: notes chunks, legacy relpaths
    for rel in ("design/base.md", "design/leaf.md", "plans/item.md",
                "code-learnings/pkg.f.md"):
        run(crib.index.index_file(proj, nd, rel))


def test_migrate_moves_requalifies_and_reindexes(crib):
    _legacy_layout(crib)
    assert any(m.get("relpath") == "design/base.md"
               for m in crib.store.get_meta({"project": "p"}).values())

    out = run(crib.project_migrate(project="p"))
    assert out["changed"] and not out["skipped"]
    base = crib.paths.projects_dir / "p"
    assert (base / "design" / "base.md").exists()
    assert (base / "plans" / "item.md").exists()
    assert (base / "learnings" / "pkg.f.md").exists()
    assert not (base / "notes" / "design").exists()      # emptied dirs pruned

    # citations requalified, frontmatter-only
    leaf = (base / "design" / "leaf.md").read_text()
    assert "ref: design:base.md" in leaf and "design/base.md" not in leaf
    assert out["refs_rewritten"] == ["design/leaf.md"]

    # the sweep dropped every legacy-path chunk and embedded the pillars
    metas = crib.store.get_meta({"project": "p"}).values()
    assert not any("/" in (m.get("relpath") or "") for m in metas)
    assert {m.get("store") for m in metas} == {"design", "plans", "learnings"}

    # graph intact across the move: dep edges, statuses, and NO new taint —
    # bodies were never touched, so the clean set is identical
    check = crib.design_check(project="p")
    assert check["clean"], check
    assert crib.plan_list(project="p")["items"][0]["blocked"] is False


def test_migrate_is_idempotent(crib):
    _legacy_layout(crib)
    assert run(crib.project_migrate(project="p"))["changed"]
    again = run(crib.project_migrate(project="p"))
    assert not again["changed"] and not again["moved"] and not again["skipped"]


def test_migrate_skips_collisions_and_reports(crib):
    _legacy_layout(crib)
    ds = crib.designstore.dir("p")
    (ds / "base.md").write_text("---\ntitle: divergent\n---\nother content\n")
    out = run(crib.project_migrate(project="p"))
    assert len(out["skipped"]) == 1
    # both survive — never clobbered; the legacy one stays for a human
    assert (crib.notestore.root("p") / "design" / "base.md").exists()
    assert "other content" in (ds / "base.md").read_text()
    # re-running after the human resolves it finishes the move
    (crib.notestore.root("p") / "design" / "base.md").unlink()
    assert not run(crib.project_migrate(project="p"))["skipped"]


def test_full_reindex_self_heals_a_pulled_straggler(crib):
    _legacy_layout(crib)
    run(crib.project_migrate(project="p"))
    # a lagging machine pushed an old-layout file; a pull recreates notes/design/
    straggler = crib.notestore.root("p") / "design" / "late.md"
    straggler.parent.mkdir(parents=True)
    straggler.write_text("---\nid: 01MIGRATELATEAAAAAAAAAAAAA\ntitle: Late\n"
                         "type: design\nstatus: active\ndeps: []\n---\nlate\n")
    res = run(crib.reindex(project="p"))
    assert res["migrated"]["moved"] == ["design/late.md"]
    assert (crib.paths.projects_dir / "p" / "design" / "late.md").exists()
    assert not straggler.exists()
    assert any(h["relpath"] == "late.md"
               for h in crib.design_lookup("late", project="p"))
