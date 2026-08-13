"""Per-connection session project resolution (precedence + seeding)."""

from crib.session import SessionState, resolve_session_project, session_state


def _seed(_cwd):
    return "seeded-from-cwd"


def test_explicit_arg_overrides_without_touching_session():
    st = SessionState()
    st.current_project = "sticky"
    res = resolve_session_project(st, "explicit", None, _seed)
    assert (res.project, res.via) == ("explicit", "explicit")
    assert not res.implicit                         # a named project is never implicit
    assert st.current_project == "sticky"          # override didn't change session


def test_the_first_path_adopts_and_says_so():
    st = SessionState()
    assert st.current_project is None
    res = resolve_session_project(st, None, "/some/cwd", _seed)
    assert (res.project, res.via) == ("seeded-from-cwd", "path")
    assert st.current_project == "seeded-from-cwd"      # adopted, so `name it once`
    assert res.session_set and res.worth_echoing        # …and never silently
    assert res.echo()["session_project_set"] == "seeded-from-cwd"


def test_a_later_path_answers_for_itself_and_re_homes_nothing():
    st = SessionState()
    st.current_project = "already-set"
    res = resolve_session_project(st, None, "/other", lambda _c: "elsewhere")
    assert (res.project, res.via) == ("elsewhere", "path")
    assert not res.session_set and not res.worth_echoing
    # THE bug: one cross-project read must not decide where later writes land
    assert st.current_project == "already-set"


def test_an_explicit_path_beats_the_sticky_project():
    # the reported failure: design_append(project_path=<other repo>) answered from
    # the sticky project and reported the note missing. project_path is if anything
    # the MORE specific selector, so it is not the one that yields.
    st = SessionState()
    st.current_project = "sticky"
    res = resolve_session_project(st, None, "/repo", lambda _c: "other-repo",
                                  default="default")
    assert (res.project, res.via) == ("other-repo", "path")
    assert not res.implicit
    assert st.current_project == "sticky"          # …and still does not re-home


def test_the_sticky_project_is_used_when_no_selector_is_given():
    st = SessionState()
    st.current_project = "chosen"
    called = []
    res = resolve_session_project(st, None, None, lambda c: called.append(c) or "X")
    assert (res.project, res.via) == ("chosen", "session")
    assert res.implicit
    assert called == []                            # seed not invoked when set


def test_a_path_deciding_nothing_falls_through_to_the_sticky_project():
    # no `.crib` under the path → the seed lands on the bare default, which the
    # caller did not ask for; the session is the better answer, and `session` keeps
    # the echo firing
    st = SessionState()
    st.current_project = "chosen"
    res = resolve_session_project(st, None, "/no/crib", lambda _c: "default",
                                  default="default")
    assert (res.project, res.via) == ("chosen", "session")


def test_a_path_that_resolves_to_the_bare_default_is_not_tagged_path():
    # project_path pointing somewhere with no `.crib` falls through to `default`.
    # That is NOT caller-directed: tagging it `path` made it non-implicit and muted
    # the wrong-project echo built for exactly this (the agent thinks it named a
    # repo; the answer came from `default`).
    st = SessionState()
    res = resolve_session_project(st, None, "/no/crib/here",
                                  lambda _c: "default", default="default")
    assert (res.project, res.via) == ("default", "seed")
    assert res.implicit                              # so the echo fires


def test_each_path_bearing_call_resolves_its_own_path():
    # adoption happens once; it never short-circuits a later call that named
    # somewhere else, which is what made project_path lose to stickiness
    st = SessionState()
    calls = []

    def seed(c):
        calls.append(c)
        return f"proj{c}"

    assert resolve_session_project(st, None, "/a", seed, default="default").project \
        == "proj/a"
    assert resolve_session_project(st, None, "/b", seed, default="default").project \
        == "proj/b"
    assert calls == ["/a", "/b"]
    assert st.current_project == "proj/a"          # adopted by the first, unmoved


def test_a_bare_default_never_adopts():
    # a stray early call with no cwd must not lock the session to `default`
    st = SessionState()
    res = resolve_session_project(st, None, None, lambda _c: "default",
                                  default="default")
    assert (res.project, res.via) == ("default", "seed")
    assert st.current_project is None
    # …so a later call carrying a real repo still adopts it
    later = resolve_session_project(st, None, "/repo", lambda _c: "real",
                                    default="default")
    assert (later.project, later.via, later.session_set) == ("real", "path", True)


def test_session_state_falls_back_to_default_without_context():
    # no MCP request context (CLI/tests) → shared default, not a crash
    st = session_state()
    assert isinstance(st, SessionState)
    assert session_state() is st                   # stable default instance


def test_only_the_no_cwd_fallthrough_is_unanchored():
    """No selector, no cwd AT ALL, no session — the one branch where the answer is
    a pure guess (only MCP can reach it; every chat lands there right after a
    daemon restart). It is marked `unanchored`, and the server layer asks or
    refuses rather than answering. A GIVEN cwd without a `.crib` is different:
    landing on `default` is that path's documented meaning — the CLI standing in
    ~ does it deliberately — so it seeds quietly, implicit but not a guess."""
    from crib.session import SessionState, resolve_session_project
    st = SessionState()
    res = resolve_session_project(st, None, None, seed=lambda cwd: "default",
                                  default="default")
    assert res.via == "seed" and res.unanchored
    assert "UNANCHORED" in res.echo().get("warning", "")
    assert res.worth_echoing                       # implicit → always surfaced

    # a REAL cwd that decides nothing: default by documentation, not by guess
    st_cli = SessionState()
    r_cli = resolve_session_project(st_cli, None, "/somewhere", 
                                    seed=lambda c: "default", default="default")
    assert r_cli.via == "seed" and not r_cli.unanchored

    # …and every ANCHORED branch: explicit, path-decided, session.
    assert not resolve_session_project(st, "p", None, seed=lambda c: "default",
                                       default="default").unanchored
    st2 = SessionState()
    r_path = resolve_session_project(st2, None, "/repo", seed=lambda c: "proj",
                                     default="default")
    assert not r_path.unanchored and r_path.via == "path"
    r_sess = resolve_session_project(st2, None, None, seed=lambda c: "default",
                                     default="default")
    assert r_sess.via == "session" and not r_sess.unanchored
    assert "warning" not in r_sess.echo()


def test_an_unanchored_read_refuses_with_the_recovery_in_the_message(
        tmp_path, monkeypatch):
    """The descriptive exception IS the mechanism: CribUserError rides the MCP face
    verbatim, the LLM reads it, and re-calling with project= is the recovery. The
    error must therefore carry everything needed to recover — the cause (sessions
    reset on restart), both selector spellings, and the project list."""
    import asyncio
    import pytest
    from crib import session as sess
    from crib.app import Crib
    from crib.config import Config
    from crib.paths import Paths
    from crib.store import InMemoryStore
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "i"))
    # outside an MCP request the shared _DEFAULT state stands in for the session;
    # a fresh one IS the fresh-session-after-restart condition under test
    monkeypatch.setattr(sess, "_DEFAULT", sess.SessionState())
    app = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
    from crib.server import build_server
    mcp = build_server(app)
    tool = {t.name: t for t in asyncio.run(mcp.list_tools())}["learning_report"]
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError) as e:
        asyncio.run(tool.run({}))                  # selector-less, fresh session
    msg = str(e.value)
    assert "no project is anchored" in msg
    assert "project=" in msg and "project_path=" in msg and "restart" in msg
