"""The plugin instructions are COPIES of CLAUDE.md.example, never symlinks —
and never drifted ones.

Why the copy rule is load-bearing (mcp-companion 0.10.x): marketplace installs
and npm packs copy the plugin subtree out of the repo into a cache with no repo
root, and neither reliably preserves symlinks — a relative `../..` symlink
arrives dangling (or worse, dereferenced), so the SessionStart hook silently
emits no instructions. bump-version.sh re-syncs the copies on every release;
this test is the belt that fails LOUDLY if a copy goes missing, gets
symlinked by accident, or drifts from the directive between releases.

The instructions matter more than the MCP face's own "instructions=" text
through a combiner: the combiner does not forward an upstream's server
instructions, so for its clients the directive in instructions.txt IS the
instructions the model sees.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "CLAUDE.md.example"
PLUGINS = ("claude", "opencode", "pi")


@pytest.mark.parametrize("plugin", PLUGINS)
def test_instructions_txt_is_a_drift_free_copy(plugin: str) -> None:
    path = ROOT / "plugins" / plugin / "instructions.txt"
    assert path.is_file(), f"missing: {plugin}/instructions.txt"
    # regular file, NOT a symlink: pack/instal copies may not preserve them
    assert not path.is_symlink(), (
        f"{plugin}/instructions.txt is a SYMLINK — pack/instal copies may arrive "
        "dangling; keep it a regular copy of CLAUDE.md.example"
    )
    assert path.read_text(encoding="utf-8") == EXAMPLE.read_text(encoding="utf-8"), (
        f"{plugin}/instructions.txt drifted from CLAUDE.md.example — re-sync it "
        "(scripts/bump-version.sh does this on every release; sync it now to land "
        "the edit between releases)"
    )
