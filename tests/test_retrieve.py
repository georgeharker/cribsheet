"""BM25 lexical scoring and reciprocal-rank fusion."""

from crib.retrieve import BM25, reciprocal_rank_fusion, tokenize


def _corpus():
    return [
        tokenize("restart a single backing server with MCPRestartServer"),  # 0: exact
        tokenize("oauth verify the persistent connection in the combiner log"),  # 1: off
        tokenize("the combiner manages per-server processes and lifecycles"),  # 2: near
    ]


def test_bm25_ranks_exact_term_match_first():
    scores = BM25(_corpus()).scores(tokenize("restart server"))
    assert scores[0] == max(scores)          # the doc with both terms wins
    assert scores[0] > scores[1]


def test_bm25_unknown_terms_score_zero():
    assert BM25(_corpus()).scores(tokenize("kubernetes helm")) == [0.0, 0.0, 0.0]


def test_rrf_rewards_agreement_across_rankings():
    # 'b' is mid in both lists; 'a' tops one, 'd' tops the other.
    dense = ["a", "b", "c", "d"]
    sparse = ["d", "b", "x", "a"]
    fused = reciprocal_rank_fusion([dense, sparse], k=60)
    # 'b' (2nd in both) beats 'c'/'x' (appear once) and is near the top
    assert fused.index("b") <= 1
    assert fused.index("b") < fused.index("c")
    assert fused.index("b") < fused.index("x")


def test_rrf_union_includes_all_ids():
    fused = reciprocal_rank_fusion([["a", "b"], ["b", "c"]])
    assert set(fused) == {"a", "b", "c"}
    assert fused[0] == "b"                    # only id in both lists
