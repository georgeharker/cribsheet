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
    for r in ("note_lookup", "note_read", "code_lookup"):  # reads are unconstrained
        assert asyncio.run(schema(r)).get("anyOf") is None, r


def test_write_project_elicits_when_target_omitted(crib):
    import asyncio

    from crib.server import _write_project_elicit

    class _Accepted:  # mimics fastmcp AcceptedElicitation
        def __init__(self, data):
            self.data = data

    class _Ctx:
        def __init__(self, behaviour):
            self.behaviour = behaviour

        async def elicit(self, message, response_type=None):
            if self.behaviour == "accept":
                return _Accepted("chosen-proj")
            if self.behaviour == "decline":
                return object()  # no .data → treated as declined
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
    assert (
        _write_project(crib, None, str(crib.paths.data_dir))
        == crib.config.default_project
    )


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
    assert not out["created"]  # existing project — no session switch


def test_unknown_project_with_no_path_errors_not_cwd(crib, monkeypatch, tmp_path):
    # even with the process cwd sitting in a plausible repo, a bare unknown name
    # must error — never claim the cwd
    decoy = _crib_repo(tmp_path, "decoy")
    monkeypatch.chdir(decoy)
    with pytest.raises(ValueError, match="never falls back"):
        crib._ensure_crib(None, "ghost", want_code=True, want_docs=False)
    assert (decoy / ".crib").read_text() == "project: decoy\n"  # untouched


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


def test_code_index_files_symbols_under_the_path_repo_not_the_sticky_project(
    crib, tmp_path, monkeypatch
):
    """P1.1: `code_index` is REPO-SCOPED like its project_* siblings. With a sticky
    session on A, indexing a file in repo B (project_path=B) must file B's symbols
    under B — resolving via `_project` filed them under A."""
    import asyncio

    from crib.server import build_server

    beta = _crib_repo(tmp_path, "beta")
    (beta / "b.py").write_text("def b(): pass\n")
    session_state().current_project = "alpha"  # sticky elsewhere

    indexed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        crib.indexer,
        "_index_code_file_tracked",
        lambda root, rel, proj, patch, existing=None, describe_mode="inline": (
            indexed.append((proj, rel))
        ),
    )
    mcp = build_server(crib)

    async def call():
        tool = await mcp.get_tool("code_index")
        return await tool.run({"path": str(beta / "b.py"), "project_path": str(beta)})

    asyncio.run(call())
    assert indexed == [("beta", "b.py")]  # B's repo, not the sticky A


def _advisory_args(project_path, json=False):
    from types import SimpleNamespace

    return SimpleNamespace(project_path=project_path, json=json)


def _wants_cwd_entry(wants=True):
    from types import SimpleNamespace
    from typing import Any, cast

    return cast(Any, SimpleNamespace(wants_cwd=wants))


def test_advisory_fires_for_explicit_unlinked_path(tmp_path):
    # `-P <dir>` where no `.crib` anchors the dir → advisory (not a refusal), naming
    # the fallback project and both recoveries. See DESIGN 'An explicit selector
    # never loses' (2026-08-26 refinement).
    from crib.cli import _unlinked_path_message

    msg = _unlinked_path_message(
        _advisory_args(str(tmp_path)), _wants_cwd_entry(), Config()
    )
    assert msg and "not linked to a crib project" in msg
    assert "crib project setup" in msg


def test_advisory_silent_without_project_path():
    # bare invocation (no -P) is a deliberate default landing — stay silent.
    from crib.cli import _unlinked_path_message

    assert (
        _unlinked_path_message(_advisory_args(None), _wants_cwd_entry(), Config())
        is None
    )


def test_advisory_silent_when_crib_links_the_path(tmp_path):
    from crib.cli import _unlinked_path_message

    (tmp_path / ".crib").write_text("project: linked\n")
    assert (
        _unlinked_path_message(
            _advisory_args(str(tmp_path)), _wants_cwd_entry(), Config()
        )
        is None
    )


def test_advisory_silent_for_non_cwd_verb(tmp_path):
    from crib.cli import _unlinked_path_message

    assert (
        _unlinked_path_message(
            _advisory_args(str(tmp_path)), _wants_cwd_entry(wants=False), Config()
        )
        is None
    )


