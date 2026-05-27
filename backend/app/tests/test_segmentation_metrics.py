from __future__ import annotations

from eval.stability.segmentation_metrics import boundary_similarity, window_diff


def test_window_diff_fn_fp_fnp_fixtures() -> None:
    # Reference segmentation (3 boundaries).
    ref = {3, 6, 9}
    # FN: miss one true boundary.
    fn = {3, 6}
    # FP: introduce one extra boundary.
    fp = {3, 6, 8, 9}
    # FNP: globally shifted boundaries.
    fnp = {2, 5, 8}

    wd_fn = window_diff(ref, fn, n_scenes=12)
    wd_fp = window_diff(ref, fp, n_scenes=12)
    wd_fnp = window_diff(ref, fnp, n_scenes=12)

    # FN/FP should be local errors; FNP is global drift and should be significantly worse.
    assert wd_fn == 0.1
    assert wd_fp == 0.1
    assert wd_fnp == 0.6


def test_boundary_similarity_is_symmetric() -> None:
    left = {3, 6, 9}
    right = {2, 6, 10}
    a = boundary_similarity(left, right, near_miss=2)
    b = boundary_similarity(right, left, near_miss=2)
    assert 0.0 <= a <= 1.0
    assert 0.0 <= b <= 1.0
    assert a == b
    assert boundary_similarity(left, left, near_miss=2) == 1.0
