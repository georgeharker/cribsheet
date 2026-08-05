"""Source attribution + the import tier (docs/plans/design-plan-import.md).

Two claims here, and both are about what a graph entry is allowed to forget.

SOURCES CHECK, AT SECTION GRANULARITY. An entry records the hash of the doc
section it was drawn from, so editing THAT section taints it and editing anything
else in the same doc does not — `test_a_source_checks_its_own_section_only` is
that pair, and it is the whole reason a source cites a section rather than a
file. They never gate, though: `test_a_changed_source_never_blocks_work` is the
line between the two edge families.

PROPOSED IS QUARANTINE. An extracted decision taints nothing (it has no authority
to spread) yet BLOCKS plan items that depend on it (unpromoted ground is unstable
ground) — the two halves of `test_proposed_dep_rule_matrix`, which is the rule
matrix in one place because the two look contradictory until you see them
together.
"""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.store import InMemoryStore

DOC = """\
# Spec

Preamble prose, no heading of its own.

## 4. Coordination

One writer at a time; the hash gate makes a re-run cheap.

## 10. Stack

Chroma, bge-small, a BM25 sidecar.

### 10.3 Fusion

Dense-dominant fusion, BM25 folded in under a coverage gate.
"""


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
    run(crib.store_note(DOC, title="Spec", project="p"))
    return crib


def run(coro):
    return asyncio.run(coro)


def _rewrite(crib, old: str, new: str, relpath: str = "spec.md") -> None:
    """Edit the doc by the ORDINARY note verb — sources must check against the
    file as it reads, not against anything the facet was told."""
    body = crib.read_note(relpath, project="p")
    assert old in body
    run(crib.edit_note(relpath, body.replace(old, new), project="p"))


def _kinds(crib) -> list[str]:
    return [c["change_kind"] for r in crib.design_check(project="p")["tainted"]
            for c in r["causes"]]


# ── the citation resolves to exactly one section ──────────────────────────────

def test_a_source_resolves_by_unique_heading_suffix(crib):
    out = run(crib.design_add("Single writer", "one writer", project="p",
                              sources=["spec.md#4. Coordination"]))
    # the SUFFIX is what you type; the full breadcrumb is what gets recorded, so
    # the citation still reads like the doc after someone re-nests a heading
    assert out["sources"] == ["spec.md#Spec/4. Coordination"]
    assert crib.design_read("Single writer", project="p")["sources"] == [
        {"ref": "spec.md", "heading": "Spec/4. Coordination",
         "label": "spec.md#Spec/4. Coordination", "state": "ok"}]


def test_an_ambiguous_or_absent_heading_lists_the_candidates(crib):
    with pytest.raises(ValueError, match="no section of spec.md matches"):
        run(crib.design_add("D", "x", project="p", sources=["spec.md#Nonesuch"]))
    # "10." prefixes two sections — refuse rather than pick one
    with pytest.raises(ValueError, match="ambiguous source heading"):
        run(crib.design_add("D", "x", project="p", sources=["spec.md#10."]))
    with pytest.raises(ValueError, match="no doc matches"):
        run(crib.design_add("D", "x", project="p", sources=["nope.md#4."]))


def test_a_source_must_name_a_section_not_a_whole_doc(crib):
    """The data-model rule: whole-file attribution would re-check an entry on any
    edit anywhere in the file, which is the noise section granularity exists to
    prevent. The error hands back the headings to choose from."""
    with pytest.raises(ValueError, match="has headings, so cite the SECTION") as e:
        run(crib.design_add("D", "x", project="p", sources=["spec.md"]))
    assert "Spec/4. Coordination" in str(e.value)


def test_a_headingless_doc_is_its_own_section(crib):
    """The one exception, and it is not really one: with no headings the whole
    body IS the section the chunker hashes, so citing the doc cites that."""
    run(crib.store_note("Flat prose, no headings at all.", title="Flat",
                        project="p"))
    out = run(crib.design_add("From the flat doc", "x", project="p",
                              sources=["flat.md"]))
    assert out["sources"] == ["flat.md"]
    assert crib.design_check(project="p")["clean"]

    _rewrite(crib, "Flat prose", "Flatter prose", relpath="flat.md")
    assert _kinds(crib) == ["source-changed"]


# ── sources check (and only their own section) ────────────────────────────────

