"""In-repo project storage: a repo carries its project's DATA tier.

The contract under test (docs/plans/repo-local-storage.md): a project's notes and
their version ring live EITHER in the global store OR in a repo subdir declared by
that repo's `.crib` `store:` — never both — and moving between the two is a file
move, not a reindex, because `chunk_id` is derived from PROJECT-relative relpaths.
So the load-bearing assertion here is that the chunk ids are byte-identical across
adopt → release.
"""

from __future__ import annotations

import asyncio

import pytest

from crib.app import Crib
from crib.config import Config, CribLink, ProjectConfig
from crib.paths import Paths, resolve_project_paths
from crib.store import InMemoryStore


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Paths.resolve().ensure()


@pytest.fixture()
def config(tmp_path):
    # a named location so the stub's `store_root` is written as a PORTABLE token,
    # which is the whole point of recording it that way
    return Config(locations={"REPOS": str(tmp_path)})


@pytest.fixture()
def crib(paths, config):
    return Crib(paths, config, InMemoryStore())


def repo_with_store(tmp_path, project="p", store=".crib-store", docs=()):
    """A code repo whose `.crib` declares an in-repo store."""
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    (root / ".crib").write_text(f"project: {project}\nstore: {store}\n"
                                + ("docs:\n" + "".join(f'  - "{d}"\n' for d in docs)
                                   if docs else ""))
    return root


def run(coro):
    return asyncio.run(coro)


def chunk_ids(crib, project):
    return sorted(crib.store.get_meta({"project": project}))


# ── `.crib` `store:` validation ───────────────────────────────────────────────

def test_store_relpath_resolves_against_the_repo_root(tmp_path):
    root = repo_with_store(tmp_path)
    link = CribLink.find(root)
    assert link.store == ".crib-store"
    assert link.store_dir == root / ".crib-store"


def test_missing_store_key_is_simply_absent(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / ".crib").write_text("project: p\n")
    link = CribLink.find(root)
    assert link.store is None and link.store_dir is None


@pytest.mark.parametrize("bad", ["/etc/crib", "../outside", "sub/../../out"])
def test_store_that_escapes_the_repo_is_refused(tmp_path, bad, capsys):
    """A `.crib` is hand-edited and travels between machines, so an absolute or
    `..`-escaping store must never plant a store outside the repo. It is dropped
    with a warning rather than raised — a malformed `.crib` sits above someone's
    whole tree and must not break every command run below it."""
    root = repo_with_store(tmp_path, store=bad)
    link = CribLink.find(root)
    assert link.store is None                       # dropped …
    assert "store:" in capsys.readouterr().err      # … and said so
    # the property itself is strict: it raises rather than returning a bad path
    with pytest.raises(ValueError, match="crib|escapes|absolute"):
        CribLink(project="p", root=root, store=bad).store_dir


# ── resolution: global vs in-repo vs unavailable ──────────────────────────────

def test_resolves_to_the_global_layout_by_default(paths, config):
    pp = resolve_project_paths(paths, config, "p")
    assert not pp.in_repo and pp.available
    assert pp.notes_dir == paths.projects_dir / "p" / "notes"
    # global projects keep the SHARED ring, keyed by note id across projects
    assert pp.versions_dir == paths.versions_dir


def test_resolves_to_the_store_when_the_stub_points_at_one(paths, config, tmp_path):
    store = tmp_path / "repo" / ".crib-store"
    store.mkdir(parents=True)
    ProjectConfig(name="p", store_root="$REPOS/repo/.crib-store").save(
        paths.project_dir("p") / ".cribproject")
    pp = resolve_project_paths(paths, config, "p")
    assert pp.in_repo and pp.available
    assert pp.notes_dir == store / "notes"
    assert pp.versions_dir == store / ".versions"   # ring travels with the notes
    assert pp.project_dir == paths.project_dir("p")  # index tier does NOT move


