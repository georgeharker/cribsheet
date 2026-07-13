"""Auto keyword_index refresh after note writes — debounced per note, daemon-only,
task references held so the background refresh can't be GC'd mid-flight."""

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
    cfg = Config()
    cfg.retrieve.keyword_labels = ["keyword_index"]
    return Crib(Paths.resolve().ensure(), cfg, InMemoryStore())


def _arm(crib, monkeypatch, calls):
    async def fake_enrich(relpath=None, project=None, **kw):
        calls.append((project, relpath))
        return {}

    from types import SimpleNamespace

    monkeypatch.setattr(crib, "enrich", fake_enrich)
    monkeypatch.setattr(Crib, "_KW_SETTLE_S", 0.05)
    # daemon marker: queue present ⇒ schedule (stub .stop() for stop_watchers)
    crib._describe_q = SimpleNamespace(stop=lambda: None)


def test_write_burst_coalesces_to_one_refresh(crib, monkeypatch):
    calls: list = []

    async def body():
        _arm(crib, monkeypatch, calls)
        for _ in range(4):               # store → append → edit → edit, same note
            crib._schedule_keyword_refresh("p", "n.md")
            await asyncio.sleep(0.01)    # inside the settle window each time
        crib._schedule_keyword_refresh("p", "other.md")   # distinct note = own timer
        await asyncio.sleep(0.2)

    asyncio.run(body())
    assert sorted(calls) == [("p", "n.md"), ("p", "other.md")]
    assert crib._kw_timers == {} and crib._bg_tasks == set()   # nothing left behind


def test_enrich_one_call_covers_both_facets(crib, monkeypatch):
    """The consolidation: ONE bulk generation per note emits keyword AND summary
    facet terms (each written to its own store, per-facet hash gates independent) —
    half the LLM calls of elaborate + summarize back to back."""
    from crib.section_index import SectionIndex

    crib.config.retrieve.keyword_labels = ["keywords"]
    crib.config.retrieve.summary_labels = ["summary"]
    gen_calls = {"n": 0}

    async def fake_struct(cfg, system, user, schema, **kw):
        gen_calls["n"] += 1
        props = schema["properties"]["sections"]["items"]["properties"]
        assert "keywords" in props and "summary" in props   # one schema, both facets
        return {"sections": [{"heading": "", "keywords": ["alpha term"],
                              "summary": ["a paraphrase of the fact"]}]}

    monkeypatch.setattr("crib.generate.agenerate_structured", fake_struct)

    async def body():
        await crib.store_note("The deployment restarts on config change.",
                              title="deploy", project="p")
        out = await crib.enrich(project="p")
        assert out["written"] == {"keywords": 1, "summary": 1}
        assert gen_calls["n"] == 1                # ONE call, both facets

        kw = SectionIndex(crib.paths.project_dir("p"), "keyword_index")
        sm = SectionIndex(crib.paths.project_dir("p"), "summary_index")
        shs = {m.get("section_hash") or m.get("content_hash")
               for _d, m in crib.store.get_docs({"project": "p"}).values()}
        assert all(kw.has("keywords", sh) for sh in shs)
        assert all(sm.has("summary", sh) for sh in shs)

        out2 = await crib.enrich(project="p")     # both facets cached → no-op
        assert out2["written"] == {"keywords": 0, "summary": 0}
        assert out2["skipped"] >= 1 and gen_calls["n"] == 1

        # partial state heals without churning the complete facet
        for sh in shs:
            sm.path("summary", sh).unlink()
        out3 = await crib.enrich(project="p")
        assert out3["written"] == {"keywords": 0, "summary": 1}

    asyncio.run(body())


def test_no_refresh_outside_the_daemon(crib, monkeypatch):
    calls: list = []

    async def body():
        _arm(crib, monkeypatch, calls)
        crib._describe_q = None          # one-shot CLI: no queue, no loop to drain it
        crib._schedule_keyword_refresh("p", "n.md")
        await asyncio.sleep(0.2)

    asyncio.run(body())
    assert calls == [] and crib._kw_timers == {}


def test_startup_backlog_catches_up_dropped_refreshes(crib, monkeypatch):
    """A stop cancels pending settle timers; `_keyword_backlog` reconciles on the
    next start via elaborate's own content-hash gate (project-wide, per label)."""
    calls: list = []

    async def body():
        _arm(crib, monkeypatch, calls)
        await crib.store_note("a fact", title="n", project="p")   # project exists
        await crib._keyword_backlog()

    asyncio.run(body())
    assert ("p", None) in calls               # whole-project, hash-gated pass


def test_startup_backlog_noop_without_labels(crib, monkeypatch):
    calls: list = []

    async def body():
        _arm(crib, monkeypatch, calls)
        crib.config.retrieve.keyword_labels = []
        await crib.store_note("a fact", title="n", project="p")
        await crib._keyword_backlog()

    asyncio.run(body())
    assert calls == []


def test_edit_then_quit_heals_on_startup(crib, monkeypatch):
    """The hazard case: a note that ALREADY HAS keywords is edited, and the daemon
    quits before the settle timer fires. Nothing remembers the queue — the durable
    signal is that the keyword store is content-addressed by SECTION HASH: the edit
    gave the section a new hash with no entry, so the startup backlog's elaborate
    pass sees a cache miss and regenerates (unchanged sections still skip)."""
    from types import SimpleNamespace

    from crib.section_index import SectionIndex

    crib.config.retrieve.keyword_labels = ["keywords"]   # builtin prompt label
    crib.config.generate.bulk = False
    gen = {"n": 0}

    async def fake(cfg, system, user, purpose="elaborate", timeout=None):
        gen["n"] += 1
        return "deployment restart\nconfig rollout"

    monkeypatch.setattr("crib.generate.agenerate", fake)
    monkeypatch.setattr(Crib, "_KW_SETTLE_S", 0.05)
    crib._describe_q = SimpleNamespace(stop=lambda: None)
    store = SectionIndex(crib.paths.project_dir("p"))

    def _hashes():
        return {m.get("section_hash") or m.get("content_hash")
                for _d, m in crib.store.get_docs({"project": "p"}).values()}

    async def body():
        res = await crib.store_note("The deployment restarts on config change.",
                                    title="deploy", project="p")
        await asyncio.sleep(0.15)                  # write-path refresh fires
        assert all(store.has("keywords", sh) for sh in _hashes())
        old_hashes = _hashes()

        await crib.edit_note(res["relpath"], "Now it rolls out canaries instead.",
                             project="p")
        crib.stop_watchers()                       # quit BEFORE the settle fires
        n_at_quit = gen["n"]
        await asyncio.sleep(0.15)
        assert gen["n"] == n_at_quit               # nothing ran after the quit
        assert not all(store.has("keywords", sh) for sh in _hashes())   # stale on disk

        await crib._keyword_backlog()              # next start
        assert gen["n"] > n_at_quit                # regenerated the edited section…
        assert all(store.has("keywords", sh) for sh in _hashes())       # …and healed
        for sh in old_hashes - _hashes():          # …and GC'd the orphaned old hash
            assert not store.has("keywords", sh)

    asyncio.run(body())


def test_stop_watchers_cancels_pending_refresh(crib, monkeypatch):
    calls: list = []

    async def body():
        _arm(crib, monkeypatch, calls)
        crib._schedule_keyword_refresh("p", "n.md")
        crib.stop_watchers()             # daemon shutdown before the settle fires
        await asyncio.sleep(0.2)

    asyncio.run(body())
    assert calls == [] and crib._kw_timers == {}
