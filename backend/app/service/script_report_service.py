"""Script scoring report pipeline (6 dimensions, action-driven rewrite).

报告内容生成分两层：
1. 评分层（6 维 rubric/signal/aggregator） —— scorecard + decision + tier_cuts + top_signals
2. 叙事层（chain 抽取）              —— coverage_card / beat_sheet / character_graph / highlights
   这层独立于评分，纯为前端 5 个 tab 的内容展示提供结构化数据。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.core.ingestion.script_loader import UnsupportedScriptFormatError
from service.score_registry import RubricConfig, load_rubric
from service.script_ingestion_service import ScriptIngestionService
from service.script_progress_tracker import tracker as progress_tracker
from service.script_tools.beat_chain import BeatSheet, extract_beat_sheet
from service.script_tools.character_graph_chain import CharacterGraph, extract_character_graph
from service.script_tools.compliance_scorer import screen_compliance
from service.script_tools.coverage_chain import CoverageCard, extract_coverage_card
from service.script_tools.decision_aggregator import decide
from service.script_tools.dimension_aggregator import DimensionScore, aggregate
from service.script_tools.genre_weights import apply_genre_weights, infer_genre_scope
from service.script_tools.improvement_action_generator import ImprovementAction, generate_actions
from service.script_tools.llm_caller import LlmCaller, ScoreLLMError
from service.script_tools.pacing_aggregator import aggregate_pacing_curve_v3 as aggregate_pacing_curve
from service.script_tools.percentile_tier import resolve_tier
from service.script_tools.reward_extractor import RewardEvent, extract_reward_events
from service.script_tools.signal_catalog import SignalValue, build_signal_context, compute_signals
from service.script_tools.tag_pipeline import run_tag_pipeline
from utils.database import engine as default_engine

logger = logging.getLogger(__name__)

_RUBRIC_ID = "v3.0.0"
_TAG_SET_VERSION = "script"
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


def _safe_tier_cuts(cuts: dict[str, Any] | None) -> dict[str, float]:
    payload = cuts if isinstance(cuts, dict) else {}
    try:
        p25 = float(payload.get("p25", 4.0))
    except (TypeError, ValueError):
        p25 = 4.0
    try:
        p50 = float(payload.get("p50", 6.0))
    except (TypeError, ValueError):
        p50 = 6.0
    try:
        p75 = float(payload.get("p75", 8.0))
    except (TypeError, ValueError):
        p75 = 8.0
    return {"p25": p25, "p50": p50, "p75": p75}


def _compute_overall_cuts(scorecard: list[dict[str, Any]]) -> dict[str, float]:
    if not scorecard:
        return {"p25": 4.0, "p50": 6.0, "p75": 8.0}
    p25_values: list[float] = []
    p50_values: list[float] = []
    p75_values: list[float] = []
    for item in scorecard:
        cuts = _safe_tier_cuts(item.get("tier_cuts"))
        p25_values.append(cuts["p25"])
        p50_values.append(cuts["p50"])
        p75_values.append(cuts["p75"])
    return {
        "p25": round(sum(p25_values) / len(p25_values), 4),
        "p50": round(sum(p50_values) / len(p50_values), 4),
        "p75": round(sum(p75_values) / len(p75_values), 4),
    }


async def _optional_chain(name: str, coro: Awaitable[Any]) -> Any:
    """叙事层 chain 可降级为 None；只吞已知业务失败，避免一个 LLM JSON 解析失败把整份报告拖崩。"""
    try:
        return await coro
    except (ScoreLLMError, ValueError) as exc:
        logger.exception("%s failed and will be stored as null: %s", name, exc)
        return None


def _select_beat_anchor_scenes(beat_sheet: Optional[BeatSheet], *, top_k: int = 3) -> list[str]:
    """从 beat_sheet 选 top_k 个最值得用户先看的场（用户决策视角：爽 > 反转 > 高潮 > 钩子）。"""
    if beat_sheet is None:
        return []
    priority = {
        "reward": 0,
        "twist": 1,
        "climax": 2,
        "opening": 3,
        "inciting": 4,
        "midpoint": 5,
        "closing": 6,
    }
    beats = [
        beat
        for act in beat_sheet.acts
        for beat in act.beats
        if beat.anchor_scene_id
    ]
    beats.sort(key=lambda b: priority.get(b.type, 99))
    out: list[str] = []
    seen: set[str] = set()
    for beat in beats:
        if beat.anchor_scene_id in seen:
            continue
        seen.add(beat.anchor_scene_id)
        out.append(beat.anchor_scene_id)
        if len(out) >= top_k:
            break
    return out


def _derive_risk_flags(compliance_payload: dict[str, Any]) -> list[str]:
    """从 compliance.hits 派生兼容字段 risk_flags（前端旧版渲染用）。"""
    flags: list[str] = []
    seen: set[str] = set()
    for hit in compliance_payload.get("hits") or []:
        if not isinstance(hit, dict):
            continue
        category = str(hit.get("category") or "").strip()
        if not category or category in seen:
            continue
        seen.add(category)
        flags.append(category)
    return flags


_REWARD_TO_HIGHLIGHT_TYPE = {
    "face_slap": "face_slap",
    "reversal": "reversal",
    "revenge": "revenge",
    "cp_progress": "cp_progress",
    "identity_reveal": "identity_reveal",
    "villain_fall": "villain_fall",
    "underdog_rise": "underdog_rise",
    "scheme_exposed": "scheme_exposed",
}
_REWARD_TYPE_HEADLINE = {
    "face_slap": "打脸",
    "reversal": "反转",
    "revenge": "复仇",
    "cp_progress": "CP 进展",
    "identity_reveal": "身份揭露",
    "villain_fall": "反派落败",
    "underdog_rise": "逆袭",
    "scheme_exposed": "阴谋败露",
}


def _trim_oneliner(s: str, max_len: int = 40) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def _build_evidence_refs_minimal(
    reward_events: list[RewardEvent],
    compliance_hits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从 reward_events + compliance.hits 派生 evidence_refs[]。

    所有前端需要跳转的高亮锚点（看点、合规风险）都注册到 evidence_refs[]，
    highlights / 卡片通过 id 引用同一条 evidence_ref。去重键是
    (scene_id, start_line, end_line)，保证 id 与锚点一一对应。
    """
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, Optional[int], Optional[int]]] = set()

    for ev in reward_events:
        line_range = getattr(ev, "evidence_line_range", None)
        start_line = line_range[0] if line_range else None
        end_line = line_range[1] if line_range else None
        key = (ev.scene_id, start_line, end_line)
        if key in seen:
            continue
        seen.add(key)
        confidence = (
            "high"
            if ev.event_type in {"reversal", "face_slap", "identity_reveal"}
            else "medium"
        )
        refs.append(
            {
                "id": f"evi_reward_{ev.scene_id}_{ev.event_type}",
                "scene_id": ev.scene_id,
                "episode_no": ev.episode_no,
                "scene_no": ev.scene_no,
                "scene_label": None,
                "start_line": start_line,
                "end_line": end_line,
                "quote": ev.evidence,
                "quote_source": f"reward:{ev.event_type}",
                "scene_summary": None,
                "reason": f"看点：{_REWARD_TYPE_HEADLINE.get(ev.event_type, '看点')}",
                "confidence": confidence,
            }
        )

    for idx, hit in enumerate(compliance_hits or []):
        scene_id = str(hit.get("scene_id") or "").strip()
        if not scene_id:
            continue
        raw_range = hit.get("evidence_line_range") or []
        start_line = raw_range[0] if isinstance(raw_range, (list, tuple)) and len(raw_range) >= 1 else None
        end_line = raw_range[1] if isinstance(raw_range, (list, tuple)) and len(raw_range) >= 2 else None
        key = (scene_id, start_line, end_line)
        if key in seen:
            continue
        seen.add(key)
        level = str(hit.get("level") or "low_risk")
        confidence = {"high_risk": "high", "medium_risk": "medium"}.get(level, "low")
        category = str(hit.get("category") or "compliance")
        refs.append(
            {
                "id": f"evi_risk_{scene_id}_{idx}",
                "scene_id": scene_id,
                "episode_no": hit.get("episode_no"),
                "scene_no": hit.get("scene_no"),
                "scene_label": None,
                "start_line": start_line,
                "end_line": end_line,
                "quote": str(hit.get("excerpt") or ""),
                "quote_source": "risk_hit",
                "scene_summary": None,
                "reason": f"合规风险：{category}",
                "confidence": confidence,
            }
        )

    return refs


