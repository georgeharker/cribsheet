"""`status` must never show a git remote's credentials (the remote URL is
how crib-memory's token was embedded)."""

from __future__ import annotations

import pytest

from crib.gitbacking import GitBacking, redact_url


@pytest.mark.parametrize(
    "url, shown",
    [
        (
            "https://user:gho_SECRET@github.com/o/r.git",
            "https://user@github.com/o/r.git",
        ),
        ("https://gho_SECRET@github.com/o/r.git", "https://github.com/o/r.git"),
        ("https://:gho_SECRET@github.com/o/r.git", "https://github.com/o/r.git"),
        ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ("ssh://git@github.com/o/r.git", "ssh://git@github.com/o/r.git"),
        ("https://github.com/o/r.git", "https://github.com/o/r.git"),
        ("/srv/crib-memory.git", "/srv/crib-memory.git"),
    ],
)
def test_redact_url(url, shown):
    assert redact_url(url) == shown


def test_state_shows_the_remote_without_its_token(tmp_path):
    g = GitBacking(tmp_path, allow_network=False)
    g._run("init")
    g._run("remote", "add", "origin", "https://me:gho_SECRET@github.com/o/r.git")
    st = g.state()
    assert st["remote"] == "https://me@github.com/o/r.git"
    assert "SECRET" not in repr(st)
    assert (
        g.current_remote() == "https://me:gho_SECRET@github.com/o/r.git"
    )  # git keeps it
