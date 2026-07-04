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


def test_bare_default_seed_is_upgraded_by_a_later_cwd():
    # a stray early call with no cwd seeds the session to `default`...
    st = SessionState()
    seeds = iter(["default", "real-project"])
    seed = lambda _cwd: next(seeds)
    assert resolve_session_project(st, None, None, seed, default="default") == "default"
    # ...and a later call carrying a cwd/.crib UPGRADES it off the bare default
    assert resolve_session_project(st, None, "/repo", seed, default="default") \
        == "real-project"
    assert st.current_project == "real-project"


def test_a_real_seed_still_sticks_against_a_later_cwd():
    # only the bare default is re-seeded; a real project stays sticky
    st = SessionState()
    calls = []
    seed = lambda c: calls.append(c) or "svg-mcp"
    assert resolve_session_project(st, None, "/a", seed, default="default") == "svg-mcp"
    assert resolve_session_project(st, None, "/b", seed, default="default") == "svg-mcp"
    assert calls == ["/a"]                          # not re-seeded on the second call


def test_session_state_falls_back_to_default_without_context():
    # no MCP request context (CLI/tests) → shared default, not a crash
    st = session_state()
    assert isinstance(st, SessionState)
    assert session_state() is st                   # stable default instance