def _build_highlights_minimal(
    reward_events: list[RewardEvent],
    beat_sheet: Optional[BeatSheet],
    evidence_refs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从 reward_events + 开场节拍派生 highlights[]。

    highlight.id 复用对应 evidence_ref.id（同空间），前端点击高亮即可定位锚点。
    """
    reward_evi_index: dict[tuple[str, str], str] = {}
    for er in evidence_refs:
        qs = er.get("quote_source") or ""
        if qs.startswith("reward:"):
            ev_type = qs.split(":", 1)[1]
            reward_evi_index[(er["scene_id"], ev_type)] = er["id"]

    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for ev in reward_events:
        if ev.scene_id in used:
            continue
        hl_type = _REWARD_TO_HIGHLIGHT_TYPE.get(ev.event_type)
        if hl_type is None:
            continue
        evi_id = reward_evi_index.get((ev.scene_id, ev.event_type))
        if evi_id is None:
            continue
        headline = _REWARD_TYPE_HEADLINE.get(ev.event_type, "看点")
        line_range = getattr(ev, "evidence_line_range", None)
        out.append(
            {
                "id": evi_id,
                "type": hl_type,
                "scene_id": ev.scene_id,
                "episode_no": ev.episode_no,
                "scene_no": ev.scene_no,
                "scene_label": None,
                "start_line": line_range[0] if line_range else None,
                "end_line": line_range[1] if line_range else None,
                "oneliner": _trim_oneliner(f"{headline} · {ev.evidence}"),
                "evidence": ev.evidence,
            }
        )
        used.add(ev.scene_id)
    if beat_sheet is not None:
        for act in beat_sheet.acts:
            for beat in act.beats:
                if beat.type == "opening" and beat.anchor_scene_id and beat.anchor_scene_id not in used:
                    out.append(
                        {
                            "id": f"evi_hook_{beat.anchor_scene_id}",
                            "type": "hook",
                            "scene_id": beat.anchor_scene_id,
                            "episode_no": None,
                            "scene_no": None,
                            "scene_label": None,
                            "start_line": None,
                            "end_line": None,
                            "oneliner": _trim_oneliner(f"开场抓人 · {beat.summary}"),
                            "evidence": beat.summary,
                        }
                    )
                    used.add(beat.anchor_scene_id)
                    break
    return out


def _load_drama_tags(*, script_id: str, engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT value,
                       confidence
                FROM scriptlens.script_tags
                WHERE script_id = :sid
                  AND dim = 'drama_tags'
                ORDER BY confidence DESC NULLS LAST, value
                """
            ),
            {"sid": script_id},
        ).mappings().all()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        value = str(row.get("value") or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        conf_raw = row.get("confidence")
        try:
            confidence = float(conf_raw) if conf_raw is not None else 0.0
        except (TypeError, ValueError):
            confidence = 0.0
        out.append({"key": "drama_tags", "value": value, "confidence": round(confidence, 4)})
    return out


def _load_plot_units(*, script_id: str, engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT pu.id::text AS plot_unit_id,
                       pu.episode_no,
                       pu.idx,
                       pu.summary,
                       pu.start_scene_id::text AS start_scene_id,
                       pu.end_scene_id::text AS end_scene_id,
                       put.dim,
                       put.value
                FROM scriptlens.plot_units pu
                LEFT JOIN scriptlens.plot_unit_tags put
                       ON put.plot_unit_id = pu.id
                WHERE pu.script_id = :sid
                ORDER BY pu.idx
                """
            ),
            {"sid": script_id},
        ).mappings().all()
    by_unit: dict[str, dict[str, Any]] = {}
    for row in rows:
        plot_unit_id = str(row.get("plot_unit_id") or "").strip()
        if not plot_unit_id:
            continue
        payload = by_unit.setdefault(
            plot_unit_id,
            {
                "plot_unit_id": plot_unit_id,
                "episode_no": int(row.get("episode_no")) if row.get("episode_no") is not None else None,
                "plot_unit_no": int(row.get("idx") or 0),
                "summary": str(row.get("summary") or ""),
                "start_scene_id": str(row.get("start_scene_id") or "") or None,
                "end_scene_id": str(row.get("end_scene_id") or "") or None,
                "scene_refs": [],
                "narrative_intensity": 0,
                "plot_hook": "none",
                "conflict_type": "none",
                "payoff_type": "none",
                "emotional_driver": "none",
                "story_stage": "none",
            },
        )
        dim = str(row.get("dim") or "").strip()
        value = str(row.get("value") or "").strip()
        if not dim or not value:
            continue
        if dim in {"plot_hook", "conflict_type", "payoff_type", "emotional_driver", "story_stage"}:
            payload[dim] = value

    for payload in by_unit.values():
        start_scene_id = payload.pop("start_scene_id", None)
        end_scene_id = payload.pop("end_scene_id", None)
        scene_refs: list[str] = []
        if start_scene_id:
            scene_refs.append(str(start_scene_id))
        if end_scene_id and end_scene_id != start_scene_id:
            scene_refs.append(str(end_scene_id))
        payload["scene_refs"] = scene_refs
        payload["narrative_intensity"] = min(
            8,
            (2 if payload["plot_hook"] != "none" else 0)
            + (2 if payload["conflict_type"] != "none" else 0)
            + (3 if payload["payoff_type"] != "none" else 0)
            + (1 if payload["emotional_driver"] != "none" else 0),
        )
    return list(by_unit.values())


def _load_characters(*, script_id: str, engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT c.id::text AS id,
                       c.canonical_name,
                       c.aliases,
                       c.archetype,
                       c.arc_type,
                       c.agency_level,
                       c.evidence
                FROM scriptlens.character_entities c
                WHERE c.script_id = :sid
                ORDER BY c.created_at, c.canonical_name
                """
            ),
            {"sid": script_id},
        ).mappings().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        aliases_raw = row.get("aliases")
        aliases = aliases_raw if isinstance(aliases_raw, list) else []
        if isinstance(aliases_raw, str):
            try:
                parsed_aliases = json.loads(aliases_raw)
            except (TypeError, ValueError):
                parsed_aliases = None
            if isinstance(parsed_aliases, list):
                aliases = parsed_aliases
        evidence_raw = row.get("evidence")
        evidence = evidence_raw if isinstance(evidence_raw, dict) else {}
        if isinstance(evidence_raw, str):
            try:
                parsed_evidence = json.loads(evidence_raw)
            except (TypeError, ValueError):
                parsed_evidence = None
            if isinstance(parsed_evidence, dict):
                evidence = parsed_evidence
        out.append(
            {
                "id": str(row.get("id") or ""),
                "name": str(row.get("canonical_name") or ""),
                "aliases": [str(item) for item in aliases if str(item).strip()],
                "archetype": str(row.get("archetype") or ""),
                "role_in_arc": str(evidence.get("character_role_in_arc") or ""),
                "arc_type": str(row.get("arc_type") or ""),
                "agency_level": str(row.get("agency_level") or ""),
                "appearance_count": int(evidence.get("scene_count") or 0),
            }
        )
    return out


