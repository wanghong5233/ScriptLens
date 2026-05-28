"""Batch 3 report pipeline (v3 rubric, 6 dimensions, action-driven rewrite)."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.core.ingestion.script_loader import UnsupportedScriptFormatError
from service.score_registry import RubricConfig, load_rubric
from service.script_ingestion_service import ScriptIngestionService
from service.script_progress_tracker import tracker as progress_tracker
from service.script_tools.bundle_extractor import extract_bundle
from service.script_tools.character_entity_resolver import resolve_character_entities
from service.script_tools.compliance_scorer import screen_compliance
from service.script_tools.decision_aggregator import decide
from service.script_tools.dimension_aggregator import DimensionScore, aggregate
from service.script_tools.genre_weights import apply_genre_weights, infer_genre_scope
from service.script_tools.improvement_action_generator import ImprovementAction, generate_actions
from service.script_tools.llm_caller import LlmCaller
from service.script_tools.pacing_aggregator import aggregate_pacing_curve_v3
from service.script_tools.percentile_tier import resolve_tier
from service.script_tools.plot_unit_segmenter import segment_plot_units
from service.script_tools.relationship_candidate_generator import ensure_relationship_candidates
from service.script_tools.signal_catalog import SignalValue, build_signal_context, compute_signals
from utils.database import engine as default_engine

logger = logging.getLogger(__name__)

_RUBRIC_VERSION = "v3.0.0"
_TAG_SET_VERSION = "v1.0.0"
_DIM_CONFIDENCE_FLOAT = {"high": 0.85, "medium": 0.6, "low": 0.35}
_CONF_RANK = {"high": 2, "medium": 1, "low": 0}


def _is_skippable_ingest_error(exc: Exception) -> bool:
    if isinstance(exc, UnsupportedScriptFormatError):
        return True
    if isinstance(exc, ValueError):
        msg = str(exc)
        if "段落为空" in msg or "无场景" in msg:
            return True
    return False


def _is_guarded_ingest_file(file_path: Path) -> bool:
    return file_path.name.startswith("完整本_") and file_path.suffix.lower() == ".md"


def ingest_dataset(
    *,
    dataset_dir: Path,
    user_id: int,
    skip_unsupported: bool = True,
    limit: int | None = None,
    summary_output: Path | None = None,
) -> dict[str, Any]:
    if not dataset_dir.exists():
        raise FileNotFoundError(f"dataset_dir not found: {dataset_dir}")
    files = sorted(path for path in dataset_dir.iterdir() if path.is_file())
    if not files:
        raise FileNotFoundError(f"dataset_dir is empty: {dataset_dir}")

    ingest_service = ScriptIngestionService()
    ok_rows: list[dict[str, Any]] = []
    failed_rows: list[dict[str, str]] = []
    mapping: dict[str, str] = {}
    processed_total = 0

    for file_path in files:
        if limit is not None and limit > 0 and len(ok_rows) >= limit:
            break
        processed_total += 1
        if _is_guarded_ingest_file(file_path):
            failed_rows.append(
                {
                    "path": str(file_path),
                    "reason": "GuardSkip: 完整本_*.md is dataset report artifact",
                }
            )
            continue
        try:
            # Guard before DB write: skip degenerate scripts with too few scenes.
            _, seg = ingest_service._load_segment(file_path=file_path)  # noqa: SLF001
            if int(seg.total_scenes or 0) < 3:
                failed_rows.append(
                    {
                        "path": str(file_path),
                        "reason": f"GuardSkip: total_scenes<{3} ({int(seg.total_scenes or 0)})",
                    }
                )
                continue

            result = ingest_service.ingest(
                file_path=file_path,
                user_id=user_id,
                title=file_path.stem,
            )
            mapping[file_path.name] = result.script_id
            ok_rows.append(
                {
                    "path": str(file_path),
                    "script_id": result.script_id,
                    "title": result.title,
                    "total_episodes": result.total_episodes,
                    "total_scenes": result.total_scenes,
                }
            )
        except Exception as exc:  # pragma: no cover - runtime integration branch
            if skip_unsupported and _is_skippable_ingest_error(exc):
                failed_rows.append({"path": str(file_path), "reason": f"{type(exc).__name__}: {exc}"})
                continue
            failed_rows.append({"path": str(file_path), "reason": f"{type(exc).__name__}: {exc}"})

    payload = {
        "dataset_dir": str(dataset_dir),
        "total": processed_total,
        "ok": ok_rows,
        "failed": failed_rows,
        "mapping": mapping,
    }
    if summary_output is not None:
        summary_output.parent.mkdir(parents=True, exist_ok=True)
        summary_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


@dataclass
class _ScriptMeta:
    script_id: str
    title: str
    total_episodes: int
    total_scenes: int


def _merge_confidence(a: str, b: str) -> str:
    rank_a = _CONF_RANK.get(a, 0)
    rank_b = _CONF_RANK.get(b, 0)
    out_rank = min(rank_a, rank_b)
    for label, rank in _CONF_RANK.items():
        if rank == out_rank:
            return label
    return "low"


async def score_one_dimension(
    *,
    script_id: str,
    dimension: str,
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> dict[str, Any]:
    caller = caller or LlmCaller()
    rubric = load_rubric(_RUBRIC_VERSION)
    valid = {dim.id for dim in rubric.dimensions} | {"compliance"}
    if dimension not in valid:
        raise ValueError(f"unknown dimension={dimension!r}; valid={sorted(valid)}")
    meta = _load_script_meta(script_id, engine=engine)
    if meta is None:
        raise ValueError(f"script_id={script_id} 不存在")

    baseline = _load_baseline_dimension(script_id=script_id, dimension=dimension, engine=engine)
    if dimension == "compliance":
        compliance = await screen_compliance(script_id=script_id, caller=caller)
        return {
            "dimension": "compliance",
            "score": compliance.score,
            "level": compliance.tier,
            "reason": compliance.reason,
            "evidence_scene_ids": _scene_ids_from_evidence(compliance.evidence_ref_ids),
            "baseline": baseline,
        }

    await ensure_v1_tags_ready(script_id=script_id, caller=caller, engine=engine)
    ctx = build_signal_context(script_id=script_id, engine=engine)
    signals = await compute_signals(rubric, ctx, caller=caller)
    dim_scores = aggregate(rubric, signals)
    genre = infer_genre_scope(ctx.drama_tags)
    for item in dim_scores:
        tier_result = resolve_tier(
            rubric,
            dimension=item.dimension,
            score=item.score,
            genre_scope=genre,
            sample_size=ctx.plot_unit_count,
        )
        item.tier = tier_result.tier
        item.confidence = _merge_confidence(item.confidence, tier_result.confidence)
    target = next((item for item in dim_scores if item.dimension == dimension), None)
    if target is None:
        raise ValueError(f"dimension={dimension!r} not found in rubric")
    return {
        "dimension": target.dimension,
        "score": target.score,
        "level": target.tier,
        "reason": target.reason,
        "evidence_scene_ids": _scene_ids_from_signal_refs(target.signal_refs),
        "signal_refs": target.signal_refs,
        "baseline": baseline,
    }


async def generate_report(
    *,
    script_id: str,
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> dict[str, Any]:
    caller = caller or LlmCaller()
    progress_tracker.start(script_id)

    try:
        progress_tracker.update_stage(script_id, "loading_meta", "running", detail="读取剧本元数据")
        meta = _load_script_meta(script_id, engine=engine)
        if meta is None:
            raise ValueError(f"script_id={script_id} 不存在")
        progress_tracker.update_stage(script_id, "loading_meta", "done", detail=f"title={meta.title}")

        progress_tracker.update_stage(script_id, "extracting_rewards", "running", detail="检查并补齐 v1 标签依赖")
        await ensure_v1_tags_ready(script_id=script_id, caller=caller, engine=engine)
        progress_tracker.update_stage(script_id, "extracting_rewards", "done", detail="v1 标签依赖已就绪")

        progress_tracker.update_stage(script_id, "extracting_narrative", "running", detail="加载 rubric + 计算 signals")
        rubric = load_rubric(_RUBRIC_VERSION)
        ctx = build_signal_context(script_id=script_id, engine=engine)
        signals = await compute_signals(rubric, ctx, caller=caller)
        progress_tracker.update_stage(
            script_id,
            "extracting_narrative",
            "done",
            detail=f"signals={len(signals)}",
        )

        progress_tracker.update_stage(script_id, "scoring_dimensions", "running", detail="维度聚合与 tier 映射")
        dim_scores = aggregate(rubric, signals)
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
            item.confidence = _merge_confidence(item.confidence, tier_result.confidence)
        weighted = apply_genre_weights(rubric, dim_scores, genre_scope=genre_scope)
        progress_tracker.update_stage(
            script_id,
            "scoring_dimensions",
            "done",
            detail=f"dimensions={len(dim_scores)} genre={genre_scope}",
        )

        progress_tracker.update_stage(script_id, "aggregating_decision", "running", detail="合规评估 + 决策聚合")
        compliance = await screen_compliance(script_id=script_id, caller=caller)
        decision = decide(dim_scores, weighted, compliance=compliance.to_dict())
        progress_tracker.update_stage(
            script_id,
            "aggregating_decision",
            "done",
            detail=f"decision={decision.decision}",
        )

        progress_tracker.update_stage(script_id, "building_evidence", "running", detail="生成节奏曲线与改写动作")
        run_id = str(uuid.uuid4())
        actions = generate_actions(
            run_id=run_id,
            script_id=script_id,
            dim_scores=dim_scores,
            signal_values=signals,
        )
        pacing_curve = aggregate_pacing_curve_v3(ctx)
        progress_tracker.update_stage(
            script_id,
            "building_evidence",
            "done",
            detail=f"actions={len(actions)} pacing_points={len(pacing_curve)}",
        )

        report_payload = _build_report_payload(
            meta=meta,
            decision=decision,
            weighted_overall_score=weighted.overall_score,
            dim_scores=dim_scores,
            compliance_payload=compliance.to_dict(),
            pacing_curve=pacing_curve,
            actions=actions,
        )

        progress_tracker.update_stage(script_id, "persisting", "running", detail="写入 reports/scoring_runs/script_scores/actions")
        _persist_v3_report(
            script_id=script_id,
            run_id=run_id,
            rubric=rubric,
            genre_scope=genre_scope,
            ctx=ctx,
            signals=signals,
            dim_scores=dim_scores,
            actions=actions,
            decision_payload=decision.payload,
            report_payload=report_payload,
            weighted_overall_score=weighted.overall_score,
            engine=engine,
        )
        _mark_script_status(script_id=script_id, status="ready", failure_reason=None, engine=engine)
        progress_tracker.update_stage(script_id, "persisting", "done", detail="v3 report persisted")
        progress_tracker.finalize(script_id)
        return report_payload
    except Exception as exc:  # noqa: BLE001
        logger.exception("generate_report failed script_id=%s: %s", script_id, exc)
        _mark_script_status(script_id=script_id, status="failed", failure_reason=f"{type(exc).__name__}: {exc}", engine=engine)
        progress_tracker.finalize(script_id, error=f"{type(exc).__name__}: {exc}")
        raise


async def ensure_v1_tags_ready(
    *,
    script_id: str,
    caller: Optional[LlmCaller] = None,
    tag_set_ver: str = _TAG_SET_VERSION,
    seed: int = 42,
    variant: str = "a",
    engine: Engine = default_engine,
) -> None:
    caller = caller or LlmCaller()
    counts = _v1_dependency_counts(script_id=script_id, engine=engine)
    ready = (
        counts["plot_units"] > 0
        and counts["plot_unit_tags"] > 0
        and counts["script_drama_tags"] > 0
        and counts["character_entities"] > 0
    )
    if ready:
        return

    await segment_plot_units(
            script_id,
        tag_set_ver=tag_set_ver,
        seed=seed,
        variant=variant,
        caller=caller,
        persist=True,
        engine=engine,
    )
    await resolve_character_entities(
        script_id,
        tag_set_ver=tag_set_ver,
        seed=seed,
        caller=caller,
        persist=True,
            engine=engine,
        )
    ensure_relationship_candidates(
            script_id,
        tag_set_ver=tag_set_ver,
        min_cooccurrence=1,
        top_k=30,
        persist=True,
        engine=engine,
    )

    await extract_bundle(
        "v1_script_structure",
            script_id,
        tag_set_ver=tag_set_ver,
        seed=seed,
        variant=variant,
        caller=caller,
        persist=True,
        engine=engine,
    )

    for target in _episode_targets(script_id=script_id, engine=engine):
        await extract_bundle(
            "v1_episode_structure",
            target,
            tag_set_ver=tag_set_ver,
            seed=seed,
            variant=variant,
            caller=caller,
            persist=True,
            engine=engine,
        )
    for char_id in _character_ids(script_id=script_id, engine=engine):
        await extract_bundle(
            "v1_character_attrs",
            char_id,
            tag_set_ver=tag_set_ver,
            seed=seed,
            variant=variant,
            caller=caller,
            persist=True,
            engine=engine,
        )
    for rel_id in _relationship_ids(script_id=script_id, tag_set_ver=tag_set_ver, engine=engine):
        await extract_bundle(
            "v1_relationship",
            rel_id,
            tag_set_ver=tag_set_ver,
            seed=seed,
            variant=variant,
            caller=caller,
            persist=True,
            engine=engine,
        )


def _build_report_payload(
    *,
    meta: _ScriptMeta,
    decision: Any,
    weighted_overall_score: float | None,
    dim_scores: list[DimensionScore],
    compliance_payload: dict[str, Any],
    pacing_curve: list[dict[str, Any]],
    actions: list[ImprovementAction],
) -> dict[str, Any]:
    decision_label = _normalize_decision_label(str(decision.decision))
    scorecard: list[dict[str, Any]] = []
    for dim in dim_scores:
        scorecard.append(
            {
                "dimension": dim.dimension,
                "score": dim.score,
                "tier": dim.tier,
                "confidence": dim.confidence,
                "coverage_ratio": dim.coverage_ratio,
                "signal_refs": dim.signal_refs,
                "reason": dim.reason,
                "evidence_ref_ids": _scene_ids_from_signal_refs(dim.signal_refs),
            }
        )
    action_seeds = [
        {
            "id": action.id,
            "dimension": action.dimension,
            "signal_key": action.signal_key,
            "issue": action.issue,
            "target": action.target,
            "action_steps": action.action_steps,
            "evidence_refs": action.evidence_refs,
            "estimated_lift": action.estimated_lift,
        }
        for action in actions
    ]
    return {
            "script_id": meta.script_id,
            "title": meta.title,
        "decision": {
            "label": decision_label,
            "confidence": decision.confidence,
            "one_sentence_reason": decision.one_sentence_reason,
            "summary": decision.one_sentence_reason,
            "decision_inputs": {**decision.payload, "raw_decision": decision.decision},
        },
        "decision_reason": decision.one_sentence_reason,
        "overall_score": weighted_overall_score,
        "summary": decision.one_sentence_reason,
        "must_read_scene_ids": [],
        "scorecard": scorecard,
            "compliance": compliance_payload,
        "evidence_refs": [],
        "highlights": [],
        "coverage_card": None,
        "beat_sheet": None,
        "character_graph": None,
        "pacing_curve": pacing_curve,
        "evaluation": {
            "dimensions": [
                {
                    "key": item["dimension"],
                    "label": item["dimension"],
                    "score": item["score"],
                    "tier": item["tier"],
                    "confidence": item["confidence"],
                    "reason": item["reason"],
                    "signal_refs": item["signal_refs"],
                }
                for item in scorecard
            ],
            "risk_flags": [],
            "rewrite_seeds": action_seeds,
        },
        "risk_flags": [],
    }


def _persist_v3_report(
    *,
    script_id: str,
    run_id: str,
    rubric: RubricConfig,
    genre_scope: str,
    ctx: Any,
    signals: dict[str, SignalValue],
    dim_scores: list[DimensionScore],
    actions: list[ImprovementAction],
    decision_payload: dict[str, Any],
    report_payload: dict[str, Any],
    weighted_overall_score: float | None,
    engine: Engine,
) -> None:
    now = datetime.utcnow()
    report_id = str(uuid.uuid4())
    input_hash = hashlib.sha256(
        json.dumps(
            {
                "script_id": script_id,
                "rubric_version": rubric.rubric_id,
                "score_ver": rubric.score_ver,
                "tag_set_ver": ctx.tag_set_ver,
                "plot_unit_count": ctx.plot_unit_count,
                "episode_count": ctx.episode_count,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    model_versions = _collect_model_versions(signals)
    prompt_versions = {
        bundle.id: f"{rubric.score_ver}:{bundle.id}" for bundle in rubric.llm_bundles
    }
    quality_flags = {
        "insufficient_dimensions": [dim.dimension for dim in dim_scores if dim.tier == "insufficient"],
        "overall_score": weighted_overall_score,
        "signal_count": len(signals),
    }

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO scriptlens.scoring_runs (
                    id, script_id, rubric_version, tag_set_ver, input_hash, genre_scope,
                    episode_count, plot_unit_count, quality_flags, model_versions,
                    prompt_versions, status, error, created_at
                )
                VALUES (
                    :id, :script_id, :rubric_version, :tag_set_ver, :input_hash, :genre_scope,
                    :episode_count, :plot_unit_count, CAST(:quality_flags AS jsonb),
                    CAST(:model_versions AS jsonb), CAST(:prompt_versions AS jsonb),
                    :status, :error, :created_at
                )
                """
            ),
            {
                "id": run_id,
                "script_id": script_id,
                "rubric_version": rubric.rubric_id,
                "tag_set_ver": ctx.tag_set_ver,
                "input_hash": input_hash,
                "genre_scope": genre_scope,
                "episode_count": ctx.episode_count,
                "plot_unit_count": ctx.plot_unit_count,
                "quality_flags": json.dumps(quality_flags, ensure_ascii=False),
                "model_versions": json.dumps(model_versions, ensure_ascii=False),
                "prompt_versions": json.dumps(prompt_versions, ensure_ascii=False),
                "status": "done",
                "error": None,
                "created_at": now,
            },
        )

        score_rows = []
        for dim in dim_scores:
            signal_payload = {
                str(ref.get("signal_key") or ""): {
                    "value": ref.get("value"),
                    "score": ref.get("score"),
                    "source": ref.get("source"),
                    "confidence": ref.get("confidence"),
                    "evidence_refs": ref.get("evidence_refs"),
                    "primary_dimension": ref.get("primary_dimension"),
                    "weight_in_dim": ref.get("weight_in_dim"),
                }
                for ref in dim.signal_refs
                if str(ref.get("signal_key") or "").strip()
            }
            score_rows.append(
                {
                    "id": str(uuid.uuid4()),
                    "script_id": script_id,
                    "run_id": run_id,
                    "dimension": dim.dimension,
                    "primary_dimension": dim.primary_dimension or dim.dimension,
                    "score": float(dim.score if dim.score is not None else 0.0),
                    "percentile": None,
                    "tier": dim.tier,
                    "confidence": _DIM_CONFIDENCE_FLOAT.get(dim.confidence, 0.35),
                    "coverage_ratio": float(dim.coverage_ratio),
                    "signals": json.dumps(signal_payload, ensure_ascii=False),
                    "weights": json.dumps(
                        {
                            "base_weight": rubric.base_weight.get(dim.dimension, 0.0),
                        },
                        ensure_ascii=False,
                    ),
                    "tag_set_ver": ctx.tag_set_ver,
                    "score_ver": rubric.score_ver,
                    "model_ver": model_versions.get("primary_model", "v3-rule-only"),
                }
            )
        if score_rows:
            conn.execute(
                text(
                    """
                    INSERT INTO scriptlens.script_scores (
                        id, script_id, run_id, dimension, primary_dimension, score, percentile,
                        tier, confidence, coverage_ratio, signals, weights,
                        tag_set_ver, score_ver, model_ver, created_at
                    )
                    VALUES (
                        :id, :script_id, :run_id, :dimension, :primary_dimension, :score, :percentile,
                        :tier, :confidence, :coverage_ratio, CAST(:signals AS jsonb), CAST(:weights AS jsonb),
                        :tag_set_ver, :score_ver, :model_ver, NOW()
                    )
                    ON CONFLICT ON CONSTRAINT uq_script_scores_script_dim_ver
                    DO UPDATE SET
                        run_id = EXCLUDED.run_id,
                        primary_dimension = EXCLUDED.primary_dimension,
                        score = EXCLUDED.score,
                        percentile = EXCLUDED.percentile,
                        tier = EXCLUDED.tier,
                        confidence = EXCLUDED.confidence,
                        coverage_ratio = EXCLUDED.coverage_ratio,
                        signals = EXCLUDED.signals,
                        weights = EXCLUDED.weights,
                        model_ver = EXCLUDED.model_ver,
                        created_at = NOW()
                    """
                ),
                score_rows,
            )

        if actions:
            action_rows = [
                {
                    "id": action.id,
                    "run_id": action.run_id,
                    "script_id": action.script_id,
                    "dimension": action.dimension,
                    "signal_key": action.signal_key,
                    "template_id": action.template_id,
                    "issue": action.issue,
                    "target": action.target,
                    "action_steps": json.dumps(action.action_steps, ensure_ascii=False),
                    "evidence_refs": json.dumps(action.evidence_refs, ensure_ascii=False),
                    "estimated_lift": json.dumps(action.estimated_lift, ensure_ascii=False),
                }
                for action in actions
            ]
            conn.execute(
                text(
                    """
                    INSERT INTO scriptlens.scoring_improvement_actions (
                        id, run_id, script_id, dimension, signal_key, template_id,
                        issue, target, action_steps, evidence_refs, estimated_lift, created_at
                    )
                    VALUES (
                        :id, :run_id, :script_id, :dimension, :signal_key, :template_id,
                        :issue, :target, CAST(:action_steps AS jsonb), CAST(:evidence_refs AS jsonb),
                        CAST(:estimated_lift AS jsonb), NOW()
                    )
                    """
                ),
                action_rows,
            )

        conn.execute(
            text("DELETE FROM scriptlens.reports WHERE script_id = :sid"),
            {"sid": script_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO scriptlens.reports (
                    id, script_id, report_json, decision_payload, generated_at
                )
                VALUES (
                    :id, :script_id, CAST(:report_json AS jsonb), CAST(:decision_payload AS jsonb), :generated_at
                )
                """
            ),
            {
                "id": report_id,
                "script_id": script_id,
                "report_json": json.dumps(report_payload, ensure_ascii=False),
                "decision_payload": json.dumps(decision_payload, ensure_ascii=False),
                "generated_at": now,
            },
        )
    report_payload["report_id"] = report_id
    report_payload["generated_at"] = now.isoformat()


def _load_script_meta(script_id: str, *, engine: Engine) -> Optional[_ScriptMeta]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id::text AS script_id,
                       COALESCE(title, '') AS title,
                       COALESCE(total_episodes, 0) AS total_episodes,
                       COALESCE(total_scenes, 0) AS total_scenes
                FROM scriptlens.scripts
                WHERE id = :sid
                LIMIT 1
                """
            ),
            {"sid": script_id},
        ).mappings().first()
    if row is None:
        return None
    return _ScriptMeta(
        script_id=str(row["script_id"]),
        title=str(row["title"]),
        total_episodes=int(row["total_episodes"] or 0),
        total_scenes=int(row["total_scenes"] or 0),
    )