def test_a_store_that_is_not_on_this_machine_is_unavailable(paths, config):
    ProjectConfig(name="p", store_root="$REPOS/never-cloned/store").save(
        paths.project_dir("p") / ".cribproject")
    pp = resolve_project_paths(paths, config, "p")
    assert pp.in_repo and not pp.available
    # every read/write funnels through `require()`, and it names the fix
    with pytest.raises(ValueError, match="not on this machine"):
        pp.require()


def test_verbs_error_actionably_while_the_store_is_missing(crib, paths):
    ProjectConfig(name="gone", store_root="$REPOS/never-cloned/store").save(
        paths.project_dir("gone") / ".cribproject")
    crib.project_paths.invalidate()
    for call in (lambda: crib.read_note("a.md", project="gone"),
                 lambda: run(crib.store_note("x", title="t", project="gone"))):
        with pytest.raises(ValueError, match="clone the repo"):
            call()
    # …but it is still LISTED, with the token, so it reads as "fetch me" not "gone"
    row = next(r for r in crib.project_list() if r["project"] == "gone")
    assert row["unavailable"] is True
    assert row["store_root"] == "$REPOS/never-cloned/store"


def test_startup_reconcile_skips_an_unavailable_store(crib, paths, capsys):
    ProjectConfig(name="gone", store_root="$REPOS/never-cloned/store").save(
        paths.project_dir("gone") / ".cribproject")
    crib.project_paths.invalidate()
    run(crib.store_note("a real note", title="ok", project="here"))
    rec = run(crib.reconcile_all())                 # must not raise
    assert rec["unavailable"] == ["gone"]
    assert "not reconciling gone" in capsys.readouterr().err
    run(crib.reconcile_all())                       # ONE warning, not one per sweep
    assert "not reconciling gone" not in capsys.readouterr().err


# ── adopt → work → release, with chunk ids held fixed ─────────────────────────

def test_adopt_moves_the_data_tier_and_leaves_chunk_ids_alone(crib, paths, tmp_path):
    root = repo_with_store(tmp_path)
    a = run(crib.store_note("turbine maintenance schedule", title="a", project="p"))
    run(crib.store_note("gardening in autumn", title="b", project="p"))
    run(crib.edit_note(a["relpath"], "turbine maintenance, revised", project="p"))
    before = chunk_ids(crib, "p")
    assert crib.list_versions(a["relpath"], project="p")   # ring has history to move

    res = run(crib.project_adopt(cwd=root))
    store = root / ".crib-store"
    assert res["changed"] and res["notes_moved"] == 2 and res["versions_moved"] >= 1
    # the stub records it PORTABLY, and the repo's own store now holds the notes
    assert res["store_root"] == "$REPOS/repo/.crib-store"
    assert (store / "notes" / a["relpath"]).exists()
    assert not (paths.notes_dir("p")).exists()
    # the ring moved alongside — and is gitignored, since the repo owns the notes
    # but not their undo stack
    assert (store / ".versions").is_dir()
    assert ".versions/" in (store / ".gitignore").read_text()
    # the derived index tier stayed machine-local
    assert not (store / "symbol_index").exists()

    # the move is a REBINDING, not a reindex: same chunks, and the hash-gated
    # reconcile the verb runs is the proof (0 changed, 0 removed)
    assert res["reconciled"]["changed"] == 0 and res["reconciled"]["removed"] == 0
    assert chunk_ids(crib, "p") == before

    # and the verbs keep working against the repo store
    assert crib.lookup("turbine", project="p")
    assert crib.list_versions(a["relpath"], project="p")
    appended = run(crib.append_note(a["relpath"], "extra line", project="p"))
    assert (store / "notes" / appended["relpath"]).read_text().endswith("extra line\n")