def test_a_source_checks_its_own_section_only(crib):
    """Edit the cited section → tainted, with the doc + heading IN THE CHAIN.
    Edit elsewhere in the same doc → still clean."""
    run(crib.design_add("Single writer", "one writer at a time", project="p",
                        sources=["spec.md#4. Coordination"]))
    assert crib.design_check(project="p")["clean"]

    _rewrite(crib, "Dense-dominant fusion", "Sparse-dominant fusion")
    assert crib.design_check(project="p")["clean"], "another section is not this one"

    _rewrite(crib, "One writer at a time", "Two writers, locked")
    row = crib.design_check(project="p")["tainted"][0]
    cause = row["causes"][0]
    assert cause["change_kind"] == "source-changed"
    assert cause["source"] == "spec.md" and cause["heading"] == "Spec/4. Coordination"
    # the chain ends at what actually moved — the section, not the decision
    assert row["paths"][0]["chain"] == ["Single writer",
                                        "spec.md#Spec/4. Coordination"]
    assert "design_reaffirm design/single-writer.md" in row["next"]
    assert "never gates" in row["next"]


def test_a_renamed_heading_is_its_own_change_kind(crib):
    run(crib.design_add("Single writer", "one writer", project="p",
                        sources=["spec.md#4. Coordination"]))
    _rewrite(crib, "## 4. Coordination", "## 4. Coordination model")
    row = crib.design_check(project="p")["tainted"][0]
    assert row["causes"][0]["change_kind"] == "source-missing"
    assert crib.design_read("Single writer", project="p")["sources"][0]["state"] \
        == "missing"


def test_reaffirm_re_records_source_hashes_alongside_dep_hashes(crib):
    run(crib.design_add("Ground", "the ground", project="p"))
    run(crib.design_add("Single writer", "one writer", deps=["Ground"], project="p",
                        sources=["spec.md#4. Coordination"]))
    _rewrite(crib, "One writer at a time", "Two writers, locked")
    run(crib.design_edit("Ground", "the ground moved", project="p"))
    assert sorted(_kinds(crib)) == ["dep-edited", "source-changed"]

    out = run(crib.design_reaffirm("Single writer", project="p"))
    assert out["sources"] == ["spec.md#Spec/4. Coordination"]
    assert out["missing_sources"] == [] and out["verified"]
    assert crib.design_check(project="p")["clean"]        # BOTH families cleared


def test_reaffirm_does_not_paper_over_a_vanished_section(crib):
    run(crib.design_add("Single writer", "one writer", project="p",
                        sources=["spec.md#4. Coordination"]))
    _rewrite(crib, "## 4. Coordination", "## 4. Coordination model")
    out = run(crib.design_reaffirm("Single writer", project="p"))
    assert out["missing_sources"] == ["spec.md#Spec/4. Coordination"]
    assert _kinds(crib) == ["source-missing"], "still flagged, not silently blessed"


def test_design_edit_can_restate_the_citations(crib):
    run(crib.design_add("Single writer", "one writer", project="p",
                        sources=["spec.md#4. Coordination"]))
    run(crib.design_edit("Single writer", "actually about fusion", project="p",
                         sources=["spec.md#10.3 Fusion"]))
    assert [s["label"] for s in
            crib.design_read("Single writer", project="p")["sources"]] == [
        "spec.md#Spec/10. Stack/10.3 Fusion"]


def test_sources_check_against_the_file_by_any_route(crib):
    """An unindexed target is split with the same chunker the indexer runs, so a
    doc crib has never seen is citable and checks identically. (The dogfood path:
    the live store holds DESIGN.md's chunks, a fresh store does not.)"""
    root = crib.notestore.notes_root("p")
    (root / "loose.md").write_text("# Loose\n\n## Only\n\noriginal text.\n")
    out = run(crib.design_add("From an unindexed doc", "x", project="p",
                              sources=["loose.md#Only"]))
    assert out["sources"] == ["loose.md#Loose/Only"]
    assert crib.design_check(project="p")["clean"]
    (root / "loose.md").write_text("# Loose\n\n## Only\n\nrewritten text.\n")
    assert _kinds(crib) == ["source-changed"]


# ── proposed: the import tier ─────────────────────────────────────────────────

