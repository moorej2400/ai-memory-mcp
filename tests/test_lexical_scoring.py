from __future__ import annotations

from ai_memory_mcp.retrieval import _lexical_score

# SQLite FTS5 bm25() returns negative values, and a more negative value is a
# better match. Real values observed on a live corpus for one query.
LIVE_BM25 = [-3.5716, -3.4428, -3.4424, -3.4423]


def test_better_match_scores_higher() -> None:
    assert _lexical_score(-3.5716) > _lexical_score(-0.9)


def test_distinct_bm25_values_keep_distinct_scores() -> None:
    scores = [_lexical_score(value) for value in LIVE_BM25]
    assert len(set(scores)) == len(scores), "score signal must not flatten"
    assert scores == sorted(scores, reverse=True)


def test_scores_stay_bounded() -> None:
    for value in (-1000.0, -3.5, -0.001, 0.0, 2.0):
        assert 0.0 <= _lexical_score(value) <= 1.0
