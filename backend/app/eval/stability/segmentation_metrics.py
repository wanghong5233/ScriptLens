from __future__ import annotations

from typing import Callable

import numpy as np


def _count_in_window(boundaries: set[int], start_idx: int, end_idx: int) -> int:
    # boundary index j means split before scene[j], j in [1, n_scenes-1].
    # For a scene window [start_idx, end_idx), valid boundaries satisfy start_idx < j < end_idx.
    return sum(1 for boundary in boundaries if start_idx < boundary < end_idx)


def window_diff(ref: set[int], hyp: set[int], n_scenes: int, k: int | None = None) -> float:
    if n_scenes <= 1:
        return 0.0
    if k is None:
        k = max(2, round(n_scenes / (2 * (len(ref) + 1))))
    k = max(1, min(int(k), n_scenes))
    n_windows = max(1, n_scenes - k)
    errors = 0
    for start_idx in range(n_windows):
        end_idx = start_idx + k
        c_ref = _count_in_window(ref, start_idx, end_idx)
        c_hyp = _count_in_window(hyp, start_idx, end_idx)
        if c_ref != c_hyp:
            errors += 1
    return float(errors / n_windows)


def boundary_similarity(ref: set[int], hyp: set[int], near_miss: int = 2) -> float:
    if not ref and not hyp:
        return 1.0
    if not ref or not hyp:
        return 0.0

    ref_sorted = sorted(ref)
    hyp_sorted = sorted(hyp)
    candidate_pairs: list[tuple[int, int, int]] = []
    for r in ref_sorted:
        for h in hyp_sorted:
            dist = abs(r - h)
            if dist <= near_miss:
                candidate_pairs.append((dist, r, h))
    candidate_pairs.sort(key=lambda item: (item[0], item[1], item[2]))

    used_ref: set[int] = set()
    used_hyp: set[int] = set()
    matched = 0.0
    substitutions = 0.0
    for dist, r, h in candidate_pairs:
        if r in used_ref or h in used_hyp:
            continue
        used_ref.add(r)
        used_hyp.add(h)
        if dist == 0:
            matched += 1.0
        else:
            # near-miss counts as half credit to tolerate boundary jitter.
            matched += 0.5
            substitutions += 0.5

    additions = float(len(hyp_sorted) - len(used_hyp))
    deletions = float(len(ref_sorted) - len(used_ref))
    denom = matched + substitutions + additions + deletions
    if denom <= 0:
        return 1.0
    return float(matched / denom)


def pairwise_mean(metric_fn: Callable[..., float], sets: list[set[int]], **kwargs) -> float:
    if len(sets) < 2:
        return 0.0
    values: list[float] = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            values.append(float(metric_fn(sets[i], sets[j], **kwargs)))
    return float(np.mean(values)) if values else 0.0
