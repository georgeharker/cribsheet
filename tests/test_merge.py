"""Frontmatter-aware merge driver: header merges deterministically (never
conflicts), body conflicts still surface (DESIGN §14)."""

from __future__ import annotations

from crib import merge, notes


def _note(id_, repo, imported, body):
    fm = {"id": id_, "source": "imported", "source_repo": repo,
          "source_path": "docs/x.md", "imported": imported}
    return notes.serialize(fm, body)


# --- frontmatter 3-way resolution -------------------------------------------

def test_frontmatter_merge_is_symmetric_and_deterministic():
    base = {"id": "01A", "imported": "2026-01-01"}
    a = {"id": "01A", "imported": "2026-06-27", "source_repo": "$HOME/a"}
    b = {"id": "01A", "imported": "2026-06-28", "source_repo": "$HOME/b"}
    ab = merge.merge_frontmatter(base, a, b)
    ba = merge.merge_frontmatter(base, b, a)
    assert ab == ba                              # which side is "ours" can't matter
    assert ab["imported"] == "2026-06-27"        # earliest (first-import) wins
    assert ab["source_repo"] == "$HOME/a"        # deterministic lexical pick


def test_frontmatter_merge_takes_changed_side():
    base = {"id": "01A", "imported": "2026-01-01"}
    ours = {"id": "01A", "imported": "2026-01-01"}      # unchanged from base
    theirs = {"id": "01A", "imported": "2026-01-01", "tags": ["new"]}  # added a key
    out = merge.merge_frontmatter(base, ours, theirs)
    assert out["tags"] == ["new"]                # a side's pure addition survives


# --- whole-note merge via the driver ----------------------------------------

def test_frontmatter_only_divergence_merges_clean():
    base = _note("01A", "$HOME/a", "2026-01-01", "Shared body.\n")
    ours = _note("01A", "$HOME/a", "2026-06-27", "Shared body.\n")
    theirs = _note("01A", "$HOME/b", "2026-06-28", "Shared body.\n")
    text, conflicted = merge.merge_note_texts(base, ours, theirs)
    assert not conflicted                        # body identical → no conflict
    fm, body = notes.parse(text)
    assert fm["imported"] == "2026-06-27" and "Shared body." in body
    assert "<<<<<<<" not in text


def test_body_divergence_surfaces_with_healed_header():
    base = _note("01A", "$HOME/a", "2026-01-01", "Original line.\n")
    ours = _note("01A", "$HOME/a", "2026-06-27", "Our rewrite.\n")
    theirs = _note("01A", "$HOME/b", "2026-06-28", "Their rewrite.\n")
    text, conflicted = merge.merge_note_texts(base, ours, theirs)
    assert conflicted                            # divergent body → surfaced
    assert "<<<<<<<" in text and ">>>>>>>" in text
    # …but the header is already resolved, not duplicated
    head = text.split("---")[1]
    assert head.count("imported:") == 1
    fm, _ = notes.parse(text)
    assert fm["imported"] == "2026-06-27"


def test_run_driver_writes_and_signals(tmp_path):
    base = tmp_path / "O"; cur = tmp_path / "A"; oth = tmp_path / "B"
    base.write_text(_note("01A", "$HOME/a", "2026-01-01", "Base.\n"))
    cur.write_text(_note("01A", "$HOME/a", "2026-06-27", "Base.\n"))   # body == base
    oth.write_text(_note("01A", "$HOME/b", "2026-06-28", "Base.\n"))
    rc = merge.run_driver(str(base), str(cur), str(oth))
    assert rc == 0                               # clean → git marks resolved
    assert notes.parse(cur.read_text())[0]["imported"] == "2026-06-27"

    oth.write_text(_note("01A", "$HOME/b", "2026-06-28", "Totally different.\n"))
    cur.write_text(_note("01A", "$HOME/a", "2026-06-27", "Our text.\n"))
    rc = merge.run_driver(str(base), str(cur), str(oth))
    assert rc == 1                               # body conflict → git leaves unmerged
    assert "<<<<<<<" in cur.read_text()


def test_symbol_toml_merge_is_clean_and_symmetric():
    from crib.codeindex import _parse, _render
    base = _render({"fqname": "m.f", "name": "f", "kind": "function",
                    "content_hash": "h1", "description": "old", "file": "m.py",
                    "line": 1, "mtime": 100, "calls": [], "called_by": [],
                    "references": [], "name_terms": ["f"]})
    ours = _render({**_parse(base), "description": "better", "mtime": 200})
    theirs = _render({**_parse(base), "mtime": 150})
    m1 = merge.merge_symbol_texts(base, ours, theirs)
    assert "<<<<<<<" not in m1
    assert _parse(m1)["description"] == "better"   # theirs==base → ours (changed) wins
    # symmetric: A/B swap yields identical bytes (never re-conflicts on the next sync)
    assert merge.merge_symbol_texts(base, theirs, ours) == m1


def test_run_driver_routes_toml_clean(tmp_path):
    from crib.codeindex import _parse, _render
    e = {"fqname": "m.f", "name": "f", "kind": "function", "content_hash": "h",
         "description": "d", "file": "m.py", "line": 1, "mtime": 1, "calls": [],
         "called_by": [], "references": [], "name_terms": ["f"]}
    b = tmp_path / "O.toml"; c = tmp_path / "A.toml"; o = tmp_path / "B.toml"
    b.write_text(_render(e))
    c.write_text(_render({**e, "mtime": 2}))
    o.write_text(_render({**e, "mtime": 3}))
    assert merge.run_driver(str(b), str(c), str(o)) == 0   # toml always clean
    assert "<<<<<<<" not in c.read_text()
