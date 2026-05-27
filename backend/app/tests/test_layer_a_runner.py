from __future__ import annotations

from eval.stability.experiment_dir import ExperimentDir
from eval.stability.layer_a_runner import run_layer_a_repeat
from service.script_tools.plot_unit_segmenter import SegmentedPlotUnit


def _make_units(script_id: str, starts: list[str]) -> list[SegmentedPlotUnit]:
    out: list[SegmentedPlotUnit] = []
    for idx, start in enumerate(starts, start=1):
        out.append(
            SegmentedPlotUnit(
                id=f"u-{idx}-{start}",
                script_id=script_id,
                episode_no=1,
                idx=idx,
                start_scene_id=start,
                end_scene_id=start,
                start_line=idx,
                end_line=idx,
                summary=f"unit {idx}",
                char_count=10,
            )
        )
    return out


def test_layer_a_runner_stable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "eval.stability.layer_a_runner._scene_index",
        lambda script_id, engine: (
            ["s1", "s2", "s3", "s4", "s5"],
            {"s1": 0, "s2": 1, "s3": 2, "s4": 3, "s5": 4},
        ),
    )
    reps = [_make_units("script-1", ["s1", "s3", "s5"]) for _ in range(5)]

    async def _fake_segment_plot_units(*args, **kwargs):  # noqa: ANN003
        return reps.pop(0)

    monkeypatch.setattr("eval.stability.layer_a_runner.segment_plot_units", _fake_segment_plot_units)
    exp_dir = ExperimentDir.create(
        tag_set_ver="v2.0.0",
        provider="dashscope",
        model="qwen3-max",
        seed=42,
        temperature=0.0,
        n_repeats=5,
        scripts=["script-1"],
        cache_disabled=True,
        run_id="layer_a_stable",
        root=tmp_path,
    )

    import asyncio

    report = asyncio.run(
        run_layer_a_repeat(
            "script-1",
            seed=42,
            n_repeats=5,
            tag_set_ver="v2.0.0",
            exp_dir=exp_dir,
            use_cache=False,
        )
    )
    assert report.verdict == "stable"
    assert report.unit_count_cv == 0.0
    assert report.window_diff_mean == 0.0


def test_layer_a_runner_unstable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "eval.stability.layer_a_runner._scene_index",
        lambda script_id, engine: (
            ["s1", "s2", "s3", "s4", "s5"],
            {"s1": 0, "s2": 1, "s3": 2, "s4": 3, "s5": 4},
        ),
    )
    reps = [
        _make_units("script-2", ["s1", "s3", "s5"]),
        _make_units("script-2", ["s1", "s2", "s3", "s4", "s5"]),
        _make_units("script-2", ["s1", "s4"]),
        _make_units("script-2", ["s1", "s3"]),
        _make_units("script-2", ["s1", "s2", "s5"]),
    ]

    async def _fake_segment_plot_units(*args, **kwargs):  # noqa: ANN003
        return reps.pop(0)

    monkeypatch.setattr("eval.stability.layer_a_runner.segment_plot_units", _fake_segment_plot_units)
    exp_dir = ExperimentDir.create(
        tag_set_ver="v2.0.0",
        provider="dashscope",
        model="qwen3-max",
        seed=42,
        temperature=0.0,
        n_repeats=5,
        scripts=["script-2"],
        cache_disabled=True,
        run_id="layer_a_unstable",
        root=tmp_path,
    )

    import asyncio

    report = asyncio.run(
        run_layer_a_repeat(
            "script-2",
            seed=42,
            n_repeats=5,
            tag_set_ver="v2.0.0",
            exp_dir=exp_dir,
            use_cache=False,
        )
    )
    assert report.verdict == "unstable"
    assert report.unit_count_cv >= 0.25 or report.window_diff_mean >= 0.40