def test_release_moves_it_back_and_still_leaves_chunk_ids_alone(crib, paths, tmp_path):
    root = repo_with_store(tmp_path)
    a = run(crib.store_note("turbine maintenance schedule", title="a", project="p"))
    run(crib.project_adopt(cwd=root))
    # edit AFTER adopting, so the history to bring back was written into the
    # store's own ring rather than left behind in the global one
    run(crib.edit_note(a["relpath"], "turbine maintenance, revised", project="p"))
    before = chunk_ids(crib, "p")

    res = run(crib.project_release(project="p"))
    assert res["changed"] and res["notes_moved"] == 1
    assert (paths.notes_dir("p") / a["relpath"]).exists()
    assert not (root / ".crib-store" / "notes").exists()
    assert res["reconciled"]["changed"] == 0 and res["reconciled"]["removed"] == 0
    assert chunk_ids(crib, "p") == before
    assert crib.project_config("p").store_root is None
    assert crib.lookup("turbine", project="p")
    assert crib.list_versions(a["relpath"], project="p")   # history came back too


def test_adopt_and_release_are_idempotent(crib, tmp_path):
    root = repo_with_store(tmp_path)
    run(crib.store_note("a note", title="a", project="p"))
    assert run(crib.project_release(project="p"))["changed"] is False
    run(crib.project_adopt(cwd=root))
    again = run(crib.project_adopt(cwd=root))
    assert again["changed"] is False and again["notes_moved"] == 0


def test_adopt_refuses_to_merge_two_note_trees(crib, tmp_path):
    """Exclusive, not overlay: a store that already holds notes is a merge, and a
    merge is what this design refuses to guess at."""
    root = repo_with_store(tmp_path)
    dest = root / ".crib-store" / "notes"
    dest.mkdir(parents=True)
    (dest / "squatter.md").write_text("---\ntitle: x\n---\nalready here\n")
    run(crib.store_note("a note", title="a", project="p"))
    with pytest.raises(ValueError, match="refusing to merge"):
        run(crib.project_adopt(cwd=root))


def test_adopt_needs_the_store_key(crib, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".crib").write_text("project: p\n")
    with pytest.raises(ValueError, match="declares no `store:`"):
        run(crib.project_adopt(cwd=root))


# ── the store is excluded from the repo's own globs (plan F1) ─────────────────
# A `.crib` glob that reaches into the store would index the SAME bytes a second
# time under a different identity (`sources/<repo>/…` vs the note's own relpath) —
# real duplicates and retrieval decoys, not hash-gated no-ops. So every
# enumeration point excludes the store; these pin that at each one.

def relpaths(crib, project):
    return {m["relpath"] for m in crib.store.get_meta({"project": project}).values()}


def test_store_notes_are_not_also_indexed_as_in_situ_docs(crib, tmp_path):
    root = repo_with_store(tmp_path, docs=["**/*.md"])
    a = run(crib.store_note("turbine maintenance schedule", title="a", project="p"))
    (root / "README.md").write_text("# readme\n\nthe repo's own prose\n")
    run(crib.project_adopt(cwd=root))
    assert (root / ".crib-store" / "notes" / a["relpath"]).exists()

    res = run(crib.index_docs_insitu("p", root))
    rels = relpaths(crib, "p")
    assert "sources/repo/README.md" in rels     # the repo's own doc still indexes
    assert res["docs"] == 1                     # …and is the ONLY thing the glob took
    # the note is indexed exactly once, as a note — nothing under the store was
    # taken as a doc, whatever `**/*.md` appears to say
    assert a["relpath"] in rels
    assert not [r for r in rels if r.startswith("sources/repo/.crib-store")]


def test_store_files_are_not_swept_as_code(crib, tmp_path):
    """Same rule at the code sweep's enumeration: the store is a boundary, like a
    nested project's own `.crib` root."""
    root = repo_with_store(tmp_path)
    run(crib.store_note("a note", title="a", project="p"))
    run(crib.project_adopt(cwd=root))
    (root / "real.py").write_text("def f():\n    return 1\n")
    (root / ".crib-store" / "notes" / "stray.py").write_text("def g():\n    return 2\n")
    files = crib._enumerate_code_files(root, ["**/*.py"])
    assert root / "real.py" in files
    assert not [f for f in files if f.is_relative_to(root / ".crib-store")]


