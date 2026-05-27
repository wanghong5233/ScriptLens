from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.score_registry import load_rubric
from service.script_report_service import ensure_v1_tags_ready
from service.script_tools.dimension_aggregator import aggregate
from service.script_tools.genre_weights import apply_genre_weights, infer_genre_scope
from service.script_tools.llm_caller import LlmCaller
from service.script_tools.percentile_tier import resolve_tier
from service.script_tools.signal_catalog import build_signal_context, compute_signals
from service.script_tools.v0_extractor_common import resolve_script_id
from utils.database import engine as default_engine


def _latest_script_id(engine: Engine) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id::text AS id
                FROM scriptlens.scripts
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().first()
    return str(row["id"]) if row else None


def _parse_seeds(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    if not out:
        raise ValueError("seeds must not be empty")
    return out


def _variance(values: list[float | None]) -> float:
    usable = [float(v) for v in values if v is not None]
    if len(usable) <= 1:
        return 0.0
    return float(statistics.pvariance(usable))


async def run_stability_eval(
    *,
    script_id: str,
    seeds: list[int],
    rubric_version: str = "v3.0.0",
    overall_variance_threshold: float = 0.05,
    dim_variance_threshold: float = 0.1,
    caller: LlmCaller | None = None,
    engine: Engine = default_engine,
) -> dict[str, Any]:
    caller = caller or LlmCaller()
    await ensure_v1_tags_ready(script_id=script_id, caller=caller, engine=engine)

    rubric = load_rubric(rubric_version)
    ctx = build_signal_context(script_id=script_id, engine=engine)
    genre_scope = infer_genre_scope(ctx.drama_tags)

    run_rows: list[dict[str, Any]] = []
    for seed in seeds:
        signal_values = await compute_signals(rubric, ctx, caller=caller, seed=seed)
        dim_scores = aggregate(rubric, signal_values)
        for item in dim_scores:
            tier_result = resolve_tier(
                rubric,
                dimension=item.dimension,
                score=item.score,
                genre_scope=genre_scope,
                sample_size=ctx.plot_unit_count,
            )
            item.tier = tier_result.tier
            item.confidence = tier_result.confidence

        weighted = apply_genre_weights(rubric, dim_scores, genre_scope=genre_scope)
        run_rows.append(
            {
                "seed": seed,
                "overall_score": weighted.overall_score,
                "dimension_scores": {item.dimension: item.score for item in dim_scores},
                "dimension_tiers": {item.dimension: item.tier for item in dim_scores},
            }
        )

    dim_variance: dict[str, float] = {}
    for dim in rubric.all_dimensions:
        dim_variance[dim] = _variance([row["dimension_scores"].get(dim) for row in run_rows])
    overall_variance = _variance([row.get("overall_score") for row in run_rows])

    checks = {
        "overall_variance_le_threshold": overall_variance <= overall_variance_threshold,
        "dimension_variance_le_threshold": all(v <= dim_variance_threshold for v in dim_variance.values()),
    }

    return {
        "script_id": script_id,
        "rubric_version": rubric.rubric_id,
        "genre_scope": genre_scope,
        "seeds": seeds,
        "runs": run_rows,
        "overall_variance": overall_variance,
        "dimension_variance": dim_variance,
        "thresholds": {
            "overall": overall_variance_threshold,
            "dimension": dim_variance_threshold,
        },
        "checks": checks,
        "all_pass": all(checks.values()),
    }


def write_markdown(payload: dict[str, Any], *, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# V3 Scoring Stability",
        "",
        f"- script_id: `{payload['script_id']}`",
        f"- rubric_version: `{payload['rubric_version']}`",
        f"- genre_scope: `{payload['genre_scope']}`",
        f"- seeds: `{payload['seeds']}`",
        f"- overall_variance: `{payload['overall_variance']:.6f}` (threshold <= `{payload['thresholds']['overall']:.6f}`)",
        "",
        "## Dimension Variance",
        "",
    ]
    for dim, value in sorted((payload.get("dimension_variance") or {}).items()):
        lines.append(
            f"- `{dim}`: `{value:.6f}` (threshold <= `{payload['thresholds']['dimension']:.6f}`)"
        )
    lines.extend(
        [
            "",
            "## Checks",
            "",
        ]
    )
    for name, passed in (payload.get("checks") or {}).items():
        lines.append(f"- {name}: `{'PASS' if passed else 'FAIL'}`")
    lines.extend(
        [
            "",
            "## Overall",
            "",
            f"- result: `{'PASS' if payload.get('all_pass') else 'FAIL'}`",
        ]
    )
    output.write_text("\n".join(lines), encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run 5-seed stability eval for Batch3 scoring.")
    parser.add_argument("--script-id", default="", help="script id or exact title")
    parser.add_argument("--rubric-version", default="v3.0.0")
    parser.add_argument("--seeds", default="42,123,456,789,1011")
    parser.add_argument("--overall-variance-threshold", type=float, default=0.05)
    parser.add_argument("--dim-variance-threshold", type=float, default=0.1)
    parser.add_argument(
        "--output",
        default="eval/deliverables/reports/v3_scoring_stability_dev.md",
        help="markdown output path",
    )
    parser.add_argument(
        "--json-output",
        default="eval/deliverables/reports/v3_scoring_stability_dev.json",
        help="json output path",
    )
    return parser.parse_args()


async def _main_async() -> None:
    args = _parse_args()
    script_ref = args.script_id.strip() or (_latest_script_id(default_engine) or "")
    if not script_ref:
        raise RuntimeError("no scripts found for scoring stability")
    script_id = resolve_script_id(script_ref, engine=default_engine) or script_ref

    payload = await run_stability_eval(
        script_id=script_id,
        seeds=_parse_seeds(args.seeds),
        rubric_version=args.rubric_version,
        overall_variance_threshold=args.overall_variance_threshold,
        dim_variance_threshold=args.dim_variance_threshold,
        caller=LlmCaller(),
        engine=default_engine,
    )

    output = Path(args.output)
    write_markdown(payload, output=output)

    json_output = Path(args.json_output)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"output": str(output), "json_output": str(json_output), **payload}, ensure_ascii=False, indent=2))


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
