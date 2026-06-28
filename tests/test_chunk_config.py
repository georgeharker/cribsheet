"""Tunable chunk windowing: overlap is a stable ratio, threaded into chunking."""

from crib.chunk import _window
from crib.config import ChunkConfig


def test_overlap_words_derived_from_ratio():
    assert ChunkConfig(window_words=320, overlap_ratio=0.20).overlap_words == 64
    # ratio holds steady when the window changes (the whole point)
    assert ChunkConfig(window_words=256, overlap_ratio=0.20).overlap_words == 51


def test_overlap_clamped_below_window():
    # an absurd ratio is capped (to 0.9) so the windowing step can never stall
    cfg = ChunkConfig(window_words=100, overlap_ratio=5.0)
    assert cfg.overlap_words == 90
    assert cfg.overlap_words < cfg.window_words


def test_window_respects_params_and_overlaps():
    words = [f"w{i}" for i in range(500)]
    text = " ".join(words)
    wins = _window(text, window_words=200, overlap=50)
    assert len(wins) > 1
    # adjacent windows share `overlap` words: window starts step=150 apart
    assert wins[1].split()[0] == "w150"
    assert wins[0].split()[-50] == wins[1].split()[0]  # the shared overlap region
