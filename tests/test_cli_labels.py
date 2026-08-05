"""`_split_labels` must distinguish flag-absent from explicit-empty.

The bug this pins: `--keywords ""` (an eval baseline forcing keyword_index OFF)
returned None, which `lookup()` reads as "use the config default" — and the
default is `keyword_labels=["keywords"]`, i.e. ON. So the `--lift keywords`
baseline ran with keywords already on, identical to the withl arm → a Δ0 false
null. Only an *absent* flag (None) may fall back to the default; an explicit ""
means "no labels".
"""

from crib.cli import _dispatch, _split_labels, build_parser


def test_absent_flag_is_none_use_default():
    assert _split_labels(None) is None


def test_explicit_empty_disables():
    # "" (and whitespace-only) → [] so lookup() overrides the config default OFF.
    assert _split_labels("") == []
    assert _split_labels("  ") == []


def test_labels_parse_and_trim():
    assert _split_labels("keywords") == ["keywords"]
    assert _split_labels("a, b ,c") == ["a", "b", "c"]
    assert _split_labels("a,,b,") == ["a", "b"]


# …and the same distinction must survive the CLI's arg → tool-call mapping, which
# is where it was actually lost: `_b_lookup` gated on truthiness, so the explicit
# "" was dropped and the config default applied after all — the disable-semantics
# above were dead code end-to-end.

def _call(*argv):
    entry, call = _dispatch(build_parser().parse_args(["note", "lookup", "q", *argv]))
    return call


def test_explicit_empty_reaches_the_tool_call_as_no_labels():
    assert _call("--keywords", "")["keyword_labels"] == []
    assert _call("--summaries", "")["summary_labels"] == []


def test_absent_flag_is_omitted_from_the_tool_call():
    # omitted entirely (not None), so the method/[retrieve] default decides
    call = _call()
    assert "keyword_labels" not in call and "summary_labels" not in call


def test_given_labels_pass_through():
    assert _call("--keywords", "keywords,phrase")["keyword_labels"] == ["keywords",
                                                                       "phrase"]
    assert _call("--summaries", "gist")["summary_labels"] == ["gist"]
