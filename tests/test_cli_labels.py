"""`_split_labels` must distinguish flag-absent from explicit-empty.

The bug this pins: `--keywords ""` (an eval baseline forcing keyword_index OFF)
returned None, which `lookup()` reads as "use the config default" — and the
default is `keyword_labels=["keywords"]`, i.e. ON. So the `--lift keywords`
baseline ran with keywords already on, identical to the withl arm → a Δ0 false
null. Only an *absent* flag (None) may fall back to the default; an explicit ""
means "no labels".
"""

from crib.cli import _split_labels


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
