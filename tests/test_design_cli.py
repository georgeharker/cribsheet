"""The design/plan facet's CLI face: body authoring and the bare-noun defaults.

Two classes of friction this pins. First, AUTHORING: a design decision is a
paragraph or five, and the only way in used to be a shell-quoted positional —
miserable enough that it quietly pushes people toward one-line decisions. `--file`,
`-` and `$EDITOR` are the fix, and each must reach the tool call as the body.
Second, the bare noun: `crib design` is a real command (its orienting read), the
way `crib project` is — not an error telling you to pick a subcommand.
"""

from __future__ import annotations

import io

import pytest

from crib.cli import VERBS, _dispatch, build_parser


def _call(*argv):
    return _dispatch(build_parser().parse_args(list(argv)))


# ── bare nouns are the orienting read ─────────────────────────────────────────

@pytest.mark.parametrize("noun, tool", [("design", "design_list"),
                                        ("plan", "plan_list"),
                                        ("project", "project_status")])
def test_a_bare_noun_means_its_orienting_read(noun, tool):
    entry, call = _call(noun)
    assert entry.tool == tool
    # …and it survives the missing selectors: a bare noun's Namespace carries
    # neither -p nor -P, which is what `_proj_of` exists for
    assert call.get("project") is None


def test_the_bare_noun_and_the_explicit_verb_are_the_same_call():
    assert _call("design")[1] == _call("design", "list")[1]
    assert _call("plan")[1] == _call("plan", "list")[1]


# ── body authoring: positional, stdin, --file, $EDITOR ────────────────────────

def test_a_positional_body_passes_straight_through():
    _, call = _call("design", "add", "T", "the body")
    assert call["content"] == "the body"


def test_dash_reads_the_body_from_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("piped body\n"))
    _, call = _call("design", "add", "T", "-")
    assert call["content"] == "piped body\n"


def test_file_reads_the_body_from_a_path(tmp_path):
    p = tmp_path / "decision.md"
    p.write_text("# Why\n\nbecause.\n")
    _, call = _call("design", "add", "T", "--file", str(p))
    assert call["content"] == "# Why\n\nbecause.\n"
    # --file WINS over a positional: passing both is a mistake, and silently
    # preferring the inline one would discard the file the user pointed at
    _, call = _call("design", "add", "T", "ignored", "--file", str(p))
    assert call["content"].startswith("# Why")


@pytest.mark.parametrize("argv, key", [
    (["design", "add", "T"], "content"),
    (["design", "edit", "ref"], "new_content"),
    (["design", "append", "ref"], "content"),
])
def test_an_omitted_body_opens_the_editor_on_a_tty(monkeypatch, argv, key):
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("sys.stdout", type("T", (), {"isatty": lambda self: True,
                                                     "write": lambda self, s: None})())
    monkeypatch.setattr("crib.cli._from_editor", lambda what: f"edited: {what}")
    _, call = _call(*argv)
    assert call[key].startswith("edited: ")


def test_an_omitted_body_reads_stdin_in_a_pipeline(monkeypatch):
    """Not a tty: this is a pipeline, so read stdin rather than trying to open an
    editor into a pipe."""
    monkeypatch.setattr("sys.stdin", io.StringIO("from the pipe"))
    _, call = _call("design", "add", "T")
    assert call["content"] == "from the pipe"


def test_a_plan_item_may_be_title_only(monkeypatch):
    """A plan item's body is optional — an omitted one must NOT open an editor
    (that is the difference from a decision, whose rationale is the artifact)."""
    monkeypatch.setattr("crib.cli._from_editor",
                        lambda what: pytest.fail("editor opened for a plan item"))
    _, call = _call("plan", "add", "wire up the emitter")
    assert call["content"] == "" and call["items"] is None


def test_repeated_item_flags_build_a_batch():
    _, call = _call("plan", "add", "first", "the body", "--item", "second",
                    "--item", "third")
    assert [i["title"] for i in call["items"]] == ["first", "second", "third"]
    assert call["items"][0]["content"] == "the body"


# ── source attribution + the import tier ──────────────────────────────────────

@pytest.mark.parametrize("argv", [
    ["design", "add", "T", "body"],
    ["design", "edit", "ref", "body"],
    ["plan", "add", "T"],
])
def test_source_flags_reach_the_call_repeatably(argv):
    _, call = _call(*argv, "--source", "DESIGN.md#4. Coordination",
                    "--source", "DESIGN.md#10.3 Fusion")
    assert call["sources"] == ["DESIGN.md#4. Coordination", "DESIGN.md#10.3 Fusion"]


def test_a_source_survives_the_batch_form():
    """`--item` switches `plan add` to the batch shape — the first item must keep
    the citation it was given rather than dropping it on the way."""
    _, call = _call("plan", "add", "first", "body", "--source", "doc.md#Bit",
                    "--item", "second")
    assert call["items"][0]["sources"] == ["doc.md#Bit"]


def test_the_import_tier_is_opt_in_on_the_cli():
    assert _call("design", "add", "T", "body")[1]["proposed"] is False
    assert _call("design", "add", "T", "body", "--proposed")[1]["proposed"] is True


@pytest.mark.parametrize("argv, tool, call", [
    (["design", "import", "DESIGN.md"], "design_import", {"relpath": "DESIGN.md"}),
    (["plan", "import", "plan.md"], "plan_import", {"relpath": "plan.md"}),
    (["design", "promote", "ref"], "design_promote", {"ref": "ref"}),
])
def test_the_new_verbs_dispatch(argv, tool, call):
    entry, got = _call(*argv)
    assert entry.tool == tool and {k: got[k] for k in call} == call


# ── the rename left nothing behind ────────────────────────────────────────────

def test_reaffirm_replaced_verify_outright():
    assert "design reaffirm" in VERBS and "design verify" not in VERBS
    assert VERBS["design reaffirm"].tool == "design_reaffirm"
    with pytest.raises(SystemExit):             # no alias: the old spelling is gone
        build_parser().parse_args(["design", "verify", "ref"])
