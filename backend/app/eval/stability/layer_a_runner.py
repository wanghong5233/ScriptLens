from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from sqlalchemy import text
from sqlalchemy.engine import Engine

from eval.stability.experiment_dir import ExperimentDir
from eval.stability.segmentation_metrics import boundary_similarity, pairwise_mean, window_diff
from service.script_tools.plot_unit_segmenter import SegmentedPlotUnit, segment_plot_units
from utils.database import engine as default_engine

LayerAVerdict = Literal["stable", "fixable", "unstable"]


@dataclass
class LayerAReport:
    script_id: str
    n_scenes: int
    unit_count_mean: float
    unit_count_cv: float
    window_diff_mean: float
    boundary_similarity_mean: float
    verdict: LayerAVerdict


def _scene_index(script_id: str, *, engine: Engine) -> tuple[list[str], dict[str, int]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id
                FROM scriptlens.scenes
                WHERE script_id::text = :sid
                ORDER BY episode_no NULLS LAST, scene_no, start_line
                """
            ),
            {"sid": script_id},
        ).mappings().all()
    scene_ids = [str(row["id"]) for row in rows]
    return scene_ids, {scene_id: idx for idx, scene_id in enumerate(scene_ids)}


def _unit_start_scene(unit: dict[str, Any] | SegmentedPlotUnit) -> str:
    if isinstance(unit, dict):
        return str(unit.get("start_scene_id") or "")
    return str(unit.start_scene_id or "")


def _boundaries_from_units(units: list[dict[str, Any] | SegmentedPlotUnit], scene_to_index: dict[str, int]) -> set[int]:
    boundaries: set[int] = set()
    for unit in units:
        start_scene_id = _unit_start_scene(unit)
        if not start_scene_id:
            continue
        idx = scene_to_index.get(start_scene_id)
        if idx is None:
            continue
        if idx > 0:
            boundaries.add(idx)
    return boundaries


def _layer_a_verdict(*, unit_count_cv: float, window_diff_mean: float) -> LayerAVerdict:
    if window_diff_mean <= 0.15 and unit_count_cv <= 0.10:
        return "stable"
    if window_diff_mean >= 0.40 or unit_count_cv >= 0.25:
        return "unstable"
    return "fixable"


async def run_layer_a_repeat(
    script_id: str,
    *,
    seed: int = 42,
    n_repeats: int = 5,
    tag_set_ver: str = "v2.0.0",
    exp_dir: ExperimentDir,
    use_cache: bool = False,
    engine: Engine = default_engine,
) -> LayerAReport:
    scene_ids, scene_to_index = _scene_index(script_id, engine=engine)
    n_scenes = len(scene_ids)
    rep_units: list[list[dict[str, Any]]] = []

    prev_cache_flag = os.getenv("SM_STABILITY_DISABLE_CACHE")
    if not use_cache:
        os.environ["SM_STABILITY_DISABLE_CACHE"] = "1"
    try:
        for rep in range(n_repeats):
            if exp_dir.is_layer_a_done(script_id, rep):
                rep_units.append(exp_dir.load_layer_a_raw(script_id, rep))
                continue
            units = await segment_plot_units(
                script_id,
                tag_set_ver=tag_set_ver,
                seed=seed,
                variant="a",
                persist=False,
                engine=engine,
            )
            exp_dir.save_layer_a_raw(script_id, rep, units)
            rep_units.append(exp_dir.load_layer_a_raw(script_id, rep))
    finally:
        if not use_cache:
            if prev_cache_flag is None:
                os.environ.pop("SM_STABILITY_DISABLE_CACHE", None)
            else:
                os.environ["SM_STABILITY_DISABLE_CACHE"] = prev_cache_flag

    unit_counts = [len(units) for units in rep_units]
    unit_count_mean = float(np.mean(unit_counts)) if unit_counts else 0.0
    unit_count_std = float(np.std(unit_counts)) if unit_counts else 0.0
    unit_count_cv = float(unit_count_std / unit_count_mean) if unit_count_mean > 0 else 0.0
    boundaries = [_boundaries_from_units(units, scene_to_index) for units in rep_units]
    wd_mean = pairwise_mean(window_diff, boundaries, n_scenes=max(1, n_scenes))
    b_mean = pairwise_mean(boundary_similarity, boundaries, near_miss=2)
    verdict = _layer_a_verdict(unit_count_cv=unit_count_cv, window_diff_mean=wd_mean)
    return LayerAReport(
        script_id=script_id,
        n_scenes=n_scenes,
        unit_count_mean=unit_count_mean,
        unit_count_cv=unit_count_cv,
        window_diff_mean=wd_mean,
        boundary_similarity_mean=b_mean,
        verdict=verdict,
    )
