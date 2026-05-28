from __future__ import annotations

import asyncio

from eval.stability import runner


class _FakeExpDir:
    def __init__(self) -> None:
        self.append_calls: list[tuple] = []

    def tag_value_map(self, script_id: str, scope: str, dim: str, rep: int) -> dict[str, str]:  # noqa: ARG002
        return {}

    def is_tag_value_done(  # noqa: ARG002
        self,
        script_id: str,
        scope: str,
        dim: str,
        rep: int,
        *,
        target_count: int | None = None,
    ) -> bool:
        return False

    def append_tag_value_raw(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.append_calls.append((args, kwargs))


def test_run_bundle_stability_fixed_seed_single_call_per_bundle(monkeypatch) -> None:
    calls: list[dict] = []

    async def _fake_extractor(
        target_id: str,
        tag_set_ver: str,
        prompt_ver: str,
        seed: int,
        variant: str,
        use_cache: bool = True,  # noqa: ARG001
    ):
        calls.append(
            {
                "target_id": target_id,
                "seed": seed,
                "variant": variant,
                "use_cache": use_cache,
                "prompt_ver": prompt_ver,
                "tag_set_ver": tag_set_ver,
            }
        )
        return {
            "world_setting": f"world-{target_id}",
            "pacing_mode": "high_density_conflict",
            "__model_ver": "qwen3-max",
        }

    monkeypatch.setattr(runner, "_persist_run", lambda **kwargs: None)
    runner.ExtractorRegistry.register("script", "script", _fake_extractor)
    task = runner.BundleStabilityTask(
        tag_set_ver="script",
        bundle_id="script_structure",
        scope="script",
        dims=["world_setting", "pacing_mode"],
        targets=["sid-1", "sid-2"],
    )
    exp_dir = _FakeExpDir()
    results = asyncio.run(
        runner.run_bundle_stability(
            task,
            seed=42,
            n_repeats=5,
            exp_dir=exp_dir,  # type: ignore[arg-type]
            use_cache=False,
        )
    )

    world = results["world_setting"]
    pacing = results["pacing_mode"]
    assert len(world.intra_runs) == 5
    assert len(pacing.intra_runs) == 5
    assert all(trace.run_key.startswith("rep:") for trace in world.intra_runs)
    assert all(trace.run_key.startswith("rep:") for trace in pacing.intra_runs)
    assert len(calls) == 10
    assert all(call["seed"] == 42 for call in calls)
    assert all(call["variant"] == "a" for call in calls)
    assert all(call["use_cache"] is False for call in calls)
    assert len(exp_dir.append_calls) == 20
