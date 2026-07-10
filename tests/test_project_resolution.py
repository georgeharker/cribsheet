"""Repo-scoped ops must resolve their project from `project_path`'s `.crib`, not
the sticky session project. Regression: a project_index(project_path=/other/repo)
with a sticky current project once indexed the OTHER repo INTO the current one."""

from __future__ import annotations

import pytest

from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.server import _source_project, _write_project
from crib.session import session_state
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def test_explicit_project_wins(crib):
    assert _source_project(crib, "chosen", "/some/repo") == "chosen"


def test_project_path_defers_to_crib_not_sticky(crib):
    # sticky session is on some project…
    session_state().current_project = "cribsheet"
    # …but a repo-scoped call names a DIFFERENT repo via project_path → return None
    # so crib.project_* reads link.project from THAT repo's .crib (never the sticky).
    assert _source_project(crib, None, "/Users/me/other-repo") is None


def test_no_path_falls_back_to_session(crib):
    session_state().current_project = "sticky-proj"
    assert _source_project(crib, None, None) == "sticky-proj"


def test_write_tools_carry_project_or_path_anyof(crib):
    # the wire schema declares "project OR project_path required" (anyOf), so a
    # validating client enforces it up front — not only the runtime guard.
    import asyncio

    from crib.server import build_server
    mcp = build_server(crib)

    async def schema(name):
        return (await mcp.get_tool(name)).to_mcp_tool().inputSchema

    want = [{"required": ["project"]}, {"required": ["project_path"]}]
    for w in ("note_store", "note_append", "note_edit", "note_forget", "note_move"):
        assert asyncio.run(schema(w)).get("anyOf") == want, w
    for r in ("note_lookup", "note_read", "code_lookup"):   # reads are unconstrained
        assert asyncio.run(schema(r)).get("anyOf") is None, r


def test_write_project_elicits_when_target_omitted(crib):
    import asyncio

    from crib.server import _write_project_elicit

    class _Accepted:                       # mimics fastmcp AcceptedElicitation
        def __init__(self, data): self.data = data

    class _Ctx:
        def __init__(self, behaviour): self.behaviour = behaviour
        async def elicit(self, message, response_type=None):
            if self.behaviour == "accept":
                return _Accepted("chosen-proj")
            if self.behaviour == "decline":
                return object()            # no .data → treated as declined
            raise RuntimeError("client has no elicitation capability")

    run = asyncio.run
    # explicit project short-circuits (never elicits)
    assert run(_write_project_elicit(crib, "shuck", None, _Ctx("accept"))) == "shuck"
    # omitted → elicited value is used
    assert run(_write_project_elicit(crib, None, None, _Ctx("accept"))) == "chosen-proj"
    # declined or unsupported → falls back to the hard error
    for b in ("decline", "unsupported"):
        with pytest.raises(ValueError, match="explicit target"):
            run(_write_project_elicit(crib, None, None, _Ctx(b)))


def test_write_project_requires_explicit_target(crib):
    # writes never inherit the sticky session — a fact belongs to its subject's project
    session_state().current_project = "some-repo-im-browsing"
    with pytest.raises(ValueError, match="explicit target"):
        _write_project(crib, None, None)
    # explicit project wins
    assert _write_project(crib, "shuck", None) == "shuck"
    # project_path resolves via that repo's .crib (here: no .crib → default)
    assert _write_project(crib, None, str(crib.paths.data_dir)) == crib.config.default_project


# ── Repo-root resolution for repo-scoped ops (never the daemon's own cwd) ───────
# Regression: `project_index(project="zdot")` from the daemon (whose process cwd
# was an unrelated checkout) fell back to Path.cwd(), wrote a .crib claiming zdot
# there, and re-rooted the project. A bare name must resolve the recorded root;
# with nothing recorded it must ERROR, not claim whatever dir the process is in.

def _crib_repo(tmp_path, name, project=None):
    """A repo dir with a .git marker and a .crib naming `project` (default: name)."""
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".crib").write_text(f"project: {project or name}\n")
    return repo


