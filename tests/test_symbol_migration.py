"""Note conversion (`learning_migrate`) and legacy-spelling resolution.

The join is correct WITHOUT conversion — a note bound to any prior spelling keeps
working through `bindings` — so what these tests pin is the tidiness pass: per-note
classification with no ordering constraint against the entry conversion, writes
through NoteStore (the chunk index moves with the file), history kept, and the
states said out loud rather than merged or guessed.
"""

from __future__ import annotations

import asyncio

import pytest

from crib import notes
from crib.app import Crib
from crib.codeindex import SymbolIndex
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore
from crib.tomlrec import write_atomic


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def run(coro):
    return asyncio.run(coro)


def _legacy_toml(e):
    """Render a record the way v0.6.1 did — `fqname`/`module`/`parent` present, no
    current-shape fields. The CURRENT `_render` cannot write this shape any more,
    which is the point; a legacy store has to be forged byte-by-byte."""
    lines = []
    for k in ("fqname", "name", "kind", "lang", "module", "parent", "content_hash",
              "file", "file_hash", "signature", "description"):
        lines.append(f'{k} = "{e.get(k, "")}"')
    lines.append(f'line = {e.get("line", 0)}')
    lines.append(f'mtime = {e.get("mtime", 0)}')
    for k in ("container", "calls", "called_by", "references", "name_terms",
              "keywords"):
        vals = e.get(k) or []
        if vals:
            lines.append(f"{k} = [")
            lines += [f'  "{v}",' for v in vals]
            lines.append("]")
        else:
            lines.append(f"{k} = []")
    return "\n".join(lines) + "\n"


def _seed(crib, project, fqname="pkg.mod.foo", name=None, file="pkg/mod.py"):
    SymbolIndex(crib.paths.project_dir(project)).write({
        "fqname": fqname, "name": name or fqname.split(".")[-1], "kind": "function",
        "lang": "python", "content_hash": "h1", "file": file, "line": 1,
        "signature": "def f():", "description": "", "container": [],
        "calls": [], "called_by": [], "references": [], "name_terms": []})


def _legacy_note(crib, project, binding, relpath=None, body="pinned"):
    """A note as the old world wrote it: bound via `symbol:`, filed under the
    legacy slug."""
    from crib.symbols import ref_slug
    root = crib.learnings.store.root(project)
    root.mkdir(parents=True, exist_ok=True)
    path = root / (relpath or f"{ref_slug(binding)}.md")
    notes.save_atomic(notes.Note(path=path, body=f"\n### 2026-01-01\n{body}\n",
                                 frontmatter={"title": binding,
                                              "kind": "code-learning",
                                              "symbol": binding, "id": f"id-{body}"}))
    return path


def test_dry_run_classifies_and_writes_nothing(crib):
    _seed(crib, "p")
    old = _legacy_note(crib, "p", "pkg.mod.foo")
    out = run(crib.learning_migrate(project="p"))
    assert out["applied"] is False and out["convert"] == 1 and out["orphan"] == 0
    assert old.exists()                                    # nothing moved
    assert "manifest" not in out                           # no record of a non-event


def test_apply_rebinds_renames_and_keeps_history(crib):
    _seed(crib, "p")
    old = _legacy_note(crib, "p", "pkg.mod.foo")
    out = run(crib.learning_migrate(project="p", apply=True))
    assert out["convert"] == 1 and out["applied"] is True and out["manifest"]

    ldir = crib.learnings.store.root("p")
    files = sorted(p.name for p in ldir.glob("*.md"))
    assert not old.exists() and len(files) == 1
    fm = notes.load(ldir / files[0]).frontmatter
    assert fm["symbol_ref"] == "pkg/mod.py#foo"
    assert "symbol" not in fm                              # renamed, not duplicated
    assert fm["symbol_was"] == ["pkg.mod.foo"]             # historied, not replaced
    assert fm["id"] == "id-pinned"                         # the note IS its id

    # idempotent: the second pass finds only noops — re-running IS the resume
    again = run(crib.learning_migrate(project="p", apply=True))
    assert again["noop"] == 1 and again["convert"] == 0


