"""Entry conversion: read + write + schema stamp, per record, resumable by re-run.

The mechanism under test is `SymbolIndex.normalize_identity` (every write files a
record under the slug of its own key) plus `symconvert`'s two guards. What these
tests pin, in order of importance:

  1. the expensive facets come through BYTE-IDENTICAL — description, keywords, and
     all three edge lists — because that is the entire argument for converting
     instead of reindexing;
  2. `fqname` survives as `symbol_was[0]` (the stored STRING, not a regeneration
     rule), and `module`/`parent` are dropped;
  3. done-ness is derived from the record (schema + canonical filename), so a
     leftover file from a crash is ordinary work for the next run, not damage.
"""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.codeindex import SYMBOL_SCHEMA_VERSION, SymbolIndex
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore
from crib.symconvert import convert_entry, convertible, preserved
from crib.tomlrec import write_atomic

TARGET = SYMBOL_SCHEMA_VERSION

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


# A v0.6.1-shaped record: one name (`fqname`), `module` and `parent` present, no
# symbol_ref / fqn / scope / schema. What every released store actually holds.
LEGACY = {
    "fqname": "pkg.mod.Klass.method", "name": "method", "kind": "method",
    "lang": "python", "module": "pkg.mod", "parent": "pkg.mod.Klass",
    "content_hash": "abcd1234", "file": "pkg/mod.py", "file_hash": "ffff0000",
    "line": 10, "mtime": 7, "signature": "def method(self):",
    "description": "does the thing", "keywords": ["thing", "doer"],
    "container": ["Klass"],
    "calls": ["helper [pkg/util.py]"], "called_by": ["main [pkg/app.py]"],
    "references": ["main [pkg/app.py]"], "name_terms": ["method"],
}


def test_convert_derives_the_identity_and_keeps_the_history():
    out = convert_entry(dict(LEGACY), TARGET)
    assert out["symbol_ref"] == "pkg/mod.py#Klass.method"
    assert out["fqn"] == "pkg.mod.Klass.method"
    assert out["scope"] == ["pkg", "mod", "Klass"]
    # the legacy KEY is kept as the stored string it was, not re-derived later
    assert out["symbol_was"] == ["pkg.mod.Klass.method"]
    assert out["schema"] == TARGET
    # retired: content preserved elsewhere (symbol_was) or derivable (truncation)
    assert "fqname" not in out and "module" not in out and "parent" not in out


def test_convert_preserves_every_expensive_facet_byte_identical():
    out = convert_entry(dict(LEGACY), TARGET)
    assert preserved(LEGACY, out) == []
    for k in ("description", "keywords", "calls", "called_by", "references",
              "content_hash"):
        assert out[k] == LEGACY[k]


def test_preserved_names_a_mangled_facet():
    out = convert_entry(dict(LEGACY), TARGET)
    out["description"] = "regenerated!"
    assert preserved(LEGACY, out) == ["description"]


def test_convert_is_idempotent():
    once = convert_entry(dict(LEGACY), TARGET)
    again = convert_entry(dict(once), TARGET)
    assert again == once


def test_an_entry_without_a_file_is_not_convertible():
    broken = {k: v for k, v in LEGACY.items() if k != "file"}
    assert not convertible(broken)
    assert convertible(LEGACY)


# --- the verb, over a real store ------------------------------------------------

@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def _seed_legacy_store(crib, project, n=3):
    """Write RAW legacy records at legacy-fqname filenames — bypassing
    `SymbolIndex.write`, which would convert them on the way in."""
    si = SymbolIndex(crib.paths.project_dir(project))
    si.root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        e = dict(LEGACY, fqname=f"pkg.mod.f{i}", name=f"f{i}", container=[],
                 parent="", description=f"desc {i}")
        write_atomic(si.root / f"pkg.mod.f{i}.toml", _legacy_toml(e))
    return si


