"""`crib generate setup` / `test` (crib.gensetup): detect, pick, show the diff,
write on confirmation, check."""

from __future__ import annotations

import tomllib
from types import SimpleNamespace

import pytest

from crib import gensetup
from crib.config import Config
from crib.gensetup import Candidate, detect, plan, run_setup, set_keys

ZSH_AI = """\
[providers.ollama-glm]
adapter = "openai-compatible"
endpoint = "https://ollama.com/v1"
api_key_env = "OLLAMA_API_KEY"
model = "glm-5.3-flash"
max_tokens = 4096

[providers.qwen-fim]
adapter = "openai-compatible"
endpoint = "http://localhost:11435/v1"
model = "q"

[providers.claude]
adapter = "claude_code"
model = "claude-sonnet-4-6"
"""


def _listing(endpoint, key):
    return {
        "https://ollama.com/v1": ["glm-5.3-flash", "kimi"],
        "http://localhost:11434/v1": ["llama3"],
    }.get(endpoint)


def test_detect_offers_what_the_machine_has_claude_last(tmp_path):
    z = tmp_path / "models.toml"
    z.write_text(ZSH_AI)
    found = detect(
        env={"OLLAMA_API_KEY": "k", "ANTHROPIC_API_KEY": "a"},
        list_models=_listing,
        which=lambda c: "/bin/claude",
        zsh_ai=z,
    )
    names = [c.name for c in found]
    assert names == [
        "ollama-cloud",
        "anthropic",
        "local-ollama",
        "ollama-glm",
        "claude",
    ]  # no qwen-fim, no 2nd "claude"
    by = {c.name: c for c in found}
    assert by["ollama-cloud"].models == ["glm-5.3-flash", "kimi"]
    assert by["ollama-cloud"].fields["structured"] == "tool"
    imported = by["ollama-glm"]  # from zsh-ai, quirks added
    assert imported.source == "zsh-ai" and imported.fields["structured"] == "tool"
    assert (
        imported.fields["max_tokens"] == 8192
        and imported.default_model == "glm-5.3-flash"
    )
    assert "Claude subscription" in by["claude"].label


def test_nothing_found_says_what_would_be_found():
    said: list[str] = []
    assert run_setup(None, [], ask=lambda _p: "", say=said.append) == 1
    assert "OLLAMA_API_KEY" in said[0]


def test_set_keys_edits_in_place_and_keeps_everything_else():
    text = '# mine\n[generate]\n# why\ntimeout = 5\nprofile = "old"\n\n[other]\nx = 1\n'
    out = set_keys(text, "generate", {"profile": "crib", "config": "~/m.toml"})
    assert out.startswith("# mine\n[generate]\n")
    assert 'profile = "crib"' in out and 'config = "~/m.toml"' in out
    assert "# why\ntimeout = 5" in out and "[other]\nx = 1" in out
    assert tomllib.loads(out)["generate"] == {
        "timeout": 5,
        "profile": "crib",
        "config": "~/m.toml",
    }
    assert "[generate]" in set_keys("", "generate", {"mode": "on"})


def _paths(tmp_path):
    return SimpleNamespace(
        config_dir=tmp_path / "config",
        config_file=tmp_path / "config" / "config.toml",
        index_dir=tmp_path / "index",
    )


def _ollama():
    return Candidate(
        "ollama-cloud",
        "Ollama Cloud",
        {
            "adapter": "openai-compatible",
            "endpoint": "https://ollama.com/v1",
            "api_key_env": "OLLAMA_API_KEY",
            "max_tokens": 8192,
            "structured": "tool",
        },
        ["glm-5.3-flash", "kimi"],
        "glm-5.3-flash",
    )


def test_setup_writes_both_files_after_confirming(tmp_path):
    paths = _paths(tmp_path)
    paths.config_dir.mkdir()
    paths.config_file.write_text("# my config\n[retrieve]\nrerank = true\n")
    answers = iter(["1", "2", "explicit", "y"])
    said: list[str] = []
    assert (
        run_setup(paths, [_ollama()], ask=lambda _p: next(answers), say=said.append)
        == 0
    )
    models = tomllib.loads((paths.config_dir / "models.toml").read_text())
    assert models["providers"]["ollama-cloud"]["model"] == "kimi"
    assert models["providers"]["ollama-cloud"]["structured"] == "tool"
    assert models["profiles"]["crib"] == {p: "ollama-cloud" for p in gensetup.PURPOSES}
    cfg_text = paths.config_file.read_text()
    assert cfg_text.startswith("# my config\n[retrieve]\nrerank = true\n")
    gen = Config.load(paths.config_file).generate
    assert gen.profile == "crib" and gen.resolved_mode() == "explicit"
    assert any("@@" in s for s in said)  # the diff was shown
    backups = list((paths.index_dir / "config-backups").iterdir())
    assert [b.name.split(".")[0] for b in backups] == ["config"]  # the one that existed


def test_declining_writes_nothing(tmp_path):
    paths = _paths(tmp_path)
    answers = iter(["1", "", "on", "n"])
    assert (
        run_setup(paths, [_ollama()], ask=lambda _p: next(answers), say=lambda _s: None)
        == 1
    )
    assert not (paths.config_dir / "models.toml").exists()
    assert not paths.config_file.exists()


def test_a_symlinked_config_is_written_through(tmp_path):
    paths = _paths(tmp_path)
    paths.config_dir.mkdir()
    real = tmp_path / "dotfiles" / "config.toml"
    real.parent.mkdir()
    real.write_text("[retrieve]\nrerank = false\n")
    paths.config_file.symlink_to(real)
    assert (
        run_setup(
            paths,
            [_ollama()],
            ask=lambda _p: "",
            say=lambda _s: None,
            assume_yes=True,
            pick="ollama-cloud",
        )
        == 0
    )
    assert paths.config_file.is_symlink()
    assert "[generate]" in real.read_text()


def test_rerunning_the_same_choice_changes_nothing(tmp_path):
    paths = _paths(tmp_path)
    assert (
        run_setup(
            paths,
            [_ollama()],
            ask=lambda _p: "",
            say=lambda _s: None,
            assume_yes=True,
            pick="ollama-cloud",
            model="kimi",
            mode="on",
        )
        == 0
    )
    assert (
        plan(
            paths.config_file, paths.config_dir / "models.toml", _ollama(), "kimi", "on"
        )
        == []
    )


def test_test_reports_each_purpose(monkeypatch):
    monkeypatch.setattr("crib.generate.generate", lambda *a, **k: "ok")

    def structured(cfg, system, user, schema, purpose="elaborate", **k):
        if purpose == "summarize":
            raise RuntimeError("401 Unauthorized")
        return {"ok": True}

    monkeypatch.setattr("crib.generate.generate_structured", structured)
    rows = {r["purpose"]: r for r in gensetup.test_purposes(object())}
    assert rows["elaborate"]["ok"] and rows["distill"]["ok"]
    assert not rows["summarize"]["ok"] and "401" in rows["summarize"]["error"]


def test_the_cli_parses_both_verbs():
    from crib.cli import build_parser

    a = build_parser().parse_args(["generate", "setup", "--provider", "x", "--yes"])
    assert a.cmd == "generate" and a.generate_cmd == "setup" and a.yes
    assert build_parser().parse_args(["generate", "test"]).generate_cmd == "test"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(gensetup, "_openai_models", lambda *a, **k: None)
