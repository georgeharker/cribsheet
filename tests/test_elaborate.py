"""LLM elaborations (§3.1): term parsing, the content-addressed store, the BM25
lift they feed, and the distill/elaborate verbs with a fake generator (no live
model — generation is injected)."""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.config import Config
from crib.section_index import SectionIndex, parse_terms
from crib.paths import Paths
from crib.retrieve import BM25, LexicalCache, tokenize
from crib.store import InMemoryStore


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    paths = Paths.resolve().ensure()
    return Crib(paths, Config(), InMemoryStore())


# --- parsing -----------------------------------------------------------------
def test_parse_terms_strips_noise_and_dedupes():
    text = "```\n- restart server\n1. Restart Server\n* index file\n\n\"lexical cache\"\n```"
    terms = parse_terms(text)
    assert "restart server" in terms
    assert "index file" in terms
    assert "lexical cache" in terms
    assert sum(t.lower() == "restart server" for t in terms) == 1   # case-insensitive dedupe


# --- store -------------------------------------------------------------------
def test_store_roundtrip_and_labels(tmp_path):
    store = SectionIndex(tmp_path)
    store.write("keywords", "h1", ["alpha", "beta"], relpath="n.md", heading="H")
    assert store.has("keywords", "h1")
    assert not store.has("keywords", "nope")
    assert store.read_terms("keywords", "h1") == ["alpha", "beta"]
    assert store.terms_for("h1", ["keywords", "missing"]) == ["alpha", "beta"]
    assert store.labels() == ["keywords"]


def test_store_toml_is_deterministic(tmp_path):
    a, b = SectionIndex(tmp_path / "a"), SectionIndex(tmp_path / "b")
    p1 = a.write("kw", "h", ["x", "y"], relpath="n.md", heading="H", model="m")
    p2 = b.write("kw", "h", ["x", "y"], relpath="n.md", heading="H", model="m")
    assert p1.read_text() == p2.read_text()   # byte-identical → merge-conflict-free


# --- BM25 consumption (the deterministic lift proof) -------------------------
def test_lexical_cache_folds_elaboration_terms():
    # neither body mentions "kubernetes"; only c1's elaboration does.
    docs = {
        "c1": ("the deployment restarts on config change",
               {"project": "p", "content_hash": "h1"}),
        "c2": ("an unrelated note about cats",
               {"project": "p", "content_hash": "h2"}),
    }

    class FakeStore:
        def get_docs(self, where):
            return docs

    def elab(project, ch, labels):
        return ["kubernetes orchestration"] if ch == "h1" else []

    lc = LexicalCache(FakeStore(), elab)

    ids, _, bm = lc.get("p", ())                      # no labels → body only
    assert max(bm.scores(tokenize("kubernetes"))) == 0.0

    ids, _, bm = lc.get("p", ("keywords",))           # label folded in
    scores = bm.scores(tokenize("kubernetes"))
    assert scores[ids.index("c1")] > 0.0
    assert scores[ids.index("c2")] == 0.0


def test_code_fence_comments_are_not_headings():
    """A `#` comment inside a ``` fence must not split a section — else config-heavy
    docs get bogus sections and both _split_sections and section_line_map keys
    diverge from reality."""
    from crib.chunk import _split_sections, section_line_map
    body = ("# Real\ntext\n```toml\n# not a heading\nk = 1\n```\nmore\n"
            "## Second\nbody\n")
    heads = [hp for hp, _ in _split_sections(body)]
    assert heads == [["Real"], ["Real", "Second"]]      # fence comment ignored
    assert list(section_line_map(body).keys()) == ["Real", "Real/Second"]


def test_section_hash_invariant_to_windowing():
    from crib.chunk import chunk_note
    # a long section split into multiple windows: all windows share one section_hash
    body = "# H\n\n" + " ".join(f"word{i}" for i in range(800))
    small = chunk_note("p", "n.md", "id", body, window_words=100, overlap=20)
    big = chunk_note("p", "n.md", "id", body, window_words=400, overlap=40)
    assert len({c.section_hash for c in small}) == 1        # one section
    assert len(small) > len(big)                            # different windowing
    assert small[0].section_hash == big[0].section_hash     # ...same section id


def test_downweight_scales_elaboration_contribution():
    # c1's only "kubernetes" match comes from its elaboration term (section-keyed)
    docs = {"c1": ("deployment restarts", {"project": "p", "section_hash": "s1"}),
            "c2": ("unrelated cats", {"project": "p", "section_hash": "s2"})}

    class FakeStore:
        def get_docs(self, where):
            return docs

    lc = LexicalCache(FakeStore(),
                      lambda pr, sh, labels: ["kubernetes"] if sh == "s1" else [])
    ids, _, bm_full = lc.get("p", ("kw",), 1.0)
    ids, _, bm_half = lc.get("p", ("kw",), 0.5)
    q = tokenize("kubernetes")
    full = bm_full.scores(q)[ids.index("c1")]
    half = bm_half.scores(q)[ids.index("c1")]
    assert full > half > 0.0                 # weight 0.5 scores lower but still hits


