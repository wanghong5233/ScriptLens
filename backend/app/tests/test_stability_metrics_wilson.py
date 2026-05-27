from __future__ import annotations

from eval.stability.metrics import stable_count_and_wilson


def test_stable_count_and_wilson_all_equal() -> None:
    matrix = [
        ["a", "b", "c"],
        ["a", "b", "c"],
        ["a", "b", "c"],
        ["a", "b", "c"],
        ["a", "b", "c"],
    ]
    n_samples, stable_count, wilson_lower = stable_count_and_wilson(matrix, confidence=0.95)
    assert n_samples == 3
    assert stable_count == 3
    assert 0.43 < wilson_lower <= 1.0


def test_stable_count_and_wilson_all_different() -> None:
    matrix = [
        ["a", "b", "c"],
        ["x", "y", "z"],
        ["u", "v", "w"],
        ["m", "n", "o"],
        ["p", "q", "r"],
    ]
    n_samples, stable_count, wilson_lower = stable_count_and_wilson(matrix, confidence=0.95)
    assert n_samples == 3
    assert stable_count == 0
    assert wilson_lower == 0.0