def test_code_convert_dry_run_reports_and_writes_nothing(crib):
    si = _seed_legacy_store(crib, "p")
    before = sorted(p.name for p in si.root.glob("*.toml"))
    out = asyncio.run(crib.code_convert(project="p"))
    assert out == {"project": "p", "applied": False, "total": 3,
                   "already_done": 0, "converted": 3,
                   "needs_reindex": [], "violations": [], "collisions": []}
    assert sorted(p.name for p in si.root.glob("*.toml")) == before


def test_code_convert_renames_stamps_and_preserves(crib):
    si = _seed_legacy_store(crib, "p")
    out = asyncio.run(crib.code_convert(project="p", apply=True))
    assert out["converted"] == 3 and not out["violations"]
    names = sorted(p.name for p in si.root.glob("*.toml"))
    # canonical: slug of the record's own key, hash always appended for a reference
    assert all("#" not in n and "-" in n and n.endswith(".toml") for n in names)
    entries = {e["name"]: e for e in si.all()}
    assert entries["f1"]["description"] == "desc 1"          # facet survived
    assert entries["f1"]["symbol_was"] == ["pkg.mod.f1"]      # history kept
    assert all(int(e.get("schema") or 0) == TARGET for e in entries.values())
    assert si.stored_schema() == TARGET                       # completion claim
    # re-run: everything already done — that IS the resume mechanism
    again = asyncio.run(crib.code_convert(project="p", apply=True))
    assert again["already_done"] == 3 and again["converted"] == 0


def test_code_convert_sweeps_a_crash_leftover(crib):
    """Crash window: new canonical file written, old name not yet unlinked. The
    leftover is detected from the data (its filename is not the slug of its own
    key) and swept as ordinary work by the next run — no journal, no repair mode."""
    import shutil
    si = _seed_legacy_store(crib, "p", n=1)
    asyncio.run(crib.code_convert(project="p", apply=True))
    canonical = next(si.root.glob("*.toml"))
    shutil.copy(canonical, si.root / "pkg.mod.f0.toml")       # resurrect the old name
    out = asyncio.run(crib.code_convert(project="p", apply=True))
    assert out["converted"] == 1                              # the leftover, re-filed
    assert sorted(p.name for p in si.root.glob("*.toml")) == [canonical.name]


def test_a_record_with_no_file_is_reported_for_reindex_not_guessed(crib):
    si = _seed_legacy_store(crib, "p", n=1)
    write_atomic(si.root / "broken.toml", 'name = "ghost"\nschema = 0\n')
    out = asyncio.run(crib.code_convert(project="p", apply=True))
    # too broken to even parse an identity → invisible to records(); a record with
    # a name but no file would land in needs_reindex — either way nothing is guessed
    assert out["violations"] == []
    assert not (si.root / f"{'ghost'}.toml").exists()


def test_colliding_records_convert_one_and_report_the_rest(crib):
    """Two records whose cleaned identities derive ONE key (real case: Lua locals
    told apart only by synthetic `for in` / `do end` containers). Converting both
    would silently erase one — so one converts, the rest are LEFT AS THEY ARE and
    reported, and the store is never stamped complete over them."""
    si = _seed_legacy_store(crib, "p", n=0)
    for cont in (["if", "for in"], ["if", "do end"]):
        e = dict(LEGACY, fqname=f"t.lua.{'-'.join(cont)}.out", name="out",
                 lang="lua", file="t.lua", container=cont, parent="")
        write_atomic(si.root / f"{e['fqname'].replace(' ', '-')}.toml",
                     _legacy_toml(e))
    out = asyncio.run(crib.code_convert(project="p", apply=True))
    assert out["converted"] == 1 and len(out["collisions"]) == 1
    assert si.stored_schema() != TARGET          # not complete, and does not claim it
    # both records still exist — one canonical, one at its legacy name
    assert len(list(si.root.glob("*.toml"))) == 2
    again = asyncio.run(crib.code_convert(project="p", apply=True))
    assert len(again["collisions"]) == 1         # still said, every run
