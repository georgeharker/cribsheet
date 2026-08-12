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
