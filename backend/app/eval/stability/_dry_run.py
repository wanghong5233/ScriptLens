from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from eval.stability.report import aggregate, write_markdown
from eval.stability.runner import ExtractorRegistry, StabilityTask, run_full
from service.tag_registry.loader import load_tag_set


async def _mock_extractor(target_id: str, tag_set_ver: str, prompt_ver: str, seed: int, variant: str) -> dict:
    cfg = load_tag_set(tag_set_ver)
    dim = prompt_ver.split(":")[1]
    dim_cfg = cfg.get_dim(dim)
    values = list(dim_cfg.values) or ["none"]
    idx = int(hashlib.md5(f"{target_id}|{seed}|{variant}|{dim}".encode("utf-8")).hexdigest(), 16) % len(values)
    return {dim: values[idx], "__model_ver": "mock-model"}


async def main_async() -> None:
    tag_set_ver = "v0.1.0"
    cfg = load_tag_set(tag_set_ver)
    scope = "plot_unit"
    ExtractorRegistry.register(tag_set_ver, scope, _mock_extractor)

    run_results = {}
    targets = [f"demo_plot_{i}" for i in range(1, 11)]
    for dim_cfg in cfg.scope_to_dims[scope]:
        task = StabilityTask(
            tag_set_ver=tag_set_ver,
            dim=dim_cfg.dim,
            scope=scope,
            targets=targets,
        )
        run_results[dim_cfg.dim] = await run_full(task)

    reports = aggregate(run_results)
    out = Path("ScriptLens/eval/reports/v0_stability_dryrun.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    write_markdown(reports, str(out), tag_set_ver=tag_set_ver, split="dryrun")
    print(f"dry run report written: {out}")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
