from __future__ import annotations

from eval.stability.experiment_dir import ExperimentDir


def test_experiment_dir_pending_skips_completed_items(tmp_path) -> None:
    exp = ExperimentDir.create(
        tag_set_ver="v2.0.0",
        provider="dashscope",
        model="qwen3-max",
        seed=42,
        temperature=0.0,
        n_repeats=3,
        scripts=["s1", "s2", "s3"],
        cache_disabled=True,
        run_id="resume_case",
        root=tmp_path,
    )
    exp.save_layer_a_raw("s1", 0, [{"start_scene_id": "scene-1"}])
    exp.save_layer_a_raw("s1", 1, [{"start_scene_id": "scene-1"}])
    exp.append_layer_b_raw("s1", "script", "dim_x", 0, "s1", "v1", {"model_ver": "qwen3-max"})

    resumed = ExperimentDir.load("resume_case", root=tmp_path)
    pending_a = resumed.pending_layer_a(["s1", "s2", "s3"], n_repeats=3)
    assert ("s1", 0) not in pending_a
    assert ("s1", 1) not in pending_a
    assert ("s1", 2) in pending_a
    assert ("s2", 0) in pending_a

    pending_b = resumed.pending_layer_b(
        ["s1", "s2", "s3"],
        [("script", "dim_x")],
        n_repeats=3,
        target_counts={("s1", "script", "dim_x"): 1, ("s2", "script", "dim_x"): 1, ("s3", "script", "dim_x"): 1},
    )
    assert ("s1", "script", "dim_x", 0) not in pending_b
    assert ("s1", "script", "dim_x", 1) in pending_b
    assert ("s2", "script", "dim_x", 0) in pending_b
