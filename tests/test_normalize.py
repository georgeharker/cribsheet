"""Frontmatter self-heal + portable provenance — the pieces that keep a git
sync from conflicting on derived-note metadata (DESIGN §14)."""

from __future__ import annotations

from pathlib import Path

import yaml

from crib import notes
from crib.config import expand_location, portable_path
from crib.util import derived_ulid


# --- deterministic identity -------------------------------------------------

def test_derived_ulid_is_stable_and_ulid_shaped():
    a = derived_ulid("imported/myrepo/docs/arch.md")
    b = derived_ulid("imported/myrepo/docs/arch.md")
    assert a == b                                   # same key → same id (any machine)
    assert a != derived_ulid("imported/myrepo/README.md")
    assert len(a) == 26 and all(c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for c in a)


# --- portable $LOCATION paths -----------------------------------------------

def test_portable_path_collapses_two_machines():
    # each machine maps its own dev root to the same name → identical token, so
    # the stored `source_repo` is byte-identical and never conflicts on sync
    mac = portable_path("/Users/geo/Development/mcp-companion", {"DEV": "/Users/geo/Development"})
    lin = portable_path("/home/geo/Development/mcp-companion", {"DEV": "/home/geo/Development"})
    assert mac == lin == "$DEV/mcp-companion"


def test_portable_path_uses_home_when_under_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    tok = portable_path(tmp_path / "Development" / "repo", {})
    assert tok == "$HOME/Development/repo"


def test_portable_path_named_root_beats_home_and_round_trips():
    locs = {"DEV": "/work/code"}
    tok = portable_path("/work/code/mcp-companion/docs/x.md", locs)
    assert tok == "$DEV/mcp-companion/docs/x.md"
    assert expand_location(tok, locs) == Path("/work/code/mcp-companion/docs/x.md")


def test_portable_path_unmatched_falls_back_to_string():
    assert portable_path("/opt/elsewhere/repo", {}) == "/opt/elsewhere/repo"


# --- self-heal of merge-duplicated frontmatter ------------------------------

# the exact shape git leaves after unioning two machines' provenance
_CONFLICTED = """\
---
id: 01KW6G9H0NX5VGKJT9CXZ4A1MN
id: 01KW7VKTZPKF55A0R0G4ZRYY9W
source: imported
source_repo: /Users/geohar/Development/mcp-companion
source_repo: /home/geohar/Development/mcp-companion
source_path: docs/designs/per-chat-session-filtering.md
imported: '2026-06-27'
imported: '2026-06-28'
---

# Per-chat session filtering

Body text that should survive untouched.
"""


def test_normalize_collapses_duplicate_keys_deterministically():
    healed, changed = notes.normalize_text(_CONFLICTED)
    assert changed
    fm, body = notes.parse(healed)
    # one value per key now, chosen as the deterministic (lexical) minimum
    assert fm["id"] == "01KW6G9H0NX5VGKJT9CXZ4A1MN"         # earliest ULID
    assert fm["imported"] == "2026-06-27"                   # first import wins
    assert fm["source_repo"] == "/Users/geohar/Development/mcp-companion"
    assert "Body text that should survive untouched." in body
    # and it's valid YAML again (no duplicate keys)
    raw_fm = healed.split("---")[1]
    assert yaml.safe_load(raw_fm)


def test_normalize_is_order_independent():
    # swap which machine's lines came first — every machine must converge on the
    # same resolution, else the merge re-conflicts on the next sync
    swapped = _CONFLICTED.replace(
        "id: 01KW6G9H0NX5VGKJT9CXZ4A1MN\nid: 01KW7VKTZPKF55A0R0G4ZRYY9W",
        "id: 01KW7VKTZPKF55A0R0G4ZRYY9W\nid: 01KW6G9H0NX5VGKJT9CXZ4A1MN")
    h1, _ = notes.normalize_text(_CONFLICTED)
    h2, _ = notes.normalize_text(swapped)
    assert h1 == h2                                     # identical bytes, either order
    assert notes.parse(h1)[0]["id"] == "01KW6G9H0NX5VGKJT9CXZ4A1MN"


def test_normalize_noop_on_clean_frontmatter():
    clean = "---\nid: 01ABC\nsource: imported\n---\n\nbody\n"
    healed, changed = notes.normalize_text(clean)
    assert not changed and healed == clean


def test_heal_file_rewrites_only_when_dirty(tmp_path):
    p = tmp_path / "n.md"
    p.write_text(_CONFLICTED)
    assert notes.heal_file(p) is True
    assert notes.load(p).id == "01KW6G9H0NX5VGKJT9CXZ4A1MN"
    assert notes.heal_file(p) is False          # idempotent: second pass is a no-op
