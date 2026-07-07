"""Cross-project refs: `.crib` `refs:` → query fan-out, qualified edges,
project boundaries, and locally-different checkout paths."""

from __future__ import annotations

import pytest

from crib.app import Crib
from crib.codeindex import SymbolIndex, _locate
from crib.config import Config, CribLink
from crib.paths import Paths
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def _sym(crib, project, fqname, file, description="", **over):
    e = {"fqname": fqname, "name": fqname.split(".")[-1], "kind": "function",
         "lang": "python", "module": fqname.rsplit(".", 1)[0], "parent": "",
         "content_hash": f"h_{fqname}", "file": file, "line": 1,
         "signature": f"def {fqname.split('.')[-1]}():",
         "description": description, "container": [], "calls": [],
         "called_by": [], "references": [],
         "name_terms": [fqname.split(".")[-1]], **over}
    SymbolIndex(crib.paths.project_dir(project)).write(e)
    return e


def _two_projects(crib, tmp_path):
    """parent project `par` (refs: [dep]) + ref project `dep`, each with a
    local checkout root recorded machine-locally via .source_root."""
    par_root = tmp_path / "checkouts" / "par"
    dep_root = tmp_path / "elsewhere" / "dep"       # deliberately different tree
    par_root.mkdir(parents=True)
    (dep_root / "src").mkdir(parents=True)
    (par_root / ".crib").write_text("project: par\nrefs:\n  - dep\n")
    (dep_root / ".crib").write_text("project: dep\n")
    # real source files — the lazy revalidation gate drops symbols whose
    # source is missing, so seeded indexes need their files to exist
    (par_root / "app.py").write_text("def main(): pass\n")
    (dep_root / "src" / "util.py").write_text("def helper(): pass\n")
    _sym(crib, "par", "app.main", "app.py", "parent entry point",
         calls=["helper [dep:src/util.py]"])
    _sym(crib, "dep", "util.helper", "src/util.py", "shared helper routine")
    SymbolIndex(crib.paths.project_dir("par")).set_source_root(par_root)
    SymbolIndex(crib.paths.project_dir("dep")).set_source_root(dep_root)
    return par_root, dep_root


# --- .crib schema + per-machine root resolution -------------------------------

def test_criblink_parses_refs(tmp_path):
    (tmp_path / ".crib").write_text("project: par\nrefs:\n  - dep\n  - other\n")
    link = CribLink.find(tmp_path)
    assert link and link.refs == ["dep", "other"]


def test_project_refs_resolve_local_roots(crib, tmp_path):
    _, dep_root = _two_projects(crib, tmp_path)
    refs = crib._project_refs("par")
    assert refs == [{"project": "dep", "root": dep_root, "indexed": True}]


# --- query-time fan-out --------------------------------------------------------

def test_xref_falls_through_to_ref_project(crib, tmp_path):
    _two_projects(crib, tmp_path)
    hits = crib.code_xref("util.helper", project="par")     # not in par
    assert hits and hits[0]["project"] == "dep"
    assert hits[0]["fqname"] == "util.helper"


def test_lookup_fans_out_and_tags_projects(crib, tmp_path):
    _two_projects(crib, tmp_path)
    hits = crib.code_lookup("helper", project="par", k=5)
    projects = {h["fqname"]: h["project"] for h in hits}
    assert projects.get("util.helper") == "dep"              # ref hit surfaced


def test_dossier_crosses_into_ref_and_annotates_edges(crib, tmp_path):
    _two_projects(crib, tmp_path)
    d = crib.code_dossier("app.main", project="par")
    assert d["project"] == "par"
    call = d["calls"][0]
    assert call["project"] == "dep"                          # qualified edge
    assert call["symbol"] == "util.helper"
    assert call["description"] == "shared helper routine"
    # and resolving a ref-owned symbol directly crosses over
    d2 = crib.code_dossier("util.helper", project="par")
    assert d2["project"] == "dep"


def test_graph_hops_across_qualified_edges(crib, tmp_path):
    _two_projects(crib, tmp_path)
    tree = crib.code_graph("app.main", project="par")
    child = tree["children"][0]
    assert child["fqname"] == "util.helper" and child["project"] == "dep"


# --- index-time attribution (_locate) ------------------------------------------

def test_locate_attributes_out_of_root_uris(tmp_path):
    root = tmp_path / "par"
    dep_root = tmp_path / "dep"
    (dep_root / "src").mkdir(parents=True)
    (dep_root / "src" / "util.py").write_text("def helper(): pass\n")
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "app.py").write_text("x = 1\n")
    refs = [("dep", dep_root.resolve(), frozenset({"src/util.py"}))]
    # in-workspace → plain rel
    assert _locate((root / "sub" / "app.py").as_uri(), root, refs) == "sub/app.py"
    # under the ref's local root → qualified
    assert _locate((dep_root / "src" / "util.py").as_uri(), root, refs) \
        == "dep:src/util.py"
    # site-packages install → suffix match against the ref's indexed files
    sp = tmp_path / "venv" / "lib" / "site-packages" / "src" / "util.py"
    sp.parent.mkdir(parents=True)
    sp.write_text("def helper(): pass\n")
    assert _locate(sp.as_uri(), root, refs) == "dep:src/util.py"
    # neither → dropped, as before
    other = tmp_path / "unrelated.py"
    other.write_text("y = 2\n")
    assert _locate(other.as_uri(), root, refs) is None


# --- project boundaries (nested .crib) ------------------------------------------

def test_enumeration_stops_at_nested_crib(crib, tmp_path):
    root = tmp_path / "repo"
    (root / "vendor" / "dep").mkdir(parents=True)
    (root / "a.py").write_text("def a(): pass\n")
    (root / "vendor" / "dep" / "b.py").write_text("def b(): pass\n")
    (root / "vendor" / "dep" / ".crib").write_text("project: dep\n")
    files = crib._enumerate_code_files(root, ["**/*.py"])
    names = {str(f.relative_to(root)) for f in files}
    assert "a.py" in names
    assert "vendor/dep/b.py" not in names        # bounded by the nested .crib


def test_single_file_index_skips_nested_project(crib, tmp_path):
    root = tmp_path / "repo"
    (root / "vendor" / "dep").mkdir(parents=True)
    (root / ".crib").write_text("project: par\n")
    (root / "vendor" / "dep" / ".crib").write_text("project: dep\n")
    (root / "vendor" / "dep" / "b.py").write_text("def b(): pass\n")
    out = crib._index_file_sync(root, "vendor/dep/b.py", "par", True)
    assert "nested project 'dep'" in out.get("skipped", "")
