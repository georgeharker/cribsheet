"""Chunk identity for repeated heading paths (plan 2.9).

`chunk_id = sha1(project + relpath + heading_path + window_idx)` collided when
one note carried two sections with the same effective heading stack — the later
section overwrote the earlier one in the store, and `section_line_map` kept only
the first span. A per-note occurrence counter, shared by the splitter and the
line map, disambiguates the id (`…#2`) while leaving the visible `heading_path`
clean.
"""

from __future__ import annotations

from crib.chunk import (
    _split_sections,
    chunk_note,
    section_key,
    section_line_map,
)
from crib.util import sha1_hex

DUPES = (
    "# Doc\n"        # 1
    "intro\n"        # 2
    "## Notes\n"     # 3
    "first body\n"   # 4
    "## Notes\n"     # 5
    "second body\n"  # 6
)


# --- splitter --------------------------------------------------------------

def test_duplicate_headings_are_separate_sections_with_occurrences():
    secs = _split_sections(DUPES)
    assert [(s.heading_path, s.occurrence) for s in secs] == [
        (["Doc"], 1), (["Doc", "Notes"], 1), (["Doc", "Notes"], 2),
    ]
    # both bodies survive — neither is dropped or merged into the other
    assert [s.text for s in secs] == ["intro", "first body", "second body"]


def test_duplicate_headings_get_distinct_chunk_ids():
    chunks = chunk_note("p", "n.md", "id", DUPES)
    ids = [c.chunk_id for c in chunks]
    assert len(set(ids)) == len(ids) == 3          # no collision, nothing lost
    dupes = [c for c in chunks if c.heading_path == ["Doc", "Notes"]]
    assert [c.text for c in dupes] == ["first body", "second body"]
    assert dupes[0].chunk_id != dupes[1].chunk_id
    # the disambiguator lives in the id only — display stays clean
    assert all(c.heading_path == ["Doc", "Notes"] for c in dupes)
    assert [c.occurrence for c in dupes] == [1, 2]


def test_occurrence_reaches_metadata_but_not_the_heading_path():
    chunks = chunk_note("p", "n.md", "id", DUPES)
    metas = [c.metadata("t", [], "note", 0.0) for c in chunks]
    assert [m["occurrence"] for m in metas] == [1, 1, 2]
    assert [m["heading_path"] for m in metas] == ["Doc", "Doc/Notes", "Doc/Notes"]


def test_first_occurrence_id_unchanged_from_the_bare_breadcrumb():
    # Only 2nd+ occurrences move; unique headings (the overwhelming majority)
    # keep their v1 ids, so a reindex doesn't churn every chunk in the store.
    c = chunk_note("p", "n.md", "id", "# Solo\nbody\n")[0]
    assert c.chunk_id == sha1_hex("p", "n.md", "Solo", "0")
    assert section_key(["Solo"]) == "Solo"
    assert section_key(["Solo"], 2) == "Solo#2"


# --- pillar store axis (v3) ------------------------------------------------

def test_notes_store_ids_are_unchanged_by_the_store_axis():
    # The notes spelling stays UNQUALIFIED, so every pre-split id survives the
    # v3 bump as a metadata-only re-stamp — no re-embed of the notes corpus.
    body = "# Solo\nbody\n"
    assert (chunk_note("p", "n.md", "id", body)[0].chunk_id
            == chunk_note("p", "n.md", "id", body, store="notes")[0].chunk_id
            == sha1_hex("p", "n.md", "Solo", "0"))


def test_same_relpath_in_two_stores_never_collides():
    # Store-relative relpaths repeat across pillars in the ONE shared
    # collection; the `store:` qualifier keeps their identities disjoint.
    body = "# Solo\nbody\n"
    ids = {chunk_note("p", "n.md", "id", body, store=s)[0].chunk_id
           for s in ("notes", "design", "plans", "learnings")}
    assert len(ids) == 4
    c = chunk_note("p", "n.md", "id", body, store="design")[0]
    assert c.chunk_id == sha1_hex("p", "design:n.md", "Solo", "0")


