from __future__ import annotations

import argparse
import asyncio
import hashlib
from pathlib import Path

from eval.stability.report import aggregate, write_markdown, write_per_dim_json
from eval.stability.runner import ExtractorRegistry, StabilityTask, run_full
from eval.stability.sampler import sample_split
from service.tag_registry.loader import load_tag_set


def _build_targets(scope: str, script_ids: list[str], episodes_by_script: dict[str, list[int]]) -> list[str]:
    if scope == "script":
        return script_ids
    if scope == "episode":
        out: list[str] = []
        for sid in script_ids:
            for ep in episodes_by_script.get(sid, []):
                out.append(f"{sid}::ep::{ep}")
        return out
    if scope == "plot_unit":
        out = []
        for sid in script_ids:
            for idx in range(1, 7):
                out.append(f"{sid}::plot::{idx}")
        return out
    if scope == "character":
        out = []
        for sid in script_ids:
            for idx in range(1, 5):
                out.append(f"{sid}::char::{idx}")
        return out
    if scope == "relationship":
        out = []
        for sid in script_ids:
            for idx in range(1, 5):
                out.append(f"{sid}::rel::{idx}")
        return out
    return script_ids


async def _mock_extractor(
    target_id: str,
    tag_set_ver: str,
    prompt_ver: str,
    seed: int,
    variant: str,
) -> dict:
    cfg = load_tag_set(tag_set_ver)
    # prompt_ver format: "{prompt_ver}:{dim}:{variant}"
    parts = prompt_ver.split(":")
    dim = parts[1] if len(parts) >= 2 else ""
    dim_cfg = cfg.get_dim(dim)
    values = list(dim_cfg.values)
    if not values:
        return {dim: "none", "__model_ver": "mock-model"}
    digest = hashlib.sha256(f"{target_id}|{seed}|{variant}|{dim}".encode("utf-8")).hexdigest()
    idx = int(digest[:8], 16) % len(values)
    return {dim: values[idx], "__model_ver": "mock-model"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run stability evaluation for tag dimensions.")
    parser.add_argument("--tag-set", required=True, dest="tag_set")
    parser.add_argument("--split", default="dev", choices=["dev", "test"])
    parser.add_argument("--dims", default="", help="Comma-separated dims. Empty means all dims in selected scope(s).")
    parser.add_argument("--scope", default="", help="script|episode|plot_unit|character|relationship")
    parser.add_argument("--output", default="", help="Markdown report output path")
    parser.add_argument(
        "--source-dir",
        default="ScriptLens/eval/爆款短剧剧本（完整本）",
        help="Script dataset directory",
    )
    return parser.parse_args()


async def _main_async() -> None:
    args = _parse_args()
    cfg = load_tag_set(args.tag_set)

    samples = sample_split(args.split, source_dir=args.source_dir)
    if not samples:
        raise RuntimeError(f"no samples found under {args.source_dir}")

    script_ids = [s.script_id for s in samples]
    episodes_by_script = {s.script_id: s.episodes_sampled for s in samples}

    scopes = [args.scope] if args.scope else list(cfg.scope_to_dims.keys())
    dims_filter = {d.strip() for d in args.dims.split(",") if d.strip()}

    if not dims_filter:
        for scope in scopes:
            for dim_cfg in cfg.scope_to_dims.get(scope, ()):
                dims_filter.add(dim_cfg.dim)

    # If no real extractor is registered yet, use deterministic mock extractor.
    for scope in scopes:
        if not ExtractorRegistry.has(args.tag_set, scope):
            ExtractorRegistry.register(args.tag_set, scope, _mock_extractor)

    run_results = {}
    for scope in scopes:
        targets = _build_targets(scope, script_ids, episodes_by_script)
        for dim_cfg in cfg.scope_to_dims.get(scope, ()):
            if dim_cfg.dim not in dims_filter:
                continue
            task = StabilityTask(
                tag_set_ver=args.tag_set,
                dim=dim_cfg.dim,
                scope=scope,
                targets=targets,
            )
            run_results[dim_cfg.dim] = await run_full(task)

    reports = aggregate(run_results)

    output = args.output
    if not output:
        output = str(Path("ScriptLens/eval/reports") / f"{args.tag_set}_{args.split}.md")
    write_markdown(reports, output, tag_set_ver=args.tag_set, split=args.split)
    write_per_dim_json(reports, str(Path(output).with_suffix("")) + "_per_dim")
    print(f"stability report generated: {output}")


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