def test_json_mode_puts_advisory_in_payload_not_just_stderr(capsys):
    # HAZARD guard: under --json a stderr-only warning is invisible to a
    # stdout-parsing consumer, which then reads a legit-looking empty result. So the
    # message lands IN the dict payload for --json (and still on stderr).
    from crib.cli import _apply_unlinked_advisory

    data = _apply_unlinked_advisory(
        _advisory_args(".", json=True), "MSG", {"items": [], "total": 0}
    )
    assert data["unlinked_project_path"] == "MSG"
    assert "MSG" in capsys.readouterr().err  # human channel too


def test_human_mode_keeps_json_payload_clean(capsys):
    # Without --json the message is stderr-only; the payload carries no extra key.
    from crib.cli import _apply_unlinked_advisory

    data = _apply_unlinked_advisory(
        _advisory_args(".", json=False), "MSG", {"items": []}
    )
    assert "unlinked_project_path" not in data
    assert "MSG" in capsys.readouterr().err


def test_apply_drops_daemon_key_when_no_advisory(capsys):
    # A bare cwd (msg is None) must strip the daemon's own copy so the CLI stays the
    # single owner and an auto-filled cwd surfaces nothing.
    from crib.cli import _apply_unlinked_advisory

    data = _apply_unlinked_advisory(
        _advisory_args(None, json=True),
        None,
        {"unlinked_project_path": "from-daemon", "x": 1},
    )
    assert "unlinked_project_path" not in data
    assert capsys.readouterr().err == ""


def test_mcp_unlinked_project_path_carries_advisory(crib, tmp_path):
    # MCP counterpart of the CLI advisory: an agent's project_path that no `.crib`
    # anchors resolves to the bare default and must SAY so on the result (every read,
    # not just echo= ones), so the agent can re-call with project=.
    from crib.server import _echo_unlinked, _resolve

    session_state().current_project = None
    res = _resolve(crib, None, str(tmp_path))  # no `.crib` under tmp_path
    assert res.via == "seed" and res.path_unmatched and not res.unanchored
    out = _echo_unlinked({"items": [], "total": 0}, res)
    assert "unlinked_project_path" in out
    # an empty list becomes a one-item diagnostic rather than a bare []
    assert _echo_unlinked([], res)[0]["unlinked_project_path"]


def test_mcp_advisory_absent_for_linked_or_explicit(crib, tmp_path):
    from crib.server import _resolve

    session_state().current_project = None
    (tmp_path / ".crib").write_text("project: linked\n")
    res = _resolve(crib, None, str(tmp_path))
    # `path_unmatched` is the guard `finish` checks before calling `_echo_unlinked`;
    # a linked path never trips it, so the advisory is never injected.
    assert not res.path_unmatched

    session_state().current_project = None
    res2 = _resolve(crib, "named", None)  # explicit project= never triggers it
    assert not res2.path_unmatched


def test_mcp_no_path_is_unanchored_not_unmatched(crib):
    # no project_path at all is the EXISTING unanchored guess (elicit/refuse), a
    # distinct branch from a supplied-but-unlinked path.
    from crib.server import _resolve

    session_state().current_project = None
    res = _resolve(crib, None, None)
    assert res.unanchored and not res.path_unmatched


def test_json_error_emits_json_object_on_stdout(monkeypatch, capsys):
    # Under --json a hard refusal must not leave stdout empty: emit {"error": msg}
    # so a stdout-parsing consumer gets a reason, not an unparseable blank.
    import json as _json

    from crib import cli
    from crib.errors import CribUserError

    def boom(args, cfg):
        raise CribUserError("nope, that is not allowed")

    monkeypatch.setattr(cli, "_run_inprocess", boom)
    rc = cli.main(["--no-daemon", "--json", "plan", "list"])
    out = capsys.readouterr()
    assert rc == 2
    assert _json.loads(out.out) == {"error": "nope, that is not allowed"}
    assert "nope, that is not allowed" in out.err  # human channel unchanged


def test_non_json_error_leaves_stdout_empty(monkeypatch, capsys):
    from crib import cli
    from crib.errors import CribUserError

    def boom(args, cfg):
        raise CribUserError("boom")

    monkeypatch.setattr(cli, "_run_inprocess", boom)
    rc = cli.main(["--no-daemon", "plan", "list"])
    out = capsys.readouterr()
    assert rc == 2
    assert out.out == ""  # no JSON without --json
    assert "boom" in out.err
