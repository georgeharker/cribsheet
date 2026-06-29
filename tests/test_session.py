"""Per-connection session project resolution (precedence + seeding)."""

from crib.session import SessionState, resolve_session_project, session_state


def _seed(_cwd):
    return "seeded-from-cwd"


def test_explicit_arg_overrides_without_touching_session():
    st = SessionState()
    st.current_project = "sticky"
    assert resolve_session_project(st, "explicit", None, _seed) == "explicit"
    assert st.current_project == "sticky"          # override didn't change session


def test_seeds_lazily_and_sticks():
    st = SessionState()
    assert st.current_project is None
    assert resolve_session_project(st, None, "/some/cwd", _seed) == "seeded-from-cwd"
    assert st.current_project == "seeded-from-cwd"  # stuck

    # later call with a *different* cwd reuses the stuck value (sticky, not re-seeded)
    assert resolve_session_project(st, None, "/other", _seed) == "seeded-from-cwd"


def test_session_current_used_over_seed():
    st = SessionState()
    st.current_project = "chosen"
    called = []
    assert resolve_session_project(st, None, "/x", lambda c: called.append(c) or "X") \
        == "chosen"
    assert called == []                            # seed not invoked when set


def test_session_state_falls_back_to_default_without_context():
    # no MCP request context (CLI/tests) → shared default, not a crash
    st = session_state()
    assert isinstance(st, SessionState)
    assert session_state() is st                   # stable default instance