def _load_character_relationships(*, script_id: str, engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id,
                       src_char_id::text AS a_id,
                       dst_char_id::text AS b_id,
                       relationship_type,
                       polarity,
                       dynamic_arc,
                       triangle
                FROM scriptlens.character_relationships
                WHERE script_id = :sid
                ORDER BY created_at
                """
            ),
            {"sid": script_id},
        ).mappings().all()
    return [
        {
            "id": str(row.get("id") or ""),
            "a_id": str(row.get("a_id") or ""),
            "b_id": str(row.get("b_id") or ""),
            "type": str(row.get("relationship_type") or ""),
            "polarity": str(row.get("polarity") or ""),
            "dynamic_arc": str(row.get("dynamic_arc") or ""),
            "triangle": str(row.get("triangle") or ""),
        }
        for row in rows
    ]


async def score_one_dimension(
    *,
    script_id: str,
    dimension: str,
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> dict[str, Any]:
    caller = caller or LlmCaller()
    rubric = load_rubric(_RUBRIC_ID)
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
            "tier": compliance.tier,
            "reason": compliance.reason,
            "evidence_scene_ids": _scene_ids_from_evidence(compliance.evidence_ref_ids),
            "baseline": baseline,
        }

    await run_tag_pipeline(
        script_ref=script_id,
        tag_set_ver=_TAG_SET_VERSION,
        caller=caller,
        engine=engine,
    )
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
        "tier": target.tier,
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

        progress_tracker.update_stage(script_id, "running_tag_pipeline", "running", detail="运行完整标签流水线")
        await run_tag_pipeline(
            script_ref=script_id,
            tag_set_ver=_TAG_SET_VERSION,
            caller=caller,
            engine=engine,
        )
        progress_tracker.update_stage(script_id, "running_tag_pipeline", "done", detail="标签流水线完成")

        progress_tracker.update_stage(script_id, "computing_signals", "running", detail="加载 rubric + 计算 signals")
        rubric = load_rubric(_RUBRIC_ID)
        ctx = build_signal_context(script_id=script_id, engine=engine)
        signals = await compute_signals(rubric, ctx, caller=caller)
        progress_tracker.update_stage(
            script_id,
            "computing_signals",
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
            item.tier_cuts = _safe_tier_cuts(tier_result.cuts)
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

        progress_tracker.update_stage(script_id, "building_pacing_and_actions", "running", detail="生成节奏曲线与改写动作")
        run_id = str(uuid.uuid4())
        actions = generate_actions(
            run_id=run_id,
            script_id=script_id,
            dim_scores=dim_scores,
            signal_values=signals,
        )
        pacing_curve = aggregate_pacing_curve(ctx)
        progress_tracker.update_stage(
            script_id,
            "building_pacing_and_actions",
            "done",
            detail=f"actions={len(actions)} pacing_points={len(pacing_curve)}",
        )

        progress_tracker.update_stage(
            script_id,
            "extracting_narrative",
            "running",
            detail="并行抽取速览卡 / 三幕节拍 / 人物关系图 / 看点事件",
        )
        reward_events: list[RewardEvent] = (
            await _optional_chain(
                "reward_extractor",
                extract_reward_events(script_id=script_id, caller=caller),
            )
            or []
        )
        coverage_task = _optional_chain(
            "coverage_chain",
            extract_coverage_card(script_id=script_id, caller=caller, engine=engine),
        )
        beat_task = _optional_chain(
            "beat_chain",
            extract_beat_sheet(
                script_id=script_id,
                reward_events=reward_events,
                caller=caller,
                engine=engine,
            ),
        )
        graph_task = _optional_chain(
            "character_graph_chain",
            extract_character_graph(script_id=script_id, caller=caller, engine=engine),
        )
        coverage_card, beat_sheet, character_graph = await asyncio.gather(
            coverage_task, beat_task, graph_task
        )
        progress_tracker.update_stage(
            script_id,
            "extracting_narrative",
            "done",
            detail=(
                f"速览{'已生成' if coverage_card else '降级'} · "
                f"节拍 {len(beat_sheet.acts) if beat_sheet else 0} 幕 · "
                f"人物 {len(character_graph.nodes) if character_graph else 0} 个 · "
                f"看点 {len(reward_events)}"
            ),
        )

        report_payload = _build_report_payload(
            meta=meta,
            decision=decision,
            weighted_overall_score=weighted.overall_score,
            dim_scores=dim_scores,
            compliance_payload=compliance.to_dict(),
            pacing_curve=pacing_curve,
            actions=actions,
            coverage_card=coverage_card,
            beat_sheet=beat_sheet,
            character_graph=character_graph,
            reward_events=reward_events,
            engine=engine,
        )

        progress_tracker.update_stage(script_id, "persisting", "running", detail="写入 reports/scoring_runs/script_scores/actions")
        _persist_report(
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
        progress_tracker.update_stage(script_id, "persisting", "done", detail="report persisted")
        progress_tracker.finalize(script_id)
        return report_payload
    except Exception as exc:  # noqa: BLE001
        logger.exception("generate_report failed script_id=%s: %s", script_id, exc)
        _mark_script_status(script_id=script_id, status="failed", failure_reason=f"{type(exc).__name__}: {exc}", engine=engine)
        progress_tracker.finalize(script_id, error=f"{type(exc).__name__}: {exc}")
        raise


def _build_report_payload(
    *,
    meta: _ScriptMeta,
    decision: Any,
    weighted_overall_score: float | None,
    dim_scores: list[DimensionScore],
    compliance_payload: dict[str, Any],
    pacing_curve: list[dict[str, Any]],
    actions: list[ImprovementAction],
    engine: Engine,
    coverage_card: Optional[CoverageCard] = None,
    beat_sheet: Optional[BeatSheet] = None,
    character_graph: Optional[CharacterGraph] = None,
    reward_events: Optional[list[RewardEvent]] = None,
) -> dict[str, Any]:
    decision_label = _normalize_decision_label(str(decision.decision))
    decision_payload = decision.payload if isinstance(decision.payload, dict) else {}
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
                "top_signals": dim.top_signals,
                "tier_cuts": _safe_tier_cuts(dim.tier_cuts),
                "reason": dim.reason,
                "evidence_ref_ids": _scene_ids_from_signal_refs(dim.signal_refs),
            }
        )
    tier_cuts_used = {
        item["dimension"]: dict(item.get("tier_cuts") or {})
        for item in scorecard
    }
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
    reward_events = reward_events or []
    compliance_hits = compliance_payload.get("hits") or []
    must_read_scene_ids = _select_beat_anchor_scenes(beat_sheet, top_k=3)
    risk_flags = _derive_risk_flags(compliance_payload)
    evidence_refs_payload = _build_evidence_refs_minimal(reward_events, compliance_hits)
    highlights_payload = _build_highlights_minimal(reward_events, beat_sheet, evidence_refs_payload)
    return {
            "script_id": meta.script_id,
            "title": meta.title,
        "decision": {
            "label": decision_label,
            "confidence": decision.confidence,
            "one_sentence_reason": decision.one_sentence_reason,
            "summary": decision.one_sentence_reason,
            "decision_inputs": {
                **decision_payload,
                "tier_cuts_used": tier_cuts_used,
                "overall_cuts": _compute_overall_cuts(scorecard),
                "raw_decision": decision.decision,
            },
        },
        "decision_reason": decision.one_sentence_reason,
        "overall_score": weighted_overall_score,
        "summary": decision.one_sentence_reason,
        "scorecard": scorecard,
        "compliance": compliance_payload,
        "drama_tags": _load_drama_tags(script_id=meta.script_id, engine=engine),
        "plot_units": _load_plot_units(script_id=meta.script_id, engine=engine),
        "characters": _load_characters(script_id=meta.script_id, engine=engine),
        "character_relationships": _load_character_relationships(script_id=meta.script_id, engine=engine),
        "must_read_scene_ids": must_read_scene_ids,
        "evidence_refs": evidence_refs_payload,
        "highlights": highlights_payload,
        "coverage_card": asdict(coverage_card) if coverage_card is not None else None,
        "beat_sheet": beat_sheet.to_dict() if beat_sheet is not None else None,
        "character_graph": character_graph.to_dict() if character_graph is not None else None,
        "risk_flags": risk_flags,
        "pacing_curve": pacing_curve,
        "evaluation": {
            "dimensions": [
                {
                    "key": item["dimension"],
                    "label": item["dimension"],
                    "score": item["score"],
                    "tier": item["tier"],
                    "confidence": item["confidence"],
                    "coverage_ratio": item["coverage_ratio"],
                    "reason": item["reason"],
                    "signal_refs": item["signal_refs"],
                    "evidence_ref_ids": item["evidence_ref_ids"],
                    "top_signals": item["top_signals"],
                    "tier_cuts": item["tier_cuts"],
                }
                for item in scorecard
            ],
            "rewrite_seeds": action_seeds,
        },
    }


def _persist_report(
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
            if dim.top_signals:
                signal_payload["__top_signals__"] = dim.top_signals
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
                    "model_ver": model_versions.get("primary_model", "rule-only"),
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
                    ON CONFLICT (script_id, dimension, tag_set_ver, score_ver)
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


def _collect_model_versions(signals: dict[str, SignalValue]) -> dict[str, Any]:
    models: list[str] = []
    for signal in signals.values():
        meta = signal.meta if isinstance(signal.meta, dict) else {}
        model = meta.get("model")
        if isinstance(model, str) and model.strip():
            models.append(model.strip())
    models = sorted(set(models))
    return {
        "primary_model": models[0] if models else "rule-only",
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
        return {"score": None, "tier": None, "reason": None, "evidence_scene_ids": []}
    payload = row.get("report_json")
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {}
    if not isinstance(payload, dict):
        return {"score": None, "tier": None, "reason": None, "evidence_scene_ids": []}
    if dimension == "compliance":
        compliance = payload.get("compliance") if isinstance(payload.get("compliance"), dict) else {}
        return {
            "score": compliance.get("score"),
            "tier": compliance.get("tier") or compliance.get("level"),
            "reason": compliance.get("reason"),
            "evidence_scene_ids": list(compliance.get("evidence_ref_ids") or []),
        }
    scorecard = payload.get("scorecard")
    if not isinstance(scorecard, list):
        return {"score": None, "tier": None, "reason": None, "evidence_scene_ids": []}
    for item in scorecard:
        if not isinstance(item, dict):
            continue
        if str(item.get("dimension") or "").strip() != dimension:
            continue
        return {
            "score": item.get("score"),
            "tier": item.get("tier") or item.get("level"),
            "reason": item.get("reason"),
            "evidence_scene_ids": list(item.get("evidence_ref_ids") or []),
        }
    return {"score": None, "tier": None, "reason": None, "evidence_scene_ids": []}


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
