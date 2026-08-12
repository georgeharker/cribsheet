"""Config and version-ring inputs are hand-edited files that sit under every
command — a typo'd key, a junk `.crib` or a stray file in a ring dir must
degrade to a warning, never take the tool down (Tier 3.4–3.7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from crib.config import Config, CribLink, expand_location, portable_path
from crib.versions import VersionRing

# ── 3.4 unknown config keys ──────────────────────────────────────────────────

def _cfg(tmp_path: Path, body: str) -> Path:
    f = tmp_path / "config.toml"
    f.write_text(body)
    return f


def test_unknown_key_in_a_table_warns_and_loads(tmp_path, capsys):
    f = _cfg(tmp_path, "[embed]\nmodel = 'fe:x'\nmdoel = 'typo'\n")
    cfg = Config.load(f)                      # no TypeError
    assert cfg.embed.model == "fe:x"          # the good key still applied
    err = capsys.readouterr().err
    assert str(f) in err and "embed" in err and "mdoel" in err


def test_unknown_top_level_key_warns_and_loads(tmp_path, capsys):
    f = _cfg(tmp_path, "default_projekt = 'oops'\nversions_keep = 3\n")
    cfg = Config.load(f)
    assert cfg.versions_keep == 3
    assert cfg.default_project == "default"
    err = capsys.readouterr().err
    assert "default_projekt" in err and "top level" in err


def test_every_table_tolerates_unknown_keys(tmp_path):
    f = _cfg(tmp_path, "\n".join(
        f"[{t}]\nnot_a_real_key = 1\n" for t in
        ("embed", "chunk", "retrieve", "memory", "chroma", "daemon", "generate")))
    cfg = Config.load(f)                      # each table filtered independently
    assert cfg.daemon.port == 7732 and cfg.retrieve.hybrid is True


def test_known_keys_are_unaffected(tmp_path, capsys):
    f = _cfg(tmp_path, "[daemon]\nport = 9999\nenabled = false\n")
    cfg = Config.load(f)
    assert cfg.daemon.port == 9999 and cfg.daemon.enabled is False
    assert capsys.readouterr().err == ""      # nothing to warn about


def test_wrong_shaped_values_warn_instead_of_raising(tmp_path, capsys):
    f = _cfg(tmp_path, "versions_keep = 'lots'\nembed = 'fe:x'\n")
    cfg = Config.load(f)
    assert cfg.versions_keep == 20 and cfg.embed.model == "hash"   # defaults kept
    err = capsys.readouterr().err
    assert "versions_keep" in err and "[embed] must be a table" in err


def test_broken_toml_names_the_file(tmp_path):
    f = _cfg(tmp_path, "[embed\nmodel = 'x'\n")
    with pytest.raises(ValueError, match=str(f)):
        Config.load(f)


# ── 3.5 malformed `.crib` ────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [
    "paths:\n  - src\n",              # no `project:` at all
    "- just\n- a\n- list\n",          # not a mapping
    "project: [unclosed\n",           # bad YAML
    "",                               # empty file
    "project: '   '\n",               # blank name
])
def test_malformed_crib_is_treated_as_absent(tmp_path, body, capsys):
    (tmp_path / ".crib").write_text(body)
    sub = tmp_path / "sub"
    sub.mkdir()
    assert CribLink.find(sub) is None         # no exception below it
    assert str(tmp_path / ".crib") in capsys.readouterr().err


def test_walk_continues_past_a_malformed_crib(tmp_path):
    """A junk `.crib` in a working dir must not hide the real one above it."""
    (tmp_path / ".crib").write_text("project: good\npaths:\n  - src\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".crib").write_text("nonsense: true\n")
    link = CribLink.find(repo)
    assert link is not None and link.project == "good" and link.root == tmp_path


def test_good_crib_still_parses(tmp_path):
    (tmp_path / ".crib").write_text(
        "project: proj\npaths:\n  - src\ndocs:\n  - 'docs/**/*.md'\n"
        "refs:\n  - other\n")
    link = CribLink.find(tmp_path)
    assert link.project == "proj" and link.paths == ["src"]
    assert link.doc_patterns == ["docs/**/*.md"] and link.refs == ["other"]


def test_scalar_list_fields_are_tolerated(tmp_path):
    (tmp_path / ".crib").write_text("project: proj\npaths: src\n")
    assert CribLink.find(tmp_path).paths == ["src"]


# ── 3.6 portable paths through symlinked roots ───────────────────────────────

def test_symlinked_root_still_tokenizes(tmp_path):
    real = tmp_path / "real-dev"
    real.mkdir()
    link = tmp_path / "Development"
    link.symlink_to(real)
    locs = {"DEV": str(link)}
    # the path arrives already resolved (how most provenance reaches us) while
    # the configured root is the symlink — it must still find its root
    assert portable_path(real / "repo" / "x.md", locs) == "$DEV/repo/x.md"
    assert portable_path(link / "repo" / "x.md", locs) == "$DEV/repo/x.md"


def test_symlinked_path_under_a_real_root(tmp_path):
    real = tmp_path / "real-dev"
    real.mkdir()
    link = tmp_path / "Development"
    link.symlink_to(real)
    locs = {"DEV": str(real)}                 # root real, path via the symlink
    assert portable_path(link / "repo", locs) == "$DEV/repo"


def test_symlinked_root_round_trips(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "aliased"
    link.symlink_to(real)
    locs = {"DEV": str(link)}
    tok = portable_path(real / "repo" / "doc.md", locs)
    assert expand_location(tok, locs).resolve() == (real / "repo" / "doc.md")


def test_longest_root_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Development").mkdir()
    locs = {"DEV": str(tmp_path / "Development")}
    assert portable_path(tmp_path / "Development" / "r", locs) == "$DEV/r"
    assert portable_path(tmp_path / "elsewhere" / "r", locs) == "$HOME/elsewhere/r"


def test_unmatched_path_still_falls_back(tmp_path):
    assert portable_path("/opt/nowhere/x", {"DEV": str(tmp_path)}) == "/opt/nowhere/x"


# ── 3.7 stray files in a version ring ────────────────────────────────────────

def test_stray_file_in_a_ring_dir_does_not_break_writes(tmp_path, capsys):
    ring = VersionRing(tmp_path / "versions", keep=5)
    e1 = ring.stash("NOTEID", "v1")
    (e1.path.parent / "foo.md").write_text("stray")          # editor backup, junk…
    (e1.path.parent / "README.md").write_text("also stray")

    e2 = ring.stash("NOTEID", "v2")                          # used to ValueError
    assert e2.seq == e1.seq + 1
    assert [e.seq for e in ring.list("NOTEID")] == [2, 1]     # strays not listed
    assert ring.read("NOTEID", e1.name) == "v1"
    assert (e1.path.parent / "foo.md").exists()              # and never deleted
    assert "ignoring 2 file(s)" in capsys.readouterr().err    # warned once


def test_prune_only_counts_real_entries(tmp_path):
    ring = VersionRing(tmp_path / "versions", keep=2)
    d = None
    for i in range(4):
        d = ring.stash("ID", f"v{i}").path.parent
    (d / "stray.md").write_text("x")
    ring.stash("ID", "v4")
    assert [e.seq for e in ring.list("ID")] == [5, 4]
    assert (d / "stray.md").exists()


def test_ring_warns_once_per_dir(tmp_path, capsys):
    ring = VersionRing(tmp_path / "versions", keep=5)
    d = ring.stash("ID2", "v1").path.parent
    (d / "junk.md").write_text("x")
    ring.stash("ID2", "v2")
    ring.stash("ID2", "v3")
    assert capsys.readouterr().err.count("version ring") == 1
