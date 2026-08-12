"""Recovery from an embedder change (robustness 2.10).

A full reindex is the one place crib may switch embedder — and switching WIPES the
shared vector collection, every project's chunks with it. The wipe must therefore
drive a FULL re-embed (all projects + their in-situ `sources/…` docs), and until a
project has been re-swept its lookups must say so (`index_rebuilding`) instead of
quietly returning a thin result set.
"""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.config import Config
from crib.embed import HashEmbedder
from crib.paths import Paths
from crib.sources import SRC_PREFIX
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def run(coro):
    return asyncio.run(coro)


def _flip_embedder(crib, dim: int) -> None:
    """A profile switch to a differently-sized model (bge-small → bge-large)."""
    emb = HashEmbedder(dim=dim)
    crib.embedder = emb
    crib.index.embedder = emb


def _insitu_chunks(crib, project) -> list[str]:
    """Indexed chunks of the project's in-situ (`sources/…`) docs — the ones the
    notes-tree sweep can't see, since their files live in the repo."""
    return [m["relpath"] for m in crib.store.get_meta({"project": project}).values()
            if m.get("relpath", "").startswith(SRC_PREFIX)]


def _repo_with_docs(tmp_path, project):
    repo = tmp_path / "myrepo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".crib").write_text(f"project: {project}\ndocs:\n  - README.md\n")
    (repo / "README.md").write_text("# MyRepo\n\nThe widget frobnicates gaskets.\n")
    return repo


def test_dim_change_re_embeds_every_project_and_marks_the_gap(crib, tmp_path):
    repo = _repo_with_docs(tmp_path, "p1")
    run(crib.store_note("alpha content about turbines", title="a", project="p1"))
    run(crib.store_note("beta content about gardening", title="b", project="p2"))
    run(crib.index_docs_insitu(cwd=repo))            # p1 also has an in-situ doc
    assert crib.lookup("turbines", project="p1")
    assert crib.lookup("gardening", project="p2")
    assert crib.lookup("frobnicates gaskets", project="p1")

    _flip_embedder(crib, 128)
    statuses: list[dict] = []
    orig = crib.notestore.reindex

    async def spy(project, relpath=None):            # capture status DURING the sweep
        statuses.append(crib.status())
        return await orig(project, relpath)

    crib.notestore.reindex = spy

    async def body():
        # the daemon's loop: a background recovery sweep can outlive the call
        crib._daemon_loop = asyncio.get_running_loop()
        res = await crib.reindex(project="p1")       # full reindex → dim change → wipe
        assert res["recreated"]

        # DURING: p1 is back (this reindex re-embedded it) but flagged incomplete;
        # p2's vectors went with the wipe and haven't been re-embedded yet, so
        # whatever it can still answer from (the lexical cache) says so too.
        assert crib._resweep_pending == {"p1", "p2"}
        assert not crib.store.get_meta({"project": "p2"})     # wiped, not yet swept
        assert not _insitu_chunks(crib, "p1")   # p1's notes are back; its docs aren't
        hits = crib.lookup("turbines", project="p1")
        assert hits and all(h.index_rebuilding for h in hits)
        assert all(h.index_rebuilding for h in crib.lookup("gardening", project="p2"))
        assert crib.status()["index_rebuilding"] == ["p1", "p2"]

        while crib._bg_tasks:                        # let the recovery sweep finish
            await asyncio.gather(*list(crib._bg_tasks))

    run(body())

    # AFTER: every project searchable again — including the in-situ doc, which the
    # per-project notes sweep never walks — and the marker is gone.
    assert not crib._resweep_pending
    assert crib.store.get_meta({"project": "p2"})     # re-embedded by the sweep
    assert _insitu_chunks(crib, "p1")                 # in-situ docs recovered too
    assert crib.lookup("gardening", project="p2")
    assert crib.lookup("frobnicates gaskets", project="p1")
    hits = crib.lookup("turbines", project="p1")
    assert hits and not any(h.index_rebuilding for h in hits)
    assert "index_rebuilding" not in crib.status()

    # …and the sweep announced WHY it was running while it ran
    assert any(s.get("reconcile_reason") == "embedder profile change"
               for s in statuses), statuses


def test_wipe_without_a_daemon_loop_warns_instead_of_stranding_a_task(
        crib, capsys):
    run(crib.store_note("alpha content about turbines", title="a", project="p1"))
    run(crib.store_note("beta content about gardening", title="b", project="p2"))
    _flip_embedder(crib, 128)
    run(crib.reindex(project="p1"))                  # in-process: no loop to outlive us
    assert not crib._bg_tasks                        # nothing scheduled to be GC'd
    assert crib._resweep_pending == {"p1", "p2"}     # …but the gap is still visible
    assert "crib project reconcile" in capsys.readouterr().err


def test_no_dim_change_no_recovery(crib):
    run(crib.store_note("alpha content about turbines", title="a", project="p1"))
    res = run(crib.reindex(project="p1"))
    assert "recreated" not in res and not crib._resweep_pending
    assert not crib.lookup("turbines", project="p1")[0].index_rebuilding


def test_a_dim_mismatch_raises_instead_of_scoring_a_truncated_vector():
    """`zip` silently truncates to the shorter vector, so a 256-dim query against
    384-dim stored vectors returned a plausible score computed over the first 256
    components — every ranking quietly degraded, nothing said so. Chroma refuses
    outright; the in-process stores have to say it themselves."""
    from crib.store import InMemoryStore as _S
    from crib.store import Record
    s = _S()
    s.upsert([Record("a", [0.1] * 384, "doc", {"project": "p"})])
    with pytest.raises(ValueError, match="dimension mismatch"):
        s.query([0.1] * 256, k=1)


def test_a_reindex_that_wipes_the_store_drops_the_derived_caches(crib):
    """The wipe takes EVERY project's chunks; the BM25/alias caches are built from
    the store, so they have to go with it (a warm daemon otherwise keeps answering
    BM25 from a corpus that no longer exists)."""
    run(crib.store_note("alpha widget config", title="a", project="p"))
    crib.lookup("widget", project="p")                     # warm the cache
    assert crib.index.lexical.get("p")[0]
    _flip_embedder(crib, dim=64)
    out = run(crib.reindex(project="p"))
    assert out.get("recreated")
    # p was re-swept by the reindex itself; the point is nothing stale survived
    assert all(len(v) == 64 for v in
               (r.embedding for r in crib.store._snapshot()))
