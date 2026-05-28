from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.score_registry import load_rubric
from service.script_report_service import _build_report_payload, ensure_v1_tags_ready, generate_report
from service.script_tools.compliance_scorer import screen_compliance
from service.script_tools.decision_aggregator import decide
from service.script_tools.dimension_aggregator import aggregate
from service.script_tools.extractor_common import resolve_script_id
from service.script_tools.genre_weights import apply_genre_weights, infer_genre_scope
from service.script_tools.improvement_action_generator import generate_actions
from service.script_tools.llm_caller import LlmCaller
from service.script_tools.pacing_aggregator import aggregate_pacing_curve_v3
from service.script_tools.percentile_tier import resolve_tier
from service.script_tools.signal_catalog import build_signal_context, compute_signals
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


def _scoring_tables_ready(engine: Engine) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                    to_regclass('scriptlens.scoring_runs') IS NOT NULL AS has_scoring_runs,
                    to_regclass('scriptlens.script_scores') IS NOT NULL AS has_script_scores,
                    to_regclass('scriptlens.scoring_improvement_actions') IS NOT NULL AS has_actions
                """
            )
        ).mappings().first()
    row = row or {}
    return bool(row.get("has_scoring_runs")) and bool(row.get("has_script_scores")) and bool(row.get("has_actions"))


def _latest_run(script_id: str, *, rubric_version: str, engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        run = conn.execute(
            text(
                """
                SELECT id::text AS id, rubric_version, tag_set_ver, genre_scope, status, created_at
                FROM scriptlens.scoring_runs
                WHERE script_id = :sid AND rubric_version = :rv
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"sid": script_id, "rv": rubric_version},
        ).mappings().first()
        if not run:
            return {}
        run_id = str(run["id"])
        score_count_row = conn.execute(
            text("SELECT COUNT(*) AS n FROM scriptlens.script_scores WHERE run_id = :rid"),
            {"rid": run_id},
        ).mappings().first()
        action_count_row = conn.execute(
            text("SELECT COUNT(*) AS n FROM scriptlens.scoring_improvement_actions WHERE run_id = :rid"),
            {"rid": run_id},
        ).mappings().first()
    payload = dict(run)
    payload["script_score_count"] = int((score_count_row or {}).get("n") or 0)
    payload["action_count"] = int((action_count_row or {}).get("n") or 0)
    return payload


async def _generate_report_dry_run(
    *,
    script_id: str,
    rubric_version: str,
    engine: Engine,
) -> dict[str, Any]:
    caller = LlmCaller()
    await ensure_v1_tags_ready(script_id=script_id, caller=caller, engine=engine)

    rubric = load_rubric(rubric_version)
    ctx = build_signal_context(script_id=script_id, engine=engine)
    signal_values = await compute_signals(rubric, ctx, caller=caller, seed=42)
    dim_scores = aggregate(rubric, signal_values)
    genre_scope = infer_genre_scope(ctx.drama_tags)
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
    compliance = await screen_compliance(script_id=script_id, caller=caller)
    decision = decide(dim_scores, weighted, compliance=compliance.to_dict())
    actions = generate_actions(
        run_id="dry-run",
        script_id=script_id,
        dim_scores=dim_scores,
        signal_values=signal_values,
    )
    pacing_curve = aggregate_pacing_curve_v3(ctx)
    report = _build_report_payload(
        meta=SimpleNamespace(
            script_id=script_id,
            title=str(ctx.script_meta.get("title") or ""),
            total_episodes=ctx.episode_count,
            total_scenes=int(ctx.script_meta.get("total_scenes") or 0),
        ),
        decision=decision,
        weighted_overall_score=weighted.overall_score,
        dim_scores=dim_scores,
        compliance_payload=compliance.to_dict(),
        pacing_curve=pacing_curve,
        actions=actions,
    )
    report["dry_run"] = True
    return report


def _build_markdown(payload: dict[str, Any]) -> str:
    decision = payload.get("decision") or {}
    scorecard = payload.get("scorecard") or []
    lines = [
        "# V3 Scoring Smoke",
        "",
        f"- script_id: `{payload.get('script_id')}`",
        f"- title: `{payload.get('title')}`",
        f"- overall_score: `{payload.get('overall_score')}`",
        f"- decision: `{decision.get('label')}` / confidence `{decision.get('confidence')}`",
        f"- rewrite_actions: `{len((payload.get('evaluation') or {}).get('rewrite_seeds') or [])}`",
        "",
        "## Dimension Scores",
        "",
    ]
    for item in scorecard:
        lines.append(
            f"- `{item.get('dimension')}`: score=`{item.get('score')}` tier=`{item.get('tier')}` "
            f"confidence=`{item.get('confidence')}` coverage=`{item.get('coverage_ratio')}`"
        )

    run = payload.get("latest_run") or {}
    if run:
        lines.extend(
            [
                "",
                "## Persistence Check",
                "",
                f"- run_id: `{run.get('id')}`",
                f"- rubric_version: `{run.get('rubric_version')}`",
                f"- tag_set_ver: `{run.get('tag_set_ver')}`",
                f"- script_scores: `{run.get('script_score_count')}`",
                f"- improvement_actions: `{run.get('action_count')}`",
                f"- status: `{run.get('status')}`",
            ]
        )
    if payload.get("dry_run"):
        lines.extend(
            [
                "",
                "## Persistence Check",
                "",
                "- skipped: `scoring tables missing, executed dry-run pipeline`",
            ]
        )
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch3 generate_report smoke run.")
    parser.add_argument("--script-id", default="", help="script id or exact title")
    parser.add_argument("--rubric-version", default="v3.0.0")
    parser.add_argument(
        "--output",
        default="eval/deliverables/reports/v3_scoring_smoke_dev.md",
        help="markdown output path",
    )
    parser.add_argument(
        "--json-output",
        default="eval/deliverables/reports/v3_scoring_smoke_dev.json",
        help="json output path",
    )
    return parser.parse_args()


async def _main_async() -> None:
    args = _parse_args()
    script_ref = args.script_id.strip() or (_latest_script_id(default_engine) or "")
    if not script_ref:
        raise RuntimeError("no scripts found for scoring smoke")
    script_id = resolve_script_id(script_ref, engine=default_engine) or script_ref

    if _scoring_tables_ready(default_engine):
        report = await generate_report(script_id=script_id, engine=default_engine)
        run = _latest_run(script_id=script_id, rubric_version=args.rubric_version, engine=default_engine)
    else:
        report = await _generate_report_dry_run(
            script_id=script_id,
            rubric_version=args.rubric_version,
            engine=default_engine,
        )
        run = {}
    payload = {
        **report,
        "latest_run": run,
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_build_markdown(payload), encoding="utf-8")

    json_output = Path(args.json_output)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"output": str(output), "json_output": str(json_output), "script_id": script_id}, ensure_ascii=False))


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