def _register(crib, project, root):
    """Record `root` as `project`'s indexed source root (what indexing persists)."""
    from crib.codeindex import SymbolIndex
    SymbolIndex(crib.paths.project_dir(project)).set_source_root(root)


def test_named_project_resolves_registered_root(crib, tmp_path):
    repo = _crib_repo(tmp_path, "alpha")
    _register(crib, "alpha", repo)
    link, created = crib._ensure_crib(None, "alpha", want_code=True, want_docs=False)
    assert not created and link.root == repo and link.project == "alpha"


def test_named_project_end_to_end_index_uses_registered_root(crib, tmp_path):
    # the zdot repro: explicit project=, no path — must act on the recorded repo
    import asyncio
    repo = _crib_repo(tmp_path, "alpha")
    _register(crib, "alpha", repo)
    out = asyncio.run(crib.project_index(project="alpha"))
    assert out["root"] == str(repo) and not out["crib_created"]
    assert not out["created"]                    # existing project — no session switch


def test_unknown_project_with_no_path_errors_not_cwd(crib, monkeypatch, tmp_path):
    # even with the process cwd sitting in a plausible repo, a bare unknown name
    # must error — never claim the cwd
    decoy = _crib_repo(tmp_path, "decoy")
    monkeypatch.chdir(decoy)
    with pytest.raises(ValueError, match="never falls back"):
        crib._ensure_crib(None, "ghost", want_code=True, want_docs=False)
    assert (decoy / ".crib").read_text() == "project: decoy\n"   # untouched


def test_incidental_cwd_of_other_repo_defers_to_registered_root(crib, tmp_path):
    # the CLI ships the shell cwd with every call — `crib project index alpha`
    # run from inside beta's repo must still act on alpha's recorded root
    alpha = _crib_repo(tmp_path, "alpha")
    beta = _crib_repo(tmp_path, "beta")
    _register(crib, "alpha", alpha)
    link, created = crib._ensure_crib(beta, "alpha", want_code=True, want_docs=False)
    assert not created and link.root == alpha and link.project == "alpha"


def test_mismatched_path_without_registered_root_errors(crib, tmp_path):
    # explicit project naming ANOTHER repo's .crib, and nothing recorded to prefer:
    # refuse — never index one project's repo into another
    beta = _crib_repo(tmp_path, "beta")
    with pytest.raises(ValueError, match="refusing to index"):
        crib._ensure_crib(beta, "alpha", want_code=True, want_docs=False)


def test_create_refuses_dirname_collision_with_existing_project(crib, tmp_path):
    # a fresh repo whose DIR NAME matches an existing project rooted elsewhere:
    # auto-creating a .crib would silently merge two repos under one project
    original = _crib_repo(tmp_path / "a", "myrepo")
    _register(crib, "myrepo", original)
    clone = tmp_path / "b" / "myrepo"
    (clone / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="already exists"):
        crib._ensure_crib(clone, None, want_code=True, want_docs=False)
    assert not (clone / ".crib").exists()


def test_stale_registration_errors(crib, tmp_path):
    # recorded root's .crib was rewritten to another project → surface it
    repo = _crib_repo(tmp_path, "alpha", project="beta")
    _register(crib, "alpha", repo)
    with pytest.raises(ValueError, match="stale registration"):
        crib._ensure_crib(None, "alpha", want_code=True, want_docs=False)


def test_insitu_docs_and_memory_import_require_repo_dir(crib):
    import asyncio
    with pytest.raises(ValueError, match="pass project_path"):
        asyncio.run(crib.index_docs_insitu())
    with pytest.raises(ValueError, match="pass project_path"):
        asyncio.run(crib.import_claude_memory())


def test_switch_if_created_fires_on_project_creation(crib, tmp_path):
    # DESIGN §15: creating a project switches the session into it. Regression:
    # the switch keyed on "created", which setup/index never returned.
    import asyncio

    from crib.server import _switch_if_created
    repo = tmp_path / "fresh"
    (repo / ".git").mkdir(parents=True)
    out = asyncio.run(crib.project_index(cwd=repo))
    assert out["crib_created"] and out["created"]
    session_state().current_project = None
    _switch_if_created(out)
    assert session_state().current_project == "fresh"