def _v1_dependency_counts(*, script_id: str, engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                    (SELECT COUNT(*) FROM scriptlens.plot_units WHERE script_id = :sid) AS plot_units,
                    (
                        SELECT COUNT(*)
                        FROM scriptlens.plot_unit_tags put
                        JOIN scriptlens.plot_units pu ON pu.id = put.plot_unit_id
                        WHERE pu.script_id = :sid
                    ) AS plot_unit_tags,
                    (SELECT COUNT(*) FROM scriptlens.script_tags WHERE script_id = :sid AND dim = 'drama_tags') AS script_drama_tags,
                    (SELECT COUNT(*) FROM scriptlens.character_entities WHERE script_id = :sid) AS character_entities,
                    (SELECT COUNT(*) FROM scriptlens.character_relationships WHERE script_id = :sid) AS relationships
                """
            ),
            {"sid": script_id},
        ).mappings().first()
    row = row or {}
    return {
        "plot_units": int(row.get("plot_units") or 0),
        "plot_unit_tags": int(row.get("plot_unit_tags") or 0),
        "script_drama_tags": int(row.get("script_drama_tags") or 0),
        "character_entities": int(row.get("character_entities") or 0),
        "relationships": int(row.get("relationships") or 0),
    }


def _episode_targets(*, script_id: str, engine: Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT episode_no
                FROM scriptlens.scenes
                WHERE script_id = :sid AND episode_no IS NOT NULL
                ORDER BY episode_no
                LIMIT 30
                """
            ),
            {"sid": script_id},
        ).mappings().all()
    return [f"{script_id}::ep::{int(row['episode_no'])}" for row in rows]