def test_store_reaches_metadata():
    m = chunk_note("p", "n.md", "id", "# S\nb\n", store="plans")[0] \
        .metadata("t", [], "note", 0.0)
    assert m["store"] == "plans"
    assert chunk_note("p", "n.md", "id", "# S\nb\n")[0] \
        .metadata("t", [], "note", 0.0)["store"] == "notes"


def test_ids_are_deterministic_across_runs():
    runs = [[c.chunk_id for c in chunk_note("p", "n.md", "id", DUPES)]
            for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]
    # ...and independent of how the note is windowed for the *first* window
    assert runs[0] == [c.chunk_id for c in
                       chunk_note("p", "n.md", "id", DUPES, window_words=4000)]


# --- line map --------------------------------------------------------------

def test_line_map_covers_every_occurrence():
    m = section_line_map(DUPES)
    assert m == {"Doc": (1, 2), "Doc/Notes": (3, 4), "Doc/Notes#2": (5, 6)}


def test_line_map_keys_match_chunk_identity():
    m = section_line_map(DUPES)
    for c in chunk_note("p", "n.md", "id", DUPES):
        meta = c.metadata("t", [], "note", 0.0)
        key = section_key(meta["heading_path"], meta["occurrence"])
        assert key in m, f"no span for {key}"
    # every span is claimed by a chunk (no orphan keys)
    assert len(m) == 3


def test_windowed_duplicate_sections_share_their_section_span():
    long_a = " ".join(f"alpha{i}" for i in range(900))
    long_b = " ".join(f"beta{i}" for i in range(900))
    body = f"## Notes\n{long_a}\n## Notes\n{long_b}\n"
    chunks = chunk_note("p", "n.md", "id", body, window_words=100, overlap=20)
    m = section_line_map(body)
    assert len(chunks) > 2                                  # really windowed
    assert len({c.chunk_id for c in chunks}) == len(chunks)  # still unique
    assert m == {"Notes": (1, 2), "Notes#2": (3, 4)}
    seconds = [c for c in chunks if c.occurrence == 2]
    assert seconds and all("beta0" in c.section_text for c in seconds)


# --- shared fence state machine (review finding 4.16) ----------------------

def test_fenced_heading_does_not_split_either_walker():
    body = (
        "# Real\n"            # 1
        "text\n"              # 2
        "```md\n"             # 3
        "## fake heading\n"   # 4
        "still code\n"        # 5
        "```\n"               # 6
        "more\n"              # 7
        "## Second\n"         # 8
        "body\n"              # 9
    )
    assert [s.heading_path for s in _split_sections(body)] == [
        ["Real"], ["Real", "Second"]]
    assert section_line_map(body) == {"Real": (1, 7), "Real/Second": (8, 9)}
    assert all("fake heading" not in "/".join(c.heading_path)
               for c in chunk_note("p", "n.md", "id", body))


def test_fenced_duplicate_heading_does_not_bump_the_occurrence_counter():
    # A `## Notes` line inside a fence must not consume occurrence #2 — else the
    # splitter and the line map would disagree about which section is which.
    body = (
        "## Notes\n"        # 1
        "```\n"             # 2
        "## Notes\n"        # 3
        "```\n"             # 4
        "## Notes\n"        # 5
        "second\n"          # 6
    )
    secs = _split_sections(body)
    assert [(s.heading_path, s.occurrence) for s in secs] == [
        (["Notes"], 1), (["Notes"], 2)]
    assert section_line_map(body) == {"Notes": (1, 4), "Notes#2": (5, 6)}


def test_tilde_fence_tracked_too():
    body = "# H\n~~~\n## nope\n~~~\ntail\n"
    assert [s.heading_path for s in _split_sections(body)] == [["H"]]
    assert list(section_line_map(body)) == ["H"]