def test_conversion_needs_no_converted_entry_store(crib):
    """The old sweep REFUSED a half-converted store; there is nothing left to
    refuse, because the entry's key is derivable whether or not the entry
    conversion has run. Seed the store RAW (legacy shape, never normalized) and
    the note still converts against the derived key."""
    si = SymbolIndex(crib.paths.project_dir("p"))
    si.root.mkdir(parents=True, exist_ok=True)
    write_atomic(si.root / "pkg.mod.foo.toml", _legacy_toml({
        "fqname": "pkg.mod.foo", "name": "foo", "kind": "function",
        "lang": "python", "content_hash": "h", "file": "pkg/mod.py", "line": 1,
        "signature": "", "description": "", "container": [],
        "calls": [], "called_by": [], "references": [], "name_terms": []}))
    _legacy_note(crib, "p", "pkg.mod.foo")
    out = run(crib.learning_migrate(project="p", apply=True))
    assert out["convert"] == 1 and out["orphan"] == 0


def test_an_unmappable_note_is_an_orphan_and_untouched(crib):
    _seed(crib, "p")
    other = _legacy_note(crib, "p", "gone.symbol", body="orphaned")
    out = run(crib.learning_migrate(project="p", apply=True))
    assert out["orphan"] == 1
    assert other.exists()                                  # keeps its binding, as-is
    assert notes.load(other).frontmatter.get("symbol") == "gone.symbol"


def test_two_notes_claiming_one_symbol_collide_never_merge(crib):
    _seed(crib, "p")
    a = _legacy_note(crib, "p", "pkg.mod.foo", body="first")
    b = _legacy_note(crib, "p", "pkg/mod.py#foo", relpath="hand-name.md",
                     body="second")
    out = run(crib.learning_migrate(project="p", apply=True))
    assert out["convert"] + out["noop"] == 1 and out["collision"] == 1
    # both bodies still exist somewhere — hand-written text is never destroyed
    bodies = "".join(p.read_text() for p in crib.learnings.store.root("p").glob("*.md"))
    assert "first" in bodies and "second" in bodies
    assert a.exists() or b.exists()


def test_converted_notes_are_searchable(crib):
    """The old sweep bypassed NoteStore, so a successfully-migrated note vanished
    from `note_lookup`. Conversion goes through the store; the chunk index must
    hold the note under its NEW relpath."""
    _seed(crib, "p")
    _legacy_note(crib, "p", "pkg.mod.foo", body="searchable insight")
    run(crib.learning_migrate(project="p", apply=True))
    ldir = crib.learnings.store.root("p")
    new_rel = next(p.name for p in ldir.glob("*.md"))
    ids = crib.learnings.store.chunk_ids("p", new_rel) \
        if hasattr(crib.learnings.store, "chunk_ids") else None
    if ids is not None:
        assert ids, "converted note has no chunks under its new relpath"
    # regardless of store internals, the verbs must find it
    assert crib.learning_read("pkg.mod.foo", project="p")["found"]
    assert crib.learning_read("pkg/mod.py#foo", project="p")["found"]


# --- legacy spellings keep resolving, exactly, forever ---------------------------

def test_the_legacy_key_resolves_as_a_run_and_symbol_was_exactly(crib):
    from crib.symbols import match_entry
    e = {"file": "rust/src/core/state.rs", "name": "exit_code", "lang": "rust",
         "container": ["ServerState"],
         "symbol_was": ["rust::src::core::state::ServerState::exit_code"]}
    # the whole legacy key is a trailing run of the canonical form
    assert match_entry(e, "rust::src::core::state::ServerState::exit_code") == "exact"
    # a spelling only history knows (no rule reproduces it) matches via symbol_was
    e2 = {"file": "scripts/dump.lua", "name": "c", "lang": "lua", "container": [],
          "symbol_was": ["scripts.dump.for in.c"]}
    assert match_entry(e2, "scripts.dump.for in.c") == "was"
    # …and EXACTLY only: prior spellings are compatibility, not a search space
    assert match_entry(e2, "for in.c") is None