def _character_ids(*, script_id: str, engine: Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id
                FROM scriptlens.character_entities
                WHERE script_id = :sid
                ORDER BY created_at, canonical_name
                LIMIT 120
                """
            ),
            {"sid": script_id},
        ).mappings().all()
    return [str(row["id"]) for row in rows]


def _relationship_ids(*, script_id: str, tag_set_ver: str, engine: Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id
                FROM scriptlens.character_relationships
                WHERE script_id = :sid
                  AND (tag_set_ver = :ver OR tag_set_ver = '' OR tag_set_ver IS NULL)
                ORDER BY created_at, id
                LIMIT 160
                """
            ),
            {"sid": script_id, "ver": tag_set_ver},
        ).mappings().all()
    return [str(row["id"]) for row in rows]


def _collect_model_versions(signals: dict[str, SignalValue]) -> dict[str, Any]:
    models: list[str] = []
    for signal in signals.values():
        meta = signal.meta if isinstance(signal.meta, dict) else {}
        model = meta.get("model")
        if isinstance(model, str) and model.strip():
            models.append(model.strip())
    models = sorted(set(models))
    return {
        "primary_model": models[0] if models else "v3-rule-only",
        "models": models,
    }


def _scene_ids_from_signal_refs(signal_refs: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for ref in signal_refs:
        evidence = ref.get("evidence_refs") if isinstance(ref, dict) else []
        if not isinstance(evidence, list):
            continue
        for item in evidence:
            if not isinstance(item, dict):
                continue
            scene_id = str(item.get("scene_id") or "").strip()
            if scene_id and scene_id not in seen:
                seen.add(scene_id)
                out.append(scene_id)
    return out


def _scene_ids_from_evidence(evidence_ref_ids: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in evidence_ref_ids:
        scene_id = str(item or "").strip()
        if scene_id and scene_id not in seen:
            seen.add(scene_id)
            out.append(scene_id)
    return out


def _load_baseline_dimension(*, script_id: str, dimension: str, engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT report_json
                FROM scriptlens.reports
                WHERE script_id = :sid
                ORDER BY generated_at DESC
                LIMIT 1
                """
            ),
            {"sid": script_id},
        ).mappings().first()
    if row is None:
        return {"score": None, "level": None, "reason": None, "evidence_scene_ids": []}
    payload = row.get("report_json")
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {}
    if not isinstance(payload, dict):
        return {"score": None, "level": None, "reason": None, "evidence_scene_ids": []}
    if dimension == "compliance":
        compliance = payload.get("compliance") if isinstance(payload.get("compliance"), dict) else {}
        return {
            "score": compliance.get("score"),
            "level": compliance.get("tier") or compliance.get("level"),
            "reason": compliance.get("reason"),
            "evidence_scene_ids": list(compliance.get("evidence_ref_ids") or []),
        }
    scorecard = payload.get("scorecard")
    if not isinstance(scorecard, list):
        return {"score": None, "level": None, "reason": None, "evidence_scene_ids": []}
    for item in scorecard:
        if not isinstance(item, dict):
            continue
        if str(item.get("dimension") or "").strip() != dimension:
            continue
        return {
            "score": item.get("score"),
            "level": item.get("tier") or item.get("level"),
            "reason": item.get("reason"),
            "evidence_scene_ids": list(item.get("evidence_ref_ids") or []),
        }
    return {"score": None, "level": None, "reason": None, "evidence_scene_ids": []}


def _normalize_decision_label(raw: str) -> str:
    if raw == "recommended":
        return "recommend_continue"
    if raw in {"conditional_recommend", "insufficient_data"}:
        return "cautious_continue"
    return "not_recommended"


def _mark_script_status(
    *,
    script_id: str,
    status: str,
    failure_reason: str | None,
    engine: Engine,
) -> None:
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE scriptlens.scripts
                    SET status = :status,
                        failure_reason = :failure_reason
                    WHERE id = :sid
                    """
                ),
                {"status": status, "failure_reason": failure_reason, "sid": script_id},
            )
    except Exception:  # noqa: BLE001
        logger.exception("failed to mark script status script_id=%s status=%s", script_id, status)
