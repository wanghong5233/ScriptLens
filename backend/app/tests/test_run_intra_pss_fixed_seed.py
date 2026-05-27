from __future__ import annotations

import asyncio

from eval.stability import runner


class _FakeExpDir:
    def __init__(self) -> None:
        self.append_calls: list[tuple] = []

    def layer_b_value_map(self, script_id: str, scope: str, dim: str, rep: int) -> dict[str, str]:  # noqa: ARG002
        return {}

    def is_layer_b_done(  # noqa: ARG002
        self,
        script_id: str,
        scope: str,
        dim: str,
        rep: int,
        *,
        target_count: int | None = None,
    ) -> bool:
        return False

    def append_layer_b_raw(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.append_calls.append((args, kwargs))


def test_run_intra_pss_fixed_seed_and_raw_persist(monkeypatch) -> None:
    calls: list[dict] = []

    async def _fake_extractor(target_id: str, tag_set_ver: str, prompt_ver: str, seed: int, variant: str, use_cache: bool = True):  # noqa: ARG001
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
        return {"world_setting": f"value-{target_id}", "__model_ver": "qwen3-max"}

    monkeypatch.setattr(runner, "_persist_run", lambda **kwargs: None)
    runner.ExtractorRegistry.register("v2.0.0", "script", _fake_extractor)
    task = runner.StabilityTask(
        tag_set_ver="v2.0.0",
        dim="world_setting",
        scope="script",
        targets=["sid-1", "sid-2"],
    )
    exp_dir = _FakeExpDir()
    result = asyncio.run(
        runner.run_intra_pss(
            task,
            seed=42,
            n_repeats=5,
            exp_dir=exp_dir,  # type: ignore[arg-type]
            use_cache=False,
        )
    )

    assert len(result.intra_runs) == 5
    assert all(trace.run_key.startswith("rep:") for trace in result.intra_runs)
    assert len(calls) == 10
    assert all(call["seed"] == 42 for call in calls)
    assert all(call["variant"] == "a" for call in calls)
    assert all(call["use_cache"] is False for call in calls)
    assert len(exp_dir.append_calls) == 10