def test_code_watcher_skips_saves_inside_the_store(tmp_path):
    """The watcher gets the same treatment as the sweep — otherwise which path an
    edit took (save vs sweep) would decide whether it got indexed twice."""
    from crib.watch import CodeWatcher
    root = repo_with_store(tmp_path, docs=["**/*.md"])
    (root / ".crib-store" / "notes").mkdir(parents=True)
    (root / ".crib-store" / "notes" / "n.md").write_text("---\ntitle: n\n---\nx\n")
    (root / "README.md").write_text("# readme\n")
    resolved = CodeWatcher._resolve_batch({
        ".crib-store/notes/n.md": (str(root), False, "doc"),
        "README.md": (str(root), False, "doc"),
    })
    assert resolved == {"\x00doc\x00README.md": (str(root), False)}


def test_adopt_warns_when_the_repos_globs_reach_into_the_store(crib, tmp_path, capsys):
    root = repo_with_store(tmp_path, docs=["**/*.md"])
    run(crib.store_note("a note", title="a", project="p"))
    run(crib.project_adopt(cwd=root))
    err = capsys.readouterr().err
    assert "**/*.md" in err and str(root / ".crib-store") in err


def test_adopt_is_quiet_when_the_globs_stay_out_of_the_store(crib, tmp_path, capsys):
    root = repo_with_store(tmp_path, docs=["docs/**/*.md"])
    (root / "docs").mkdir()
    (root / "docs" / "d.md").write_text("# d\n")
    run(crib.store_note("a note", title="a", project="p"))
    run(crib.project_adopt(cwd=root))
    assert "reach into" not in capsys.readouterr().err


# ── the watcher covers an in-repo store ───────────────────────────────────────

def test_watcher_indexes_an_edit_inside_an_in_repo_store(crib, tmp_path):
    pytest.importorskip("watchdog")
    root = repo_with_store(tmp_path, docs=["**/*.md"])
    run(crib.store_note("seed note", title="seed", project="p"))
    run(crib.project_adopt(cwd=root))
    notes_dir = root / ".crib-store" / "notes"

    async def scenario():
        crib.start_watchers(asyncio.get_running_loop())
        # the repo's docs are indexed in-situ too, so the CODE watcher holds this
        # root as well and sees the very same save — the store must reach the notes
        # path and ONLY the notes path
        await crib.index_docs_insitu("p", root)
        # an external editor writing straight into the repo's store
        (notes_dir / "external.md").write_text(
            "---\ntitle: ext\n---\nThe watcher should index this from the repo store.")
        for _ in range(50):                     # ~5s for debounce + index
            await asyncio.sleep(0.1)
            if crib.lookup("watcher index from the repo store", project="p"):
                break
        await asyncio.sleep(0.8)                # > the code watcher's own debounce
        crib.stop_watchers()
        return crib.lookup("watcher index from the repo store", project="p")

    hits = asyncio.run(scenario())
    assert hits and hits[0].relpath == "external.md"
    assert not [r for r in relpaths(crib, "p") if r.startswith("sources/")]


def test_watch_roots_skip_an_unavailable_store(crib, paths, capsys):
    ProjectConfig(name="gone", store_root="$REPOS/never-cloned/store").save(
        paths.project_dir("gone") / ".cribproject")
    crib.project_paths.invalidate()
    assert crib._in_repo_notes_roots() == {}
    assert "not watching gone" in capsys.readouterr().err


# ── git sync belongs to the repo, not to crib ─────────────────────────────────

def test_memory_sync_refuses_for_an_adopted_project(crib, config, paths, tmp_path,
                                                    monkeypatch, capsys):
    from crib.cli import main
    root = repo_with_store(tmp_path)
    run(crib.store_note("a note", title="a", project="p"))
    run(crib.project_adopt(cwd=root))
    monkeypatch.chdir(root)
    # `crib memory sync` acts on the GLOBAL data tree, which no longer carries this
    # project's notes — syncing would silently do nothing for them, and "fixing"
    # that by committing from both sides is two git histories of one file.
    monkeypatch.setattr("crib.config.Config.load", lambda _f: config)
    assert main(["memory", "sync"]) == 1
    err = capsys.readouterr().err
    assert "live in" in err and "that repo's git" in err