def test_proposed_dep_rule_matrix(crib):
    """What a `proposed` decision does to the graph around it — the two halves
    that look contradictory apart and are one rule together: it has no authority
    to SPREAD (taints nothing) and no authority to BUILD ON (gates work)."""
    run(crib.design_add("Extracted", "from the doc", project="p", proposed=True))
    run(crib.design_add("Built on it", "rests on the extracted one",
                        deps=["Extracted"], project="p"))
    run(crib.plan_add("do the work", deps=["Extracted"], project="p"))

    # 1. a proposed dep never taints its dependents, not even as a new edge
    assert crib.design_check(project="p")["clean"]
    run(crib.design_edit("Extracted", "from the doc, restated", project="p"))
    assert crib.design_check(project="p")["clean"]

    # 2. …and it DOES gate the plan item: unpromoted ground is unstable ground
    row = crib.plan_list(project="p")["items"][0]
    assert row["blocked"] and row["blocked_by"][0]["status"].startswith("proposed")
    assert crib.plan_next(project="p")["items"] == []

    # 3. promotion is what changes both: the item frees, the edges start checking
    run(crib.design_promote("Extracted", project="p"))
    assert [r["title"] for r in crib.plan_next(project="p")["items"]] == ["do the work"]
    run(crib.design_edit("Extracted", "restated again", project="p"))
    assert [r["title"] for r in crib.design_check(project="p")["tainted"]] \
        == ["Built on it"]


def test_promote_seeds_checked_fresh_and_refuses_a_non_proposal(crib):
    run(crib.design_add("Ground", "the ground", project="p"))
    run(crib.design_add("Extracted", "on the ground", deps=["Ground"], project="p",
                        proposed=True, sources=["spec.md#10.3 Fusion"]))
    _rewrite(crib, "Dense-dominant fusion", "Dense-dominant fusion, tuned")
    run(crib.design_edit("Ground", "the ground moved", project="p"))
    assert crib.design_check(project="p")["tainted"], "a proposal checks its OWN edges"

    out = run(crib.design_promote("Extracted", project="p"))
    assert out["status"] == "active" and out["sources"]
    # seeded fresh against the graph AND the docs as they read at promotion
    assert crib.design_check(project="p")["clean"]
    with pytest.raises(ValueError, match="already active"):
        run(crib.design_promote("Extracted", project="p"))


def test_a_proposal_is_listed_distinctly_and_queued_for_promotion(crib):
    run(crib.design_add("Extracted", "from the doc", project="p", proposed=True,
                        sources=["spec.md#4. Coordination"]))
    listed = crib.design_list(project="p")
    assert listed["proposed"] == 1 and listed["designs"][0]["status"] == "proposed"
    assert listed["designs"][0]["sources"] == 1
    check = crib.design_check(project="p")
    assert check["clean"] and [p["title"] for p in check["proposed"]] == ["Extracted"]
    assert "design_promote" in check["proposed"][0]["next"]
    assert "design_promote" in crib.design_read("Extracted", project="p")["next"]
    assert crib.design_tree(project="p")["roots"][0]["status"] == "proposed"


def test_design_add_still_lands_active(crib):
    """Only EXTRACTION quarantines — hand-authoring is already a human judgement."""
    out = run(crib.design_add("Hand written", "decided just now", project="p"))
    assert out["status"] == "active"


# ── plan items: sources report, they never re-open ────────────────────────────

def test_a_changed_source_never_blocks_work(crib):
    """The line between the families: a dep gates, a source doesn't. The decision
    is tainted for re-reading and the work built on it stays actionable."""
    run(crib.design_add("Fusion", "dense dominant", project="p",
                        sources=["spec.md#10.3 Fusion"]))
    run(crib.plan_add("tune the fusion", deps=["Fusion"], project="p"))
    _rewrite(crib, "Dense-dominant fusion", "Sparse-dominant fusion")

    assert _kinds(crib) == ["source-changed"]              # checked…
    row = crib.plan_list(project="p")["items"][0]
    assert not row["blocked"] and row["blocked_by"] == []   # …but never gated
    assert [r["title"] for r in crib.plan_next(project="p")["items"]] \
        == ["tune the fusion"]