# --- elaborate verb (fake generator) ----------------------------------------
def test_elaborate_writes_store_and_skips_existing(crib, monkeypatch):
    run(crib.store_note("The deployment restarts on config change.",
                        title="deploy", project="p"))

    calls = {"n": 0}

    async def fake(cfg, system, user, purpose="elaborate", timeout=None):
        calls["n"] += 1
        return "kubernetes\norchestration\ncluster"

    monkeypatch.setattr("crib.generate.agenerate", fake)

    out = run(crib.elaborate("keywords", project="p"))
    assert out["written"] >= 1 and out["skipped"] == 0
    first_calls = calls["n"]

    store = SectionIndex(crib.paths.project_dir("p"))
    # every written chunk carries the generated terms
    assert any(store.read_terms("keywords", ch.split(".")[0])
               for ch in [p.name for p in (store.root / "keywords").glob("*.toml")])

    out2 = run(crib.elaborate("keywords", project="p"))   # content-addressed: skip
    assert out2["written"] == 0 and out2["skipped"] >= 1
    assert calls["n"] == first_calls                       # no new LLM calls


def test_elaborate_unknown_label_errors(crib):
    run(crib.store_note("x", title="t", project="p"))
    with pytest.raises(ValueError, match="unknown elaborate label"):
        run(crib.elaborate("no-such-label", project="p"))


# --- summary_index: dense alias vectors -------------------------------------
def test_summary_alias_cache_ranks_section_by_rephrasing():
    """A section whose summary paraphrases a query (zero shared tokens with the
    body) is surfaced via its alias vector — the dense-side proof."""
    from crib.embed import HashEmbedder
    from crib.retrieve import SummaryVectorCache
    docs = {"c1": ("body text one", {"project": "p", "section_hash": "s1"}),
            "c2": ("body text two", {"project": "p", "section_hash": "s2"})}

    class FakeStore:
        def get_docs(self, where):
            return docs

    emb = HashEmbedder(dim=64)

    def summaries(pr, sh, labels):
        return ["kubernetes orchestration platform"] if sh == "s1" else \
               ["feline domestic animals"]

    cache = SummaryVectorCache(FakeStore(), emb, summaries)
    qv = emb.embed_query(["kubernetes orchestration platform"])[0]
    ranked = cache.ranking("p", ("summary",), qv, topn=5)
    assert ranked and ranked[0] == "c1"        # s1's rep chunk ranks first


def test_summarize_writes_summary_index(crib, monkeypatch):
    run(crib.store_note("The deployment restarts on config change.",
                        title="deploy", project="p"))

    async def fake(cfg, system, user, purpose="summarize", timeout=None):
        return "a rephrasing\nanother framing\na gist"

    monkeypatch.setattr("crib.generate.agenerate", fake)
    out = run(crib.summarize("summary", project="p"))
    assert out["written"] >= 1 and out["errors"] == 0
    store = SectionIndex(crib.paths.project_dir("p"), "summary_index")
    assert store.labels() == ["summary"]


# --- distill verb (fake generator) ------------------------------------------
def test_distill_revises_and_thrash_guards(crib, monkeypatch):
    out = run(crib.store_note("verbose original body with fluff", title="n", project="p"))
    rel = out["relpath"]

    async def revise(cfg, system, user, purpose="distill", timeout=None):
        return "tight revised body"

    monkeypatch.setattr("crib.generate.agenerate", revise)
    r1 = run(crib.distill(rel, project="p"))
    assert r1["changed"] is True
    from crib import notes
    note = notes.load(crib.abspath("p", rel))
    assert note.body.strip() == "tight revised body"
    assert note.frontmatter["source"] == "distilled"

    async def unchanged(cfg, system, user, purpose="distill", timeout=None):
        return "tight revised body"    # same as current → thrash guard

    monkeypatch.setattr("crib.generate.agenerate", unchanged)
    r2 = run(crib.distill(rel, project="p"))
    assert r2["changed"] is False


# --- provider resolution from a providers/profiles TOML (like models.toml) ---
_GEN_TOML = (
    '[defaults]\ntemperature = 0.2\n\n'
    '[providers.qwen]\nadapter = "openai-compatible"\n'
    'endpoint = "http://localhost:11435/v1"\nmodel = "q"\n\n'
    '[providers.zen]\nadapter = "anthropic"\n'
    'endpoint = "https://zen/v1"\napi_key_env = "OPENCODE_API_KEY"\nmodel = "z"\n\n'
    '[profiles.local]\ndistill = "qwen"\nelaborate = "qwen"\n\n'
    '[profiles.cloud]\ndistill = "zen"\nelaborate = "zen"\n'
)


def test_resolve_provider_by_profile_and_purpose(tmp_path):
    from crib.config import GenerateConfig
    from crib.generate import resolve_provider
    toml = tmp_path / "gen.toml"
    toml.write_text(_GEN_TOML)

    p = resolve_provider(GenerateConfig(config=str(toml), profile="cloud"), "distill")
    assert p.adapter == "anthropic" and p.model == "z"
    assert resolve_provider(
        GenerateConfig(config=str(toml), profile="local"), "elaborate").model == "q"
    # explicit provider wins over the profile lookup
    assert resolve_provider(
        GenerateConfig(config=str(toml), profile="local", provider="zen"),
        "distill").model == "z"


def test_resolve_provider_inline_fallback():
    from crib.config import GenerateConfig
    from crib.generate import resolve_provider
    p = resolve_provider(
        GenerateConfig(adapter="openai-compatible", model="m", endpoint="http://x/v1"),
        "distill")
    assert p.adapter == "openai-compatible" and p.model == "m"


def test_resolve_provider_missing_selection_errors(tmp_path):
    from crib.config import GenerateConfig
    from crib.generate import GenerationError, resolve_provider
    toml = tmp_path / "gen.toml"
    toml.write_text(_GEN_TOML)
    # config file but no profile and no provider → nothing selects
    with pytest.raises(GenerationError, match="no generation provider"):
        resolve_provider(GenerateConfig(config=str(toml)), "distill")
