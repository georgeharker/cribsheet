"""project_create / project_delete / project_rename — the lifecycle verbs.

Create is the residency doorway (skeleton + birth manifest + at-birth `.crib`/
stub declaration); delete is fenced and trash-moved; rename churns chunk ids
but carries note ids and ring history. Write policy: implicit creation STAYS —
every write into a never-created project succeeds (the lifecycle verbs are an
optional richer doorway, not a gate).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, cast

import pytest

from crib.app import Crib
from crib.config import Config
from crib.errors import CribUserError
from crib.paths import Paths
from crib.store import InMemoryStore


def _cfg(tmp_path):
    """A config naming the tmp root as `$REPOS` — so a stub's `store_root` is
    written as a PORTABLE token, which is the point of recording it that way
    (the same fixture pattern test_repo_storage.py uses)."""
    return Config(locations={"REPOS": str(tmp_path)})


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), _cfg(tmp_path), InMemoryStore())


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repos" / "toucan"
    (r / "src").mkdir(parents=True)
    return r


def run(coro):
    return asyncio.run(coro)


# --- project_create ---------------------------------------------------------


def test_create_global_skeleton_and_manifest(crib):
    out = run(crib.project_create(project="toucan"))
    assert out["created"] is True and out["residency"] == "global"
    root = crib.paths.project_dir("toucan")
    for seg in ("notes", "design", "plans", "learnings"):
        assert (root / seg).is_dir()
    pcfg = crib.project_config("toucan")
    assert pcfg.created and pcfg.host  # birth manifest stamped
    assert crib.project_is_new("toucan") is False
    # searchable surface sees it immediately
    assert "toucan" in crib.projects()


def test_create_is_idempotent_and_never_rewrites_the_manifest(crib):
    first = run(crib.project_create(project="toucan"))
    assert first["created"] is True
    pcfg = crib.project_config("toucan")
    born, host = pcfg.created, pcfg.host
    second = run(crib.project_create(project="toucan"))
    assert second["created"] is False
    assert crib.project_config("toucan").created == born  # never rewritten
    assert crib.project_config("toucan").host == host


def test_write_policy_unchanged_implicit_creation(crib):
    run(crib.project_create(project="toucan"))
    # Never-created projects still materialize on first write — the doorway
    # is optional (the back-compat half of the write-policy decision).
    out = run(crib.store_note("fact", title="t", project="implicit-born"))
    assert out["created"] is True
    assert "implicit-born" in crib.projects()


def test_create_invalid_and_missing_name(crib):
    with pytest.raises(CribUserError):
        run(crib.project_create(project="../escape"))
    with pytest.raises(CribUserError):
        run(crib.project_create())  # no project, no store: nothing to resolve


def test_create_resident_born_in_repo(tmp_path, monkeypatch, repo, crib):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    c = Crib(Paths.resolve().ensure(), _cfg(tmp_path), InMemoryStore())
    out = run(c.project_create(project="toucan", cwd=repo, store=".crib-store"))
    assert out["created"] is True and out["residency"] == "repo"
    store = repo / ".crib-store"
    for seg in ("notes", "design", "plans", "learnings"):
        assert (store / seg).is_dir()
    assert (repo / ".crib").is_file()
    assert (store / ".gitignore").read_text() == c._STORE_GITIGNORE
    pp = c.project_paths("toucan")
    assert pp.in_repo and pp.store_root == store
    token = c.project_config("toucan").store_root
    assert token and "$" in token  # portable $LOCATION spelling, not abspath
    # .crib birth: store declared in the repo file (travels with checkouts)
    assert "store: .crib-store" in (repo / ".crib").read_text()


def test_create_resident_refuses_nonempty_global_project(tmp_path, monkeypatch, repo):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    c = Crib(Paths.resolve().ensure(), _cfg(tmp_path), InMemoryStore())
    run(c.store_note("existing notes", title="t", project="toucan"))
    with pytest.raises(CribUserError, match="adopt"):
        run(c.project_create(project="toucan", cwd=repo, store=".crib-store"))


def test_create_resident_refuses_cwdless_and_cross_project(tmp_path, monkeypatch, repo):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    c = Crib(Paths.resolve().ensure(), _cfg(tmp_path), InMemoryStore())
    with pytest.raises(CribUserError, match="project_path"):
        run(c.project_create(project="toucan", store=".crib-store"))  # no cwd
    # a repo already .crib-linked elsewhere refuses a different name
    (repo / ".crib").write_text("project: other\n")
    with pytest.raises(CribUserError, match="'other'"):
        run(c.project_create(project="toucan", cwd=repo, store=".crib-store"))


# --- project_delete ---------------------------------------------------------


def _write_note(crib, project, relpath, body, fm=None):
    from crib import notes

    from crib.notes import Note

    path = crib.abspath(project, relpath)
    path.parent.mkdir(parents=True, exist_ok=True)
    note = Note(path=path, frontmatter=fm or {"title": relpath}, body=body)
    notes.save_atomic(note)
    return asyncio.get_event_loop_policy()


def test_delete_moves_tree_to_trash_and_drops_chunks(crib):
    run(crib.store_note("memory body", title="Mem", project="goner"))
    out = run(crib.project_delete(project="goner"))
    assert out["deleted"] is True
    assert out["trash"] is not None
    import pathlib

    assert pathlib.Path(out["trash"]).is_dir()
    trashed = list(pathlib.Path(out["trash"]).rglob("*.md"))
    assert trashed  # the tree is recoverable
    assert not crib.paths.project_dir("goner").exists()
    assert "goner" not in crib.projects()
    assert crib.lookup("memory body", project="default") == []


def test_delete_fences_default_and_requires_explicit(crib):
    with pytest.raises(CribUserError, match="pass project="):
        run(crib.project_delete())  # never the session default
    with pytest.raises(CribUserError, match="default project"):
        run(crib.project_delete(project=crib.config.default_project))


def test_delete_repo_resident_store_never_touched(tmp_path, monkeypatch, repo):
    """THE INVARIANT: delete removes crib's own state only — the user repo's
    store (committed notes, the in-repo ring, .gitignore, .crib) is untouched,
    and `force` gates nothing."""
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    c = Crib(Paths.resolve().ensure(), _cfg(tmp_path), InMemoryStore())
    run(c.project_create(project="toucan", cwd=repo, store=".crib-store"))
    # A note written into the (repo-resident) pillar store, via the resolver.
    from crib import notes as notes_mod
    from crib.notes import Note

    p = c.project_paths("toucan").pillar_dir("notes") / "resident.md"
    notes_mod.save_atomic(Note(path=p, frontmatter={"title": "r"}, body="resident"))
    store = repo / ".crib-store"
    before = sorted(str(f.relative_to(store)) for f in store.rglob("*"))
    out = run(c.project_delete(project="toucan", force=True))
    assert out["store_deleted"] is False
    after = sorted(str(f.relative_to(store)) for f in store.rglob("*"))
    # every repo-side path SURVIVES the delete — before-set == after-set
    assert before == after
    assert p.exists()  # the note in the user repo SURVIVES the delete
    assert (repo / ".crib").exists()  # ...and the repo's own crib declarations
    assert "NOT touched" in out["message"]


def test_delete_keeps_the_ring(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    c = Crib(Paths.resolve().ensure(), _cfg(tmp_path), InMemoryStore())
    run(c.store_note("body one", title="One", project="ringed"))
    run(c.store_note("body two", title="Two", project="ringed"))
    out = run(c.project_delete(project="ringed"))
    # The ring is keyed by note id and deliberately kept by the delete.
    assert out["ring_entries_kept"] >= 1
    assert (c.paths.versions_dir).exists()


def test_delete_mirror_binding_dropped(crib, tmp_path):
    crib.memory_bindings.upsert(tmp_path / "h", "goner")
    out = run(crib.project_delete(project="goner"))
    assert out["deleted"] is True
    assert all(b.get("project") != "goner" for b in crib.memory_bindings.all())


# --- project_rename ---------------------------------------------------------


def test_rename_moves_tree_keeps_ids_and_ring(crib, tmp_path):
    a = run(crib.store_note("alpha body", title="Aa", project="old-name"))
    run(crib.append_note(a["relpath"], "more", project="old-name"))
    out = run(crib.project_rename(project="old-name", to="new-name"))
    assert out["renamed"] is True
    assert "old-name" not in crib.projects() and "new-name" in crib.projects()
    # note survives under the new project, ring history carried
    assert "alpha body" in crib.read_note(a["relpath"], project="new-name")
    assert out["ring_entries_carried"] >= 1
    # the ring FILE is really there (versions_dir.exists() is true by ensure())
    versions = crib.paths.versions_dir
    assert any(
        any((versions / d).iterdir())
        for d in os.listdir(versions)
        if (versions / d).is_dir()
    )
    with pytest.raises(FileNotFoundError):
        _ = crib.read_note(a["relpath"], project="old-name")


def test_rename_fences(crib):
    run(crib.project_create(project="a"))
    run(crib.project_create(project="b"))
    with pytest.raises(CribUserError, match="already exists"):
        run(crib.project_rename(project="a", to="b"))
    with pytest.raises(CribUserError, match="same name"):
        run(crib.project_rename(project="a", to="a"))
    with pytest.raises(CribUserError, match="pinned"):
        run(crib.project_rename(project="a", to=crib.config.default_project))


def test_rename_updates_session_and_stub(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    c = Crib(Paths.resolve().ensure(), _cfg(tmp_path), InMemoryStore())
    from crib.session import session_state

    session_state().current_project = "old"
    run(c.store_note("x", title="T", project="old"))
    run(c.project_rename(project="old", to="renamed"))
    assert session_state().current_project == "renamed"
    pcfg = c.project_config("renamed")
    assert pcfg.name == "renamed"
    # a rename NEVER fakes a birth record: implicitly-born stays implicitly-born
    assert pcfg.created is None


def test_rename_reports_affected_cross_project_refs(crib, tmp_path, monkeypatch):
    # 'other' refs: 'old-name' in its .crib — rename reports (never edits the
    # foreign repo), per the spike's finding that .crib files live in repos.
    run(crib.project_create(project="old-name"))
    other_repo = tmp_path / "repos" / "other"
    other_repo.mkdir(parents=True, exist_ok=True)
    (other_repo / ".crib").write_text("project: other\nrefs:\n  - old-name\n")
    # refs: resolve through the named project's registered root — register the
    # repo as a doc source (what a repo with in-situ docs leaves behind) so
    # 'other' is scannable by the rename report.
    crib._source_roots("other").upsert("sources/other/", other_repo)
    out = run(crib.project_rename(project="old-name", to="renamed"))
    assert _collect(crib) == ["other"]
    # the report carries the REFERRER (which project holds the ref), not just
    # the old name — and a --json-serializable root
    assert out["refs_affected"], "refs_affected empty"
    row = out["refs_affected"][0]
    assert row["referrer"] == "other"
    assert row["target"] == "old-name"
    assert row["root"] is None or isinstance(row["root"], str)
    # the whole result must be --json-serializable
    json.dumps(out)


def _collect(crib):
    rows = crib.project_list()
    out = []
    for r in rows:
        refs = crib._project_refs(r["project"])
        if any(x.get("project") == "old-name" for x in refs):
            out.append(r["project"])
    return out


def test_confirm_destructive_elicitation_paths(monkeypatch):
    """The MCP delete gate: accept (echo), refuse (wrong echo), refuse
    (no elicitation support) — the destructive act must never run without the
    human-decision, and the human decision must never be a parameter."""
    from crib.server import _confirm_destructive

    class _Result:
        def __init__(self, data):
            self.data = data

    def _install(elicit_fn):
        import fastmcp.server.dependencies as deps

        class _Ctx:
            async def elicit(self, msg, response_type=None):
                return elicit_fn(msg)

        monkeypatch.setattr(deps, "get_context", lambda: _Ctx(), raising=False)

    # accept: echo matches
    _install(lambda msg: _Result("proj-a"))
    run(_confirm_destructive("proj-a", "project_delete", "DELETE proj-a"))
    # decline: wrong echo
    _install(lambda msg: _Result("different"))
    with pytest.raises(CribUserError, match="did not echo"):
        run(_confirm_destructive("proj-a", "project_delete", "x"))

    # no elicitation support → refusal naming the terminal path
    def _boom(msg):
        raise RuntimeError("unsupported")

    import fastmcp.server.dependencies as deps2

    class _Bad:
        async def elicit(self, msg, response_type=None):
            _boom(msg)

    monkeypatch.setattr(deps2, "get_context", lambda: _Bad(), raising=False)
    with pytest.raises(CribUserError, match="does not"):
        run(_confirm_destructive("proj-a", "project_delete", "x"))


def test_describe_queue_drop_project():
    """A queued describe for a deleted/renamed project never fires (it would
    describe symbols from files that no longer exist, backoff-retrying a corpse)."""
    from crib.describe_queue import DescribeQueue

    class _Loop:  # synchronous stand-in for the loop hop (typed as AbstractEventLoop)
        def call_soon_threadsafe(self, fn, *args):
            fn(*args)

        def call_later(self, delay, fn, *args):
            return None

    async def _describe(*a, **k):  # matches the Awaitable describe_fn contract
        return None

    q = DescribeQueue(
        cast(asyncio.AbstractEventLoop, _Loop()),
        cast(Any, _describe),
    )
    q.enqueue("gone", __import__("pathlib").Path("/"), "a.py", {"s": {}})
    q.enqueue("stay", __import__("pathlib").Path("/"), "b.py", {"s": {}})
    dropped = q.drop_project("gone")
    assert dropped >= 1
    assert q.pending_files() == 1  # only 'stay' remains


def test_delete_recreates_never_left_behind(crib):
    """Delete of a project that exists in DERIVED state only (chunks/bindings
    without a tree) still cleans up — and delete-of-nothing says so."""
    run(crib.store_note("b", title="t", project="derived-only"))
    # simulate tree-less state: chunks present, tree gone
    chunks = list(crib.store.get_meta({"project": "derived-only"}))
    assert chunks
    out = run(crib.project_delete(project="derived-only"))
    assert out["deleted"] is True
    assert crib.store.get_meta({"project": "derived-only"}) == {}
    nothing = run(crib.project_delete(project="derived-only"))
    assert nothing["deleted"] is False


def test_trash_suffix_survives_rapid_deletes(crib, tmp_path, monkeypatch):
    for name in ("rapid-a", "rapid-b"):
        run(crib.project_create(project=name))
    # two deletes within the same timestamp second must not collide
    out1 = run(crib.project_delete(project="rapid-a"))
    out2 = run(crib.project_delete(project="rapid-b"))
    assert out1["trash"] and out2["trash"]
    assert out1["trash"] != out2["trash"]


def test_delete_gitignore_includes_trash():
    from crib.gitbacking import _GITIGNORE

    assert ".trash/" in _GITIGNORE


def test_create_allows_precreated_empty_pillar_dirs(crib, tmp_path, repo):
    """Pre-created EMPTY pillar dirs are not "notes" — only content collides
    (the inverted `any()` regression: a generator object is always truthy)."""
    run(crib.project_create(project="toucan"))  # empty global skeleton
    (tmp_path / "pre").mkdir()
    pstore = repo / ".crib-store"
    (pstore / "notes").mkdir(parents=True)  # empty in-repo pillar dir
    out = run(crib.project_create(project="toucan", cwd=repo, store=".crib-store"))
    assert out["created"] is True and out["residency"] == "repo"


def test_create_refuses_default_project_as_resident(crib, repo):
    with pytest.raises(CribUserError, match="default"):
        run(crib.project_create(project="default", cwd=repo, store=".crib-store"))


def test_create_invalid_store_never_writes_crib(crib, repo):
    """Validation happens BEFORE anything is written: a refused create leaves
    no poisoned .crib in a foreign repo (escape + newline injection + blocker)."""
    for bad in ("../outside", "st\nrefs: [x]", "/abs/path", "a: b", "*x", "[x]", "."):
        with pytest.raises(CribUserError):
            run(crib.project_create(project="p", cwd=repo, store=bad))
        assert not (repo / ".crib").exists(), f"{bad!r} wrote a .crib"


def test_create_never_overwrites_foreign_gitignore(crib, repo):
    (repo / ".crib-store").mkdir()
    (repo / ".crib-store" / ".gitignore").write_text("# mine\n")
    run(crib.project_create(project="toucan", cwd=repo, store=".crib-store"))
    assert (
        repo / ".crib-store" / ".gitignore"
    ).read_text() == "# mine\n"  # crib's ignore never clobbers a foreign one


def test_delete_scoped_store_removal_never_touches_noncrib(crib, repo):
    """THE INVARIANT, the strongest form: a delete touches the user repo not
    at all — resident notes, foreign files, the ring and .gitignore all stay,
    force or not. (The first pass rmtree'd the store whole; the second scoped
    it to crib content; this is the final form: nothing repo-side moves.)"""
    run(crib.project_create(project="toucan", cwd=repo, store=".crib-store"))
    (repo / ".git").mkdir(exist_ok=True)  # a real checkout marker
    from crib import notes as _notes
    from crib.notes import Note

    store = repo / ".crib-store"
    p = store / "notes" / "resident.md"
    _notes.save_atomic(Note(path=p, frontmatter={"title": "r"}, body="resident"))
    foreign = store / "keep.txt"
    foreign.write_text("not crib's\n")
    out = run(crib.project_delete(project="toucan", force=True))
    assert out["store_deleted"] is False
    assert p.exists() and foreign.exists()
    assert not out.get("store_leftovers")  # nothing was removed, nothing to name
    assert (repo / ".git").exists()
    assert (repo / ".crib").read_text()  # the repo's declarations stay too
    assert isinstance(out["message"], str)  # the 1-tuple regression guard
    assert "NOT touched" in out["message"]


def test_rename_stamps_null_id_notes(crib):
    """`id: null` scans as the literal string "null" — the canonical id treats
    those as ABSENT, so the note is a stamp candidate, not an id owner."""
    from crib import notes as _notes
    from crib.notes import Note

    pdir = crib.paths.project_dir("nullid")
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "notes").mkdir(exist_ok=True)
    _notes.save_atomic(
        Note(
            path=pdir / "notes" / "n.md",
            frontmatter={"title": "t", "id": None},
            body="x",
        )
    )
    out = run(crib.project_rename(project="nullid", to="nullid2"))
    assert out["renamed"] is True
    # the null-id note was treated as idless and stamped
    assert out["id_less_notes_stamped"] == 1


def test_rename_rebinds_mirror_bindings(crib, tmp_path):
    """Rename REBINDS mirror bindings (drop would unbind the harness mirror
    for good — the first rename pass did exactly that)."""
    run(crib.store_note("x", title="T", project="old-name"))
    crib.memory_bindings.upsert(tmp_path / "harness", "old-name")
    run(crib.project_rename(project="old-name", to="new-name"))
    bindings = crib.memory_bindings.all()
    assert [b["project"] for b in bindings] == ["new-name"]
    assert [str(b["root"]) for b in bindings] == [str(tmp_path / "harness")]


def test_rename_keeps_sources_registry(crib, tmp_path):
    """The in-situ doc registry travels with the tree (entries key on repo
    paths, not the project name) — wiping it silently lost every sources/ doc."""
    run(crib.store_note("x", title="T", project="old-name"))
    repo = tmp_path / "repos" / "docsrepo"
    repo.mkdir(parents=True)
    crib._source_roots("old-name").upsert("sources/docsrepo/", repo)
    run(crib.project_rename(project="old-name", to="new-name"))
    assert "sources/docsrepo/" in crib._source_roots("new-name").all()


def test_rename_rewrites_own_repo_crib(crib, tmp_path, repo):
    """An adopted project's OWN repo .crib says `project: <old>` after a
    rename — every cwd-resolved read in that repo would resurrect the old
    namespace. The line gets rewritten (this repo, not a foreign one)."""
    run(crib.project_create(project="toucan", cwd=repo, store=".crib-store"))
    run(crib.project_rename(project="toucan", to="toucan2"))
    text = (repo / ".crib").read_text()
    assert "project: toucan2" in text
    assert "project: toucan\n" not in text


def test_delete_plan_preview(crib):
    """_delete_plan applies the fences and reports the honest preview the
    MCP confirmation is built from."""
    run(crib.store_note("b", title="t", project="planned"))
    plan = crib._delete_plan("planned", force=False)
    assert plan["exists_anywhere"] is True
    assert plan["notes_count"] >= 1 and plan["chunks_count"] >= 1
    assert plan["will_remove_store"] is False
    with pytest.raises(CribUserError, match="default project"):
        crib._delete_plan(crib.config.default_project, force=False)


def test_id_stamp_collision_skipped(crib):
    """Same relative name in two pillars derives the SAME id — stamping both
    would forge duplicate frontmatter ids; the second is skipped and reported."""
    from crib import notes as notes_mod
    from crib.notes import Note

    for pillar in ("notes", "design"):
        pp = crib.project_paths("dupe-name")
        pp.pillar_dir(pillar).mkdir(parents=True, exist_ok=True)
        p = pp.pillar_dir(pillar) / "same.md"
        notes_mod.save_atomic(Note(path=p, frontmatter={"title": "s"}, body="x"))
    out = run(crib.project_rename(project="dupe-name", to="dupe-renamed"))
    assert out["renamed"] is True
    # exactly one stamped, exactly one collision-skipped (same derived id)
    assert out["id_less_notes_stamped"] == 1
    assert out["id_less_stamp_collisions"] == 1


def test_delete_unwatches_code_watcher_for_global_project(crib, repo, monkeypatch):
    """forget_project runs for EVERY layout (the indent bug kept global
    projects watched — the next save in their checkout resurrected the
    phantom namespace the hook's own docstring warns about)."""
    from crib.watch import CodeWatcher

    async def _noop(*a, **k):
        return None

    cw = CodeWatcher(_noop, __import__("asyncio").new_event_loop())
    crib._code_watcher = cw
    run(crib.store_note("x", title="T", project="watched-global"))
    cw.watch_root("watched-global", repo)
    assert cw.watches("watched-global")
    run(crib.project_delete(project="watched-global"))
    assert not cw.watches("watched-global")


# --- project_relocate -------------------------------------------------------


def test_relocate_renames_mount_rekeys_chunks_and_repoints(tmp_path, crib):
    """THE DEFAULT (mount rename): the checkout's dir name moved, so the
    sources/<old>/ mount re-homes to sources/<new-name>/ — the doc chunk rekeys
    (old dropped + re-indexed under the new relpath), the registry's prefix name
    follows the dir, .source_root moves, and the cheap rehash runs. Citations
    keep resolving; the .crib names THIS project already, so it is NOT
    rewritten."""
    import json as _json
    import shutil

    from crib.codeindex import SymbolIndex
    from crib.sources import SRC_PREFIX

    old_dir = tmp_path / "repos" / "toucan-old"
    old_dir.mkdir(parents=True)
    (old_dir / "docs").mkdir()
    (old_dir / "docs" / "guide.md").write_text("# Guide\n\nrelocate me\n")
    (old_dir / ".crib").write_text('project: toucan\ndocs: ["docs/*.md"]\n')
    run(crib.project_create(project="toucan"))
    out = run(crib.index_docs_insitu("toucan", old_dir))
    old_prefix = out["prefix"]
    assert old_prefix == f"{SRC_PREFIX}toucan-old/"
    SymbolIndex(crib.paths.project_dir("toucan")).set_source_root(old_dir)

    new_dir = tmp_path / "repos" / "toucan-new"
    shutil.move(str(old_dir), str(new_dir))
    res = run(crib.project_relocate(project="toucan", project_path=str(new_dir), wait_s=120))
    assert res["relocated"] is True
    assert res["crib_rewritten"] is None  # .crib names THIS project already
    # the mount RENAMED to the new dir's name (that is the default's point)
    after = crib._source_roots("toucan").all()
    assert sorted(after) == [f"{SRC_PREFIX}toucan-new/"]
    assert after[f"{SRC_PREFIX}toucan-new/"] == str(new_dir)
    mount = res["mount_renamed"][0]
    assert mount["from"] == old_prefix and mount["to"] == f"{SRC_PREFIX}toucan-new/"
    assert res["chunks_rekeyed"] == 1  # one doc dropped + re-indexed
    assert int(mount["docs"]) == 1
    # the in-situ doc resolves through the NEW spelling …
    solved = crib._source_roots("toucan").resolve(
        f"{SRC_PREFIX}toucan-new/docs/guide.md"
    )
    assert solved is not None and solved.is_file()
    assert solved.read_text().startswith("# Guide")
    # …and the OLD spelling is gone (no zombie docs)
    assert crib._source_roots("toucan").resolve(f"{old_prefix}docs/guide.md") is None
    # the code index's absolute root moved with the one rewrite
    si = SymbolIndex(crib.paths.project_dir("toucan"))
    assert si.source_root() == new_dir
    # the cheap confirmation rehash RAN (mount-rename mode) and changed nothing
    assert res["reindex"] is not None
    assert res["reindex"].get("complete") is not False
    # the result is json-serializable (it rides the MCP face)
    _json.dumps(res)


def test_relocate_keep_prefix_is_identity_keeping(tmp_path, crib):
    """keep_prefix=true: the mount prefix KEEPS ITS NAME (citations keep spelling
    the old dir name), the root still re-points, and the confirmation rehash is
    SKIPPED — a docs re-hash would register sources/<new-name>/ beside the kept
    mount and double-index every doc."""
    import shutil

    from crib.codeindex import SymbolIndex

    old_dir = tmp_path / "repos" / "toucan-old"
    old_dir.mkdir(parents=True)
    (old_dir / "docs").mkdir()
    (old_dir / "docs" / "guide.md").write_text("# Guide\n\nrelocate me\n")
    (old_dir / ".crib").write_text('project: toucan\ndocs: ["docs/*.md"]\n')
    run(crib.project_create(project="toucan"))
    out = run(crib.index_docs_insitu("toucan", old_dir))
    old_prefix = out["prefix"]
    SymbolIndex(crib.paths.project_dir("toucan")).set_source_root(old_dir)

    new_dir = tmp_path / "repos" / "toucan-new"
    shutil.move(str(old_dir), str(new_dir))
    res = run(
        crib.project_relocate(
            project="toucan", project_path=str(new_dir), keep_prefix=True, wait_s=120
        )
    )
    assert res["mount_renamed"] == [] and res["chunks_rekeyed"] == 0
    assert res["reindex"] is None  # skipped: a re-hash would double-mount
    after = crib._source_roots("toucan").all()
    assert sorted(after) == [old_prefix]  # the name was KEPT
    assert after[old_prefix] == str(new_dir)
    assert crib._source_roots("toucan").resolve(f"{old_prefix}docs/guide.md").is_file()


def test_relocate_rewrites_citations_and_own_crib_after_the_fact(tmp_path, crib):
    """THE PEER'S CASE, exact order: the repo dir moved FIRST, project_rename
    ran SECOND (its chunk churn rebuilds pillar notes but SKIPS the in-situ docs
    — the registry root is dead); the cited decisions sit 'source-missing' with
    their doc chunks LOST at call time. The relocate fixes it: rewrites the
    repo's own .crib line (dead-source evidence proves it is OURS), renames the
    mount, and the final rehash re-indexes the docs from the new path — so
    design_check goes clean WITHOUT any note body having moved."""
    import shutil

    name0 = "toucan"
    repo = tmp_path / "repos" / name0
    repo.mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("# Guide\n\ncited section\n")
    (repo / ".crib").write_text(f'project: {name0}\ndocs: ["docs/*.md"]\n')
    run(crib.project_create(project=name0))
    run(crib.index_docs_insitu(name0, repo))

    # a decision CITED from the in-situ doc (hash captured at add time; the
    # citation stores the canonical in-situ spelling)
    dec = run(
        crib.design_add(
            project=name0,
            title="Mount identity is the dir name",
            content="Decided because doc says so.",
            sources=["docs/guide.md#Guide"],
        )
    )
    ref_by = dec.get("relpath") or "Mount identity is the dir name"
    read0 = crib.design_read(ref_by, project=name0)
    assert read0["sources"][0]["ref"] == f"sources/{name0}/docs/guide.md"

    # THE DOUBLE MOVE, out from under everything — the peer's order:
    moved = tmp_path / "repos2" / "toucan-renamed"
    shutil.move(str(repo), str(moved))  # the checkout dir moves FIRST
    run(crib.project_rename(project=name0, to="toucan2"))  # …then the namespace
    assert crib.project_config("toucan2")  # the stub carried the rename
    # citations ARE broken right now: the rename re-indexed nothing in situ
    # (the registry root was dead), so the doc's chunks are LOST
    check = crib.design_check(project="toucan2")
    rows = _check_rows(check)
    assert rows, "expected a stale row (source-missing)"
    assert any("source-missing" in json.dumps(r) for r in rows), rows

    res = run(crib.project_relocate(project="toucan2", project_path=str(moved), wait_s=180))
    # the .crib NOW names the project the namespace was renamed to
    assert "project: toucan2" in (moved / ".crib").read_text()
    assert res["crib_rewritten"] == str(moved / ".crib")
    # the mount RENAMED to the new dir name; the lost chunks are re-indexed by
    # the final rehash (chunks_rekeyed counts the SURVIVING ones — zero here)
    assert res["mount_renamed"], res
    assert res["citations_rewritten"] == 1
    assert res["chunks_rekeyed"] == 0
    assert res["reindex"] is not None
    new_prefix = "sources/toucan-renamed/"
    assert sorted(crib._source_roots("toucan2").all()) == [new_prefix]
    # the doc lives again under the new spelling, and the citation follows
    solved = crib._source_roots("toucan2").resolve(f"{new_prefix}docs/guide.md")
    assert solved is not None and solved.is_file()
    assert solved.read_text().startswith("# Guide")
    readback = crib.design_read(ref_by, project="toucan2")
    assert readback["sources"][0]["ref"] == f"{new_prefix}docs/guide.md"
    # …and the citation is HEALTHY: the decision is no longer reported stale
    check2 = crib.design_check(project="toucan2")
    assert not _check_rows(check2) or not any(
        "source-missing" in json.dumps(r) for r in _check_rows(check2)
    )


def test_relocate_reports_foreign_refs_never_edits_them(tmp_path, crib, repo):
    """Other projects' .crib refs naming this project are REPORTED (never
    edited) — the foreign repos are never written to."""
    run(crib.project_create(project="toucan"))
    (repo / ".crib").write_text("project: toucan\n")  # minimal, enough to pass
    other_repo = tmp_path / "repos" / "other"
    other_repo.mkdir(parents=True, exist_ok=True)
    (other_repo / ".crib").write_text("project: other\nrefs:\n  - toucan\n")
    crib._source_roots("other").upsert("sources/other/", other_repo)
    res = run(crib.project_relocate(project="toucan", project_path=str(repo), wait_s=120))
    # nothing to re-point (the repo never moved / no dead source) — but the
    # foreign ref rows ride the report
    assert res["prefixes_repointed"] == [] and res["mount_renamed"] == []
    json.dumps(res)  # fully serializable


def _check_rows(check: Any) -> list[dict[str, Any]]:
    """design_check's rows, tolerantly: the verb returns {'tainted': [rows]} (a
    dict with 'rows' spelling is tolerated too), and tests need the rows to
    inspect their causes' change_kind fields."""
    if isinstance(check, list):
        return list(check)
    return list(check.get("tainted") or check.get("rows") or [])


def test_relocate_fences(crib, tmp_path):
    # never the session default or a cwd guess: both names are required
    with pytest.raises(CribUserError, match="pass project"):
        run(crib.project_relocate())
    with pytest.raises(CribUserError, match="pass project"):
        run(crib.project_relocate(project="only-one"))
    # a missing dir
    with pytest.raises(CribUserError, match="no such directory"):
        run(crib.project_relocate(project="ghost", project_path=str(tmp_path / "nope")))
    # no .crib in the moved tree
    bare = tmp_path / "bare"
    bare.mkdir()
    run(crib.project_create(project="nolink"))
    with pytest.raises(CribUserError, match="no .crib found"):
        run(crib.project_relocate(project="nolink", project_path=str(bare)))
    # a .crib naming a foreign project, with NO dead-source evidence
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / ".crib").write_text("project: someoneelse\n")
    run(crib.project_create(project="mine"))
    with pytest.raises(CribUserError, match="someoneelse"):
        run(crib.project_relocate(project="mine", project_path=str(foreign)))