def test_a_finished_item_flags_revisit_rather_than_re_opening(crib):
    run(crib.plan_add("wire the fusion", project="p",
                      sources=["spec.md#10.3 Fusion"]))
    run(crib.plan_status("wire the fusion", "done", project="p"))
    assert crib.plan_list(all=True, project="p")["revisit"] == 0

    _rewrite(crib, "Dense-dominant fusion", "Sparse-dominant fusion")
    listed = crib.plan_list(all=True, project="p")
    row = listed["items"][0]
    assert listed["revisit"] == 1 and row["status"] == "done"   # status untouched
    assert "changed since this was drawn from it" in row["revisit"][0]
    assert "plan_status plans/wire-the-fusion.md todo" in row["next"]

    # …and re-asserting the status is what clears it: a status write re-records
    # the sources, so "I looked, it's still done" needs no verb of its own
    run(crib.plan_status("wire the fusion", "done", project="p"))
    assert crib.plan_list(all=True, project="p")["revisit"] == 0


def test_a_batch_item_carries_its_own_sources(crib):
    out = run(crib.plan_add(items=[
        {"title": "coordination work", "sources": ["spec.md#4. Coordination"]},
        {"title": "fusion work", "deps": ["#1"],
         "sources": ["spec.md#10.3 Fusion"]}], project="p"))
    assert [r["sources"] for r in out["items"]] == [
        ["spec.md#Spec/4. Coordination"], ["spec.md#Spec/10. Stack/10.3 Fusion"]]


# ── import: sections + hashes + citations + the procedure ─────────────────────

def test_import_returns_sections_hashes_citations_and_the_procedure(crib):
    run(crib.design_add("Single writer", "one writer", project="p",
                        sources=["spec.md#4. Coordination"]))
    out = crib.design_import("spec.md", project="p")

    assert out["relpath"] == "spec.md" and out["path"].endswith("/spec.md")
    by_source = {s["source"]: s for s in out["sections"]}
    assert list(by_source) == ["spec.md#Spec",           # document order
                               "spec.md#Spec/4. Coordination",
                               "spec.md#Spec/10. Stack",
                               "spec.md#Spec/10. Stack/10.3 Fusion"]
    # every section arrives with the hash it currently has, so the `source`
    # string can be cited VERBATIM and the citation is correct by construction
    cited = by_source["spec.md#Spec/4. Coordination"]
    assert cited["section_hash"] and cited["words"] == 12
    assert cited["preview"].startswith("One writer at a time")

    # …and what already draws on this doc, so a second import extends the graph
    assert [(e["title"], e["cites"]) for e in out["existing"]] == [
        ("Single writer", ["spec.md#Spec/4. Coordination"])]

    proc = out["instruction"]
    assert "note_read spec.md" in proc and "runs no model" not in proc
    for step in ("design_lookup", "sources", "proposed=True", "design_tree",
                 "design_promote"):
        assert step in proc, step
    assert "Never cite the doc as a whole" in proc


def test_import_writes_nothing(crib):
    before = crib.design_list(project="p")["total"]
    crib.design_import("spec.md", project="p")
    assert crib.design_list(project="p")["total"] == before


def test_plan_import_carries_the_plan_procedure(crib):
    out = crib.plan_import("spec.md", project="p")
    assert out["kind"] == "plan" and out["existing"] == []
    proc = out["instruction"]
    assert "plan_add(items=[...])" in proc and '"#1"' in proc
    assert "plan_list" in proc and "revisit" in proc


def test_import_resolves_a_doc_by_path_suffix_or_errors(crib):
    """A doc may be named by its bare filename — how you'd refer to a doc indexed
    in situ (`DESIGN.md` → `sources/<repo>/DESIGN.md`) — as long as that is
    unambiguous. An exact relpath always wins over the suffix search."""
    root = crib.notestore.notes_root("p")
    for sub in ("one", "two"):
        (root / sub).mkdir()
        (root / sub / "deep.md").write_text(f"# {sub}\n\n## Bit\n\nx\n")
    assert crib.design_import("one/deep.md", project="p")["relpath"] == "one/deep.md"
    with pytest.raises(ValueError, match="ambiguous doc"):
        crib.design_import("deep.md", project="p")
    with pytest.raises(ValueError, match="no doc matches"):
        crib.design_import("missing.md", project="p")

    (root / "deep.md").write_text("# top\n\n## Bit\n\nx\n")
    assert crib.design_import("deep.md", project="p")["relpath"] == "deep.md"
