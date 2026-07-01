"""End-to-end core loop on the dependency-free path (HashEmbedder + InMemoryStore).

Exercises the keystone invariants from DESIGN §4: store -> index -> lookup, the
hash gate (no-op on unchanged content), the version ring, and append/edit/restore.
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
    paths = Paths.resolve().ensure()
    config = Config()  # defaults: hash embedder, in-memory-ish
    return Crib(paths, config, InMemoryStore())


def run(coro):
    return asyncio.run(coro)


def test_store_then_lookup(crib):
    run(crib.store_note("The capital of France is Paris.", title="France",
                        project="p"))
    run(crib.store_note("Python uses indentation for blocks.", title="Python",
                        project="p"))
    hits = crib.lookup("france capital", project="p")
    assert hits
    assert hits[0].relpath.startswith("france")


def test_hash_gate_noop_on_unchanged(crib):
    out = run(crib.store_note("alpha beta gamma", title="t", project="p"))
    rel = out["relpath"]
    # Reindexing identical content must change nothing.
    res = run(crib.reindex(rel, project="p"))
    assert res["changed"] == 0


def test_metadata_drift_self_heals_without_reembed(crib):
    """A metadata-schema field missing on a content-unchanged chunk is refreshed
    on reindex via a cheap set_meta (no re-embed) — the gate is content-only, so
    this drift would otherwise be silently skipped."""
    out = run(crib.store_note("alpha beta gamma", title="t", project="p"))
    rel = out["relpath"]
    # Simulate an older-schema chunk: strip section_hash from stored metadata.
    metas = crib.store.get_meta({"project": "p", "relpath": rel})
    stripped = {i: {k: v for k, v in m.items() if k != "section_hash"}
                for i, m in metas.items()}
    crib.store.set_meta(stripped)
    assert all(not m.get("section_hash")
               for m in crib.store.get_meta({"project": "p", "relpath": rel}).values())
    # Reindex identical content: detects the missing field and refreshes metadata.
    res = run(crib.reindex(rel, project="p"))
    assert res["changed"] == 1
    healed = crib.store.get_meta({"project": "p", "relpath": rel})
    assert all(m.get("section_hash") for m in healed.values())


def test_append_and_version_ring(crib):
    out = run(crib.store_note("first body", title="note", project="p"))
    rel = out["relpath"]
    run(crib.append_note(rel, "second chunk", heading="More", project="p"))
    versions = crib.list_versions(rel, project="p")
    assert len(versions) == 1  # the pre-append content was stashed
    text = crib.read_note(rel, project="p")
    assert "second chunk" in text and "## More" in text


def test_edit_then_restore(crib):
    out = run(crib.store_note("original content here", title="doc", project="p"))
    rel = out["relpath"]
    run(crib.edit_note(rel, "completely different content", project="p"))
    assert "different" in crib.read_note(rel, project="p")

    versions = crib.list_versions(rel, project="p")
    assert versions
    run(crib.restore(rel, versions[-1]["version"], project="p"))
    assert "original content" in crib.read_note(rel, project="p")


def test_stable_id_assigned_and_persisted(crib):
    out = run(crib.store_note("content", title="x", project="p"))
    text = crib.read_note(out["relpath"], project="p")
    assert "id:" in text
