"""[generate] mode: "on" generates anywhere, "explicit" only for a verb someone
called, "off" never. Unset, it is "explicit" unless generation is configured, so
a daemon nobody set up never drives the Claude CLI on its own."""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.config import Config, GenerateConfig
from crib.errors import CribUserError
from crib.paths import Paths
from crib.store import InMemoryStore


def test_the_default_mode_depends_on_whether_generation_is_configured():
    assert GenerateConfig().resolved_mode() == "explicit"  # Claude CLI fallback
    assert GenerateConfig(config="~/.config/zsh-ai/models.toml").resolved_mode() == "on"
    assert GenerateConfig(adapter="openai-compatible").resolved_mode() == "on"
    assert GenerateConfig(mode="off", config="x").resolved_mode() == "off"
    with pytest.raises(ValueError, match="mode must be"):
        GenerateConfig(mode="sometimes").resolved_mode()


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


@pytest.mark.parametrize(
    "mode, background, verb",
    [("on", True, True), ("explicit", False, True), ("off", False, False)],
)
def test_who_may_generate(crib, mode, background, verb):
    crib.config.generate.mode = mode
    assert crib._may_generate(background=True) is background
    assert crib._may_generate(background=False) is verb
    assert crib._background_describe("defer") == ("defer" if background else "none")


def test_off_refuses_the_generation_verbs(crib):
    crib.config.generate.mode = "off"
    with pytest.raises(CribUserError, match='mode is "off"'):
        asyncio.run(crib.distill("x.md", project="p"))
    with pytest.raises(CribUserError, match='mode is "off"'):
        asyncio.run(crib.elaborate("keywords", project="p"))


def test_explicit_writes_structure_and_leaves_descriptions_pending(crib, monkeypatch):
    """A code index nobody asked for (the watcher, a query finding code changed)
    saves the symbols and blanks a changed description instead of calling the
    LLM; status counts what waits."""
    called: list[str] = []
    monkeypatch.setattr(
        "crib.codeindex.describe_file", lambda *a, **k: called.append("describe") or {}
    )
    entries = [
        {
            "name": "f",
            "kind": "function",
            "lang": "python",
            "file": "m.py",
            "line": 1,
            "content_hash": "h1",
            "signature": "def f():",
            "container": [],
            "calls": [],
            "called_by": [],
            "references": [],
            "name_terms": ["f"],
        }
    ]
    monkeypatch.setattr("crib.codeindex.extract_file", lambda *a, **k: list(entries))
    root = crib.paths.data_dir / "src"
    root.mkdir(parents=True)
    (root / "m.py").write_text("def f():\n    pass\n")
    crib._index_code_file_tracked(
        root, "m.py", "p", True, None, crib._background_describe("inline")
    )
    assert called == []
    recs = crib.code.records("p")
    assert recs and recs[0]["description"] == ""
    gen = crib.status()["generation"]
    assert gen["mode"] == "explicit" and gen["pending_descriptions"] == {"p": 1}
