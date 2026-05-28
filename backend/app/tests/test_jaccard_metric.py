from __future__ import annotations

from eval.stability.metrics import jaccard_stable_count_and_wilson, pairwise_jaccard


def test_pairwise_jaccard_perfect_match() -> None:
    matrix = [
        ['["a","b"]', '["x"]'],
        ['["a","b"]', '["x"]'],
        ['["a","b"]', '["x"]'],
    ]
    assert pairwise_jaccard(matrix) == 1.0


def test_pairwise_jaccard_all_disjoint() -> None:
    matrix = [
        ['["a"]', '["x"]'],
        ['["b"]', '["y"]'],
        ['["c"]', '["z"]'],
    ]
    assert pairwise_jaccard(matrix) == 0.0


def test_jaccard_stable_count_and_wilson_threshold() -> None:
    matrix = [
        ['["a","b"]', '["x"]'],
        ['["a","b"]', '["y"]'],
        ['["a","b"]', '["z"]'],
    ]
    n_samples, stable_count, wilson_lower = jaccard_stable_count_and_wilson(matrix, threshold=0.7)
    assert n_samples == 2
    assert stable_count == 1
    assert 0.0 <= wilson_lower <= 1.0
