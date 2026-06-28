"""Line-span mapping for lookup hits.

`section_line_map` resolves a chunk's heading_path to its (line_start, line_end)
span in the *current* file on disk — computed at query time, so the numbers
never go stale when edits above a section shift it. These lock the span
arithmetic (frontmatter offset, heading nesting, extent-to-next-heading) and the
wiring through `crib.lookup`.
"""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.chunk import chunk_note, section_line_map
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    paths = Paths.resolve().ensure()
    return Crib(paths, Config(), InMemoryStore())


def run(coro):
    return asyncio.run(coro)


# --- section_line_map: pure span arithmetic --------------------------------

def test_frontmatter_offset_and_nesting():
    text = (
        "---\n"          # 1
        "id: x\n"        # 2
        "title: T\n"     # 3
        "---\n"          # 4
        "\n"             # 5
        "# A\n"          # 6
        "intro\n"        # 7
        "\n"             # 8
        "## B\n"         # 9
        "body of b\n"    # 10
        "\n"             # 11
        "# C\n"          # 12
        "last\n"         # 13
    )
    m = section_line_map(text)
    # start = heading line; end = line before the next heading (extent).
    assert m == {"A": (6, 8), "A/B": (9, 11), "C": (12, 13)}
    # No spurious pre-heading section when only blank lines precede the first.
    assert "" not in m


def test_no_frontmatter_and_preheading_section():
    text = (
        "prologue line\n"   # 1
        "more prologue\n"   # 2
        "\n"                # 3
        "# H\n"             # 4
        "under h\n"         # 5
    )
    m = section_line_map(text)
    assert m[""] == (1, 3)      # content before the first heading
    assert m["H"] == (4, 5)


def test_missing_heading_is_absent():
    m = section_line_map("# Only\nbody\n")
    assert "Nope" not in m
    assert m["Only"] == (1, 2)


# --- lookup wiring ---------------------------------------------------------

def test_lookup_reports_line_span(crib):
    body = "# Alpha\nintro about alpha\n\n# Beta\nbeta gamma delta\n"
    rel = run(crib.store_note(body, title="doc", project="p"))["relpath"]

    hit = next(h for h in crib.lookup("beta gamma", project="p")
               if h.relpath == rel)
    assert hit.heading.endswith("Beta")

    # The reported start must be the real line of "# Beta" in the written file
    # (which now carries frontmatter above the body).
    lines = crib.read_note(rel, project="p").splitlines()
    beta_line = next(i for i, ln in enumerate(lines, 1) if ln.strip() == "# Beta")
    assert hit.line_start == beta_line


def test_long_section_span_covers_full_extent(crib):
    # A section long enough to window into multiple chunks; every window must
    # resolve to the one section span (its full extent), not a sub-window.
    rows = [f"word{i} alpha beta gamma delta epsilon zeta eta theta iota"
            for i in range(60)]
    body = "# Big\n" + "\n".join(rows) + "\n"
    assert len(chunk_note("p", "x.md", "id", body)) > 1   # it really windows

    rel = run(crib.store_note(body, title="big", project="p"))["relpath"]
    hit = next(h for h in crib.lookup("alpha beta gamma", project="p")
               if h.relpath == rel)

    lines = crib.read_note(rel, project="p").splitlines()
    big_line = next(i for i, ln in enumerate(lines, 1) if ln.strip() == "# Big")
    assert hit.line_start == big_line
    assert hit.line_end == len(lines)     # extends to the last line of the file
