"""Script scoring report pipeline (6 dimensions, self-contained, release/v1-mvp).

release/v1-mvp 评分流水线说明：

- 评分**完全独立于 tag_pipeline**（整剧抽情节打标签方向已废弃）。报告生成只跑：
    1. 叙事层 5 chain（reward / coverage / beat / character_graph / motivation）
    2. 6 维规则评分（dimension_scorer.score_<dim>，story/character/concept/
       emotion/pacing/dialogue）
    3. 合规扫描（compliance_scorer.screen_compliance，独立维度）
- Batch3 的 rubric / signal_catalog / score_registry / tag_pipeline /
  decision_aggregator / dimension_aggregator / improvement_action_generator /
  pacing_aggregator / evaluation_chain / bundle_extractor / plot_unit_segmenter /
  character_entity_resolver / relationship_candidate_generator /
  tag_alignment_analyzer 等模块进入 dead-code 隔离区（顶部 docstring 已标注），
  下次 cleanup PR 统一清理。
- payload 契约：drama_tags / plot_units / characters / character_relationships
  字段保留 key，但因 tag_pipeline 不再运行、对应表无数据，查询自然返回 []，
  前端 4 个 tab 走空态。
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
from service.script_ingestion_service import ScriptIngestionService
from service.script_progress_tracker import tracker as progress_tracker
from service.script_tools.beat_chain import BeatSheet, extract_beat_sheet
from service.script_tools.character_graph_chain import CharacterGraph, extract_character_graph
from service.script_tools.character_pipeline import (
    CharacterBio,
    CharacterEntity,
    cooccurrence_candidate_relationships,
    persist_bios,
    persist_entities,
    persist_relationships,
    resolve_entities,
    write_bios_concurrent,
)
from service.script_tools.compliance_scorer import screen_compliance
from service.script_tools.coverage_chain import CoverageCard, extract_coverage_card
from service.script_tools.pacing_aggregator import aggregate_pacing_curve
from service.script_tools.scene_repo import get_all_scenes
from service.script_tools.dimension_scorer import (
    ScoreOutput,
    score_character,
    score_concept,
    score_dialogue,
    score_emotion,
    score_pacing,
    score_story,
)
from service.script_tools.llm_caller import LlmCaller, ScoreLLMError
from service.script_tools.motivation_chain import MotivationResult, score_motivation
from service.script_tools.reward_extractor import RewardEvent, extract_reward_events
from utils.database import engine as default_engine

logger = logging.getLogger(__name__)

# release/v1-mvp 6 维评分版本号（不再走 rubric registry）
_SCORE_VER = "v1-mvp-6d"
_TAG_SET_VER_NONE = "none"

# 6 维默认权重（与 rubric_sets/v3.yaml.base_weight 保持一致；不引 yaml 避免拉回 rubric 依赖）
_DIM_BASE_WEIGHT: dict[str, float] = {
    "story": 0.20,
    "character": 0.20,
    "concept": 0.15,
    "emotion": 0.20,
    "pacing": 0.15,
    "dialogue": 0.10,
}
_DIM_ORDER: tuple[str, ...] = ("story", "character", "concept", "emotion", "pacing", "dialogue")
_DEFAULT_TIER_CUTS: dict[str, float] = {"p25": 4.0, "p50": 6.0, "p75": 8.0}

# schemas.script.TierName = Literal["excellent","good","weak","poor","insufficient"]
# 4 档分位与 service.script_tools.percentile_tier.resolve_tier 一致：
#   score >= p75=8 → excellent；>= p50=6 → good；>= p25=4 → weak；其它 → poor；None → insufficient。
# 历史的 "above / average / below" 字面量是错的，会让 ReportPayload.model_validate 直接 500。
def _score_to_tier(score: Optional[int | float]) -> str:
    if score is None:
        return "insufficient"
    s = float(score)
    p25 = _DEFAULT_TIER_CUTS["p25"]
    p50 = _DEFAULT_TIER_CUTS["p50"]
    p75 = _DEFAULT_TIER_CUTS["p75"]
    if s >= p75:
        return "excellent"
    if s >= p50:
        return "good"
    if s >= p25:
        return "weak"
    return "poor"


# ScoreOutput.level → 前端 confidence 文案（schemas.script.ConfidenceName: high/medium/low）
_LEVEL_TO_CONFIDENCE: dict[Optional[str], str] = {
    "high": "high",
    "medium": "medium",
    "low": "medium",
    None: "low",
}
_DIM_CONFIDENCE_FLOAT = {"high": 0.85, "medium": 0.6, "low": 0.35}


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
        verified = bool(getattr(ev, "quote_verified", False))
        refs.append(
            {
                "id": f"evi_reward_{ev.scene_id}_{ev.event_type}",
                "scene_id": ev.scene_id,
                "episode_no": ev.episode_no,
                "scene_no": ev.scene_no,
                "scene_label": None,
                "start_line": start_line,
                "end_line": end_line,
                # v3.5：quote 仅在 verified 时填 verbatim 原文；否则留空（避免被 audit 当 unverifiable）
                "quote": ev.quote_verbatim if verified else "",
                "claim": ev.claim,  # 诠释文本（always 可用，前端 tooltip 主字段）
                "quote_source": f"reward:{ev.event_type}",
                "quote_verified": verified,
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
        risk_verified = bool(hit.get("quote_verified", False))
        # risk hit 的 excerpt = verified 时是 verbatim 原文 + rationale，未 verified 时仅 rationale
        # 拆字段：verified 时 quote 是原文；未 verified 时 quote 留空，rationale 走 claim
        excerpt = str(hit.get("excerpt") or "")
        refs.append(
            {
                "id": f"evi_risk_{scene_id}_{idx}",
                "scene_id": scene_id,
                "episode_no": hit.get("episode_no"),
                "scene_no": hit.get("scene_no"),
                "scene_label": None,
                "start_line": start_line,
                "end_line": end_line,
                "quote": excerpt if risk_verified else "",
                "claim": excerpt if not risk_verified else "",
                "quote_source": "risk_hit",
                "quote_verified": risk_verified,
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
        verified = bool(getattr(ev, "quote_verified", False))
        # oneliner 始终用 claim（诠释文本，描述 reward 是什么），不再混 quote 进来
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
                "oneliner": _trim_oneliner(f"{headline} · {ev.claim}"),
                "claim": ev.claim,
                "quote": ev.quote_verbatim if verified else "",
                "quote_verified": verified,
                # legacy `evidence` 字段保留兼容旧前端（与 evidence_ref.quote 同语义：verified 时 verbatim，否则空）
                "evidence": ev.quote_verbatim if verified else ev.claim,
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


def _bios_to_payload(bios: Optional[list[CharacterBio]]) -> list[dict[str, Any]]:
    """character_pipeline.CharacterBio → ReportPayload.character_bios 字典形态。

    字段名严格对齐 schemas.script.ReportCharacterBio；前端 ``CharacterBioDTO``
    与下游高光集锦物料层都按这份契约消费。
    """
    if not bios:
        return []
    out: list[dict[str, Any]] = []
    for bio in bios:
        appearance = bio.appearance or {}
        outfit = appearance.get("outfit") or {}
        out.append(
            {
                "id": bio.id,
                "character_id": bio.character_id,
                "identity_present": bio.identity_present,
                "identity_hidden": bio.identity_hidden,
                "identity_origin": bio.identity_origin,
                "appearance": {
                    "age": str(appearance.get("age") or ""),
                    "height": str(appearance.get("height") or ""),
                    "build": str(appearance.get("build") or ""),
                    "facial": str(appearance.get("facial") or ""),
                    "signature_props": list(appearance.get("signature_props") or []),
                    "outfit": {
                        "material": str(outfit.get("material") or ""),
                        "palette": str(outfit.get("palette") or ""),
                        "form": str(outfit.get("form") or ""),
                    },
                },
                "persona_surface": bio.persona_surface,
                "persona_core": bio.persona_core,
                "weakness": bio.weakness,
                "arc_light": bio.arc_light,
                "dialogue_style": getattr(bio, "dialogue_style", "") or "",
                "catchphrases": list(bio.catchphrases or []),
                "relations_summary": list(bio.relations_summary or []),
                "notable_scenes": list(getattr(bio, "notable_scenes", []) or []),
                "bio_ver": bio.bio_ver,
                "source": bio.source,
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


def _score_one(
    *,
    dimension: str,
    script_id: str,
    meta: "_ScriptMeta",
    reward_events: list[RewardEvent],
    coverage_card: Optional[CoverageCard],
    beat_sheet: Optional[BeatSheet],
    character_graph: Optional[CharacterGraph],
    motivation_result: Optional[MotivationResult],
    engine: Engine,
) -> ScoreOutput:
    """6 维 dispatcher：把 chain 输出按维度分发给 dimension_scorer 函数。"""
    if dimension == "story":
        return score_story(
            beat_sheet=beat_sheet,
            reward_events=reward_events,
            total_episodes=meta.total_episodes,
        )
    if dimension == "character":
        return score_character(
            motivation_result=motivation_result,
            character_graph=character_graph,
        )
    if dimension == "concept":
        return score_concept(
            coverage_card=coverage_card,
            script_id=script_id,
            engine=engine,
        )
    if dimension == "emotion":
        return score_emotion(
            reward_events=reward_events,
            total_episodes=meta.total_episodes,
        )
    if dimension == "pacing":
        return score_pacing(
            script_id=script_id,
            reward_events=reward_events,
            engine=engine,
        )
    if dimension == "dialogue":
        return score_dialogue(script_id=script_id, engine=engine)
    raise ValueError(f"unsupported dimension={dimension!r}")


async def score_one_dimension(
    *,
    script_id: str,
    dimension: str,
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> dict[str, Any]:
    """单维度复评（用于 doc-studio rewrite 后的对比评分）。

    release/v1-mvp 简化版：按需重新跑该维度依赖的上游 chain + score_<dim>，
    不依赖 rubric / signal_catalog / tag_pipeline。
    """
    caller = caller or LlmCaller()
    valid = {"story", "character", "concept", "emotion", "pacing", "dialogue", "compliance"}
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

    reward_events: list[RewardEvent] = []
    coverage_card: Optional[CoverageCard] = None
    beat_sheet: Optional[BeatSheet] = None
    character_graph: Optional[CharacterGraph] = None
    motivation_result: Optional[MotivationResult] = None

    if dimension in {"story", "emotion", "pacing"}:
        reward_events = (
            await _optional_chain(
                "reward_extractor",
                extract_reward_events(script_id=script_id, caller=caller),
            )
            or []
        )
    if dimension == "story":
        beat_sheet = await _optional_chain(
            "beat_chain",
            extract_beat_sheet(
                script_id=script_id,
                reward_events=reward_events,
                caller=caller,
                engine=engine,
            ),
        )
    elif dimension == "character":
        motivation_result = await _optional_chain(
            "motivation_chain",
            score_motivation(script_id=script_id, caller=caller),
        )
        character_graph = await _optional_chain(
            "character_graph_chain",
            extract_character_graph(
                script_id=script_id, caller=caller, engine=engine
            ),
        )
    elif dimension == "concept":
        # 单维 debug 模式：用最小聚合输入（不强求 beat / graph / compliance 完整）
        debug_reward = (
            await _optional_chain(
                "reward_extractor",
                extract_reward_events(script_id=script_id, caller=caller),
            )
            or []
        )
        coverage_card = await _optional_chain(
            "coverage_chain",
            extract_coverage_card(
                title=meta.title,
                total_episodes=meta.total_episodes or 0,
                total_scenes=meta.total_scenes or 0,
                reward_events=debug_reward,
                beat_sheet=None,
                characters=_load_characters(script_id=script_id, engine=engine),
                relationships=_load_character_relationships(script_id=script_id, engine=engine),
                compliance_payload={},
                drama_tags=_load_drama_tags(script_id=script_id, engine=engine),
                caller=caller,
            ),
        )

    output = _score_one(
        dimension=dimension,
        script_id=script_id,
        meta=meta,
        reward_events=reward_events,
        coverage_card=coverage_card,
        beat_sheet=beat_sheet,
        character_graph=character_graph,
        motivation_result=motivation_result,
        engine=engine,
    )
    return {
        "dimension": dimension,
        "score": output.score,
        "tier": _score_to_tier(output.score),
        "reason": output.reason,
        "evidence_scene_ids": list(output.evidence_ref_ids),
        "baseline": baseline,
    }


async def generate_report(
    *,
    script_id: str,
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> dict[str, Any]:
    """release/v1-mvp 报告生成流水线（self-contained，零 tag_pipeline 依赖）。

    阶段：
      1. loading_meta              —— 读取剧本元数据
      2. extracting_narrative      —— 并行跑 reward → coverage/beat/graph/motivation 5 chain
      3. scoring_6d                —— 6 维规则评分（dimension_scorer）
      4. compliance                —— 独立合规扫描
      5. building_payload          —— 组装 report payload
      6. persisting                —— 落库 reports / scoring_runs / script_scores
    """
    caller = caller or LlmCaller()
    progress_tracker.start(script_id)

    try:
        progress_tracker.update_stage(script_id, "loading_meta", "running", detail="读取剧本元数据")
        meta = _load_script_meta(script_id, engine=engine)
        if meta is None:
            raise ValueError(f"script_id={script_id} 不存在")
        progress_tracker.update_stage(script_id, "loading_meta", "done", detail=f"title={meta.title}")

        # ① 人物归一化：必须先于 narrative 阶段。entities 是后续 character_graph
        # nodes / character_bios.character_id / character_relationships.src_dst 三处
        # 共用的 UUID id-space 锚点。共现得到的 candidate edges 给 chain 当 baseline
        # edges，避免 chain 在 baseline 路径下 edge_by_pair 为空导致 LLM enrichment
        # 全部被丢。
        progress_tracker.update_stage(
            script_id,
            "extracting_characters",
            "running",
            detail="按场次共现聚类 + 别名归一",
        )
        scenes = get_all_scenes(script_id=script_id, engine=engine)
        entities: list[CharacterEntity] = await resolve_entities(
            script_id=script_id, scenes=scenes
        )
        persist_entities(entities, script_id=script_id, engine=engine)
        candidate_relationships = cooccurrence_candidate_relationships(entities, scenes)
        progress_tracker.update_stage(
            script_id,
            "extracting_characters",
            "done",
            detail=(
                f"主要角色 {len(entities)} 个 · "
                f"候选关系边 {len(candidate_relationships)} 条"
            ),
        )

        progress_tracker.update_stage(
            script_id,
            "extracting_narrative",
            "running",
            detail="并行抽取看点 / 三幕节拍 / 人物关系图 / 人物小传 / 动机 / 合规",
        )
        # v3.6 并行调度（依赖图见 docs/08 §6）：
        #   reward / graph / motivation / bios / compliance 五条链全部独立
        #     —— 启动后**立即并发**，不再串行等 reward
        #   beat 唯一依赖 reward → reward.done() 后立即启动，藏进 bios 长尾
        #   coverage 依赖 beat + graph + compliance + reward → 串行（真依赖）
        # 预估端到端：max(graph, motivation, bios, compliance, reward+beat) + coverage
        reward_task = asyncio.create_task(
            _optional_chain(
                "reward_extractor",
                extract_reward_events(script_id=script_id, caller=caller),
            )
        )
        # 透传 entities + 共现候选关系：chain 走 resolver baseline 路径，节点 id =
        # entity.id（UUID），LLM enrichment 仅补 motivation/goal/obstacle 与边的
        # type/polarity；id-space 与 character_bios 严格一致。
        graph_task = asyncio.create_task(
            _optional_chain(
                "character_graph_chain",
                extract_character_graph(
                    script_id=script_id,
                    caller=caller,
                    engine=engine,
                    characters=[e.to_chain_dict() for e in entities],
                    relationships=candidate_relationships,
                ),
            )
        )
        motivation_task = asyncio.create_task(
            _optional_chain(
                "motivation_chain",
                score_motivation(script_id=script_id, caller=caller),
            )
        )
        # 小传与 graph / beat / motivation 并发：bio 单点失败不影响其他链。
        bios_task = asyncio.create_task(
            write_bios_concurrent(
                entities, scenes=scenes, caller=caller, semaphore_size=4
            )
        )
        # v3.6：合规扫描提前到 narrative 阶段并行——只依赖 scenes 表，不依赖任何 chain
        # 结果，并发收益 30-60s。compliance 阶段下方仅等 task 完成（大概率已 done）。
        compliance_task = asyncio.create_task(
            screen_compliance(script_id=script_id, caller=caller)
        )

        # reward 一完成立刻启动 beat（reward 通常 ~30s，bios 通常更长）
        reward_events: list[RewardEvent] = (await reward_task) or []
        beat_task = asyncio.create_task(
            _optional_chain(
                "beat_chain",
                extract_beat_sheet(
                    script_id=script_id,
                    reward_events=reward_events,
                    caller=caller,
                    engine=engine,
                ),
            )
        )

        beat_sheet, character_graph, motivation_result, bios = (
            await asyncio.gather(
                beat_task, graph_task, motivation_task, bios_task
            )
        )
        coverage_card: Optional[CoverageCard] = None  # 见下方 composing_coverage 阶段
        # 持久化 bios + 把 chain 输出的 enriched edges 写入 character_relationships。
        persist_bios(bios, engine=engine)
        if character_graph is not None:
            persist_relationships(
                character_graph.edges, script_id=script_id, engine=engine
        )
        progress_tracker.update_stage(
            script_id,
            "extracting_narrative",
            "done",
            detail=(
                f"节拍 {len(beat_sheet.acts) if beat_sheet else 0} 幕 · "
                f"人物 {len(character_graph.nodes) if character_graph else 0} 个 · "
                f"小传 {sum(1 for b in bios if b.persona_core or b.identity_present)}/{len(bios)} · "
                f"看点 {len(reward_events)} · "
                f"动机决策 {len(motivation_result.judged_decisions) if motivation_result else 0}"
            ),
        )

        progress_tracker.update_stage(
            script_id, "compliance", "running", detail="等待合规并行 task 完成"
        )
        compliance = await compliance_task
        progress_tracker.update_stage(
            script_id,
            "compliance",
            "done",
            detail=f"compliance.tier={compliance.tier}",
        )

        # v3.5：30 秒决策卡基于全剧聚合结论做综合判断（beat / graph / reward / compliance /
        # drama_tags 已全部就位），不再读场原文也不再附单场 anchor。
        progress_tracker.update_stage(
            script_id, "composing_coverage", "running", detail="基于全剧聚合数据撰写决策卡"
        )
        drama_tags_for_cov = _load_drama_tags(script_id=script_id, engine=engine)
        characters_for_cov = _load_characters(script_id=script_id, engine=engine)
        relationships_for_cov = _load_character_relationships(script_id=script_id, engine=engine)
        coverage_card = await _optional_chain(
            "coverage_chain",
            extract_coverage_card(
                title=meta.title,
                total_episodes=meta.total_episodes or 0,
                total_scenes=meta.total_scenes or 0,
                reward_events=reward_events,
                beat_sheet=beat_sheet,
                characters=characters_for_cov,
                relationships=relationships_for_cov,
                compliance_payload=compliance.to_dict(),
                drama_tags=drama_tags_for_cov,
                caller=caller,
            ),
        )
        progress_tracker.update_stage(
            script_id,
            "composing_coverage",
            "done",
            detail=f"速览{'已生成' if coverage_card else '降级'}",
        )

        progress_tracker.update_stage(
            script_id, "scoring_6d", "running", detail="6 维规则评分（self-contained）"
        )
        dim_outputs: dict[str, ScoreOutput] = {
            dim: _score_one(
                dimension=dim,
                script_id=script_id,
                meta=meta,
            reward_events=reward_events,
                coverage_card=coverage_card,
                beat_sheet=beat_sheet,
            character_graph=character_graph,
                motivation_result=motivation_result,
                engine=engine,
            )
            for dim in _DIM_ORDER
        }
        scored_count = sum(1 for o in dim_outputs.values() if o.score is not None)
        progress_tracker.update_stage(
            script_id,
            "scoring_6d",
            "done",
            detail=f"dimensions={len(dim_outputs)} scored={scored_count}",
        )

        progress_tracker.update_stage(
            script_id, "building_payload", "running", detail="组装报告 payload"
        )
        run_id = str(uuid.uuid4())
        report_payload = _build_report_payload(
            meta=meta,
            dim_outputs=dim_outputs,
            compliance_payload=compliance.to_dict(),
            coverage_card=coverage_card,
            beat_sheet=beat_sheet,
            character_graph=character_graph,
            character_bios=bios,
            reward_events=reward_events,
            engine=engine,
        )
        progress_tracker.update_stage(script_id, "building_payload", "done")

        progress_tracker.update_stage(
            script_id,
            "persisting",
            "running",
            detail="写入 reports / scoring_runs / script_scores",
        )
        _persist_report(
            script_id=script_id,
            run_id=run_id,
            dim_outputs=dim_outputs,
            report_payload=report_payload,
            engine=engine,
        )
        _mark_script_status(script_id=script_id, status="ready", failure_reason=None, engine=engine)
        progress_tracker.update_stage(script_id, "persisting", "done", detail="report persisted")
        progress_tracker.finalize(script_id)
        return report_payload
    except Exception as exc:  # noqa: BLE001
        logger.exception("generate_report failed script_id=%s: %s", script_id, exc)
        _mark_script_status(
            script_id=script_id,
            status="failed",
            failure_reason=f"{type(exc).__name__}: {exc}",
            engine=engine,
        )
        progress_tracker.finalize(script_id, error=f"{type(exc).__name__}: {exc}")
        raise


def _scorecard_from_outputs(dim_outputs: dict[str, ScoreOutput]) -> list[dict[str, Any]]:
    """6 维 ScoreOutput → scorecard payload。

    与 Batch3 时代 scorecard 字段保持兼容（前端 zero-change）；信号/聚合相关字段
    （signal_refs / top_signals）置空，因为 release/v1-mvp 走规则评分不分信号。
    """
    scorecard: list[dict[str, Any]] = []
    for dim in _DIM_ORDER:
        out = dim_outputs.get(dim)
        if out is None:
            scorecard.append(
                {
                    "dimension": dim,
                    "score": None,
                    "tier": "insufficient",
                    "confidence": "low",
                    "coverage_ratio": 0.0,
                    "signal_refs": [],
                    "top_signals": [],
                    "tier_cuts": dict(_DEFAULT_TIER_CUTS),
                    "reason": "未参与评分",
                    "evidence_ref_ids": [],
                }
            )
            continue
        scorecard.append(
            {
                "dimension": dim,
                "score": out.score,
                "tier": _score_to_tier(out.score),
                "confidence": _LEVEL_TO_CONFIDENCE.get(out.level, "low"),
                "coverage_ratio": 1.0 if out.score is not None else 0.0,
                "signal_refs": [],
                "top_signals": [],
                "tier_cuts": dict(_DEFAULT_TIER_CUTS),
                "reason": out.reason,
                "evidence_ref_ids": list(out.evidence_ref_ids),
            }
        )
    return scorecard


def _compute_overall_score(dim_outputs: dict[str, ScoreOutput]) -> Optional[float]:
    """对有分维度按 _DIM_BASE_WEIGHT 加权（仅有分项参与归一化）。"""
    total_weight = 0.0
    weighted_sum = 0.0
    for dim in _DIM_ORDER:
        out = dim_outputs.get(dim)
        if out is None or out.score is None:
            continue
        weight = _DIM_BASE_WEIGHT.get(dim, 0.0)
        total_weight += weight
        weighted_sum += float(out.score) * weight
    if total_weight <= 0:
        return None
    return round(weighted_sum / total_weight, 2)


def _derive_decision(
    overall_score: Optional[float],
    compliance_payload: dict[str, Any],
) -> tuple[str, str, str]:
    """简单规则决策（release/v1-mvp）：基于 overall_score + 合规级别。

    返回 (label, confidence, one_sentence_reason)。
    label ∈ {recommend_continue, cautious_continue, not_recommended}。
    """
    compliance_tier = str(compliance_payload.get("tier") or "").strip()
    if compliance_tier == "high_risk":
        return (
            "not_recommended",
            "high",
            "合规扫描发现高风险红线命中，建议先做内容整改。",
        )
    if overall_score is None:
        return (
            "cautious_continue",
            "low",
            "评分维度证据不足，建议人工复核后再定。",
        )
    if overall_score >= 6.5:
        return (
            "recommend_continue",
            "high",
            f"6 维加权 {overall_score:.1f}/10，整体表现良好，建议推进。",
        )
    if overall_score >= 4.5:
        return (
            "cautious_continue",
            "medium",
            f"6 维加权 {overall_score:.1f}/10，存在明显短板，建议针对弱项改写后复评。",
        )
    return (
        "not_recommended",
        "medium",
        f"6 维加权 {overall_score:.1f}/10，多维度低分，整体故事质量需要重写。",
    )


def _build_report_payload(
    *,
    meta: _ScriptMeta,
    dim_outputs: dict[str, ScoreOutput],
    compliance_payload: dict[str, Any],
    engine: Engine,
    coverage_card: Optional[CoverageCard] = None,
    beat_sheet: Optional[BeatSheet] = None,
    character_graph: Optional[CharacterGraph] = None,
    character_bios: Optional[list[CharacterBio]] = None,
    reward_events: Optional[list[RewardEvent]] = None,
) -> dict[str, Any]:
    """release/v1-mvp 报告 payload 组装。

    payload 字段契约（与前端 4 个 tab + scorecard + decision 保持兼容）：
      - scorecard / evaluation.dimensions  : 6 维 from dim_outputs
      - decision                            : 规则决策（基于 overall_score + compliance）
      - overall_score                       : 6 维加权和（仅有分项参与归一）
      - compliance / risk_flags             : 独立合规扫描
      - drama_tags / plot_units /
        characters / character_relationships: tag_pipeline 已废弃 → 表为空 → 自然为 []
      - coverage_card / beat_sheet /
        character_graph                     : 5 chain 输出
      - reward / evidence_refs / highlights /
        must_read_scene_ids                 : 看点 + 证据锚点
      - pacing_curve                        : v4 emotion-arc（reward + beat_sheet + scenes，
                                              零 plot_unit 依赖）。详见
                                              docs/2026-05-30-pacing-curve-v4.md
      - evaluation.rewrite_seeds            : v1-mvp 暂留空 []（Batch3 actions 体系
                                              已废弃，rewrite 由 doc-studio agent 接管）
    """
    scorecard = _scorecard_from_outputs(dim_outputs)
    overall_score = _compute_overall_score(dim_outputs)
    decision_label, decision_confidence, decision_reason = _derive_decision(
        overall_score, compliance_payload
    )
    tier_cuts_used = {item["dimension"]: dict(item.get("tier_cuts") or {}) for item in scorecard}

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
            "confidence": decision_confidence,
            "one_sentence_reason": decision_reason,
            "summary": decision_reason,
            "decision_inputs": {
                "tier_cuts_used": tier_cuts_used,
                "overall_cuts": _compute_overall_cuts(scorecard),
                "raw_decision": decision_label,
                "score_ver": _SCORE_VER,
            },
        },
        "decision_reason": decision_reason,
        "overall_score": overall_score,
        "summary": decision_reason,
        "scorecard": scorecard,
        "compliance": compliance_payload,
        # drama_tags / plot_units 仍走 tag_pipeline（已废弃，表为空 → []）
        "drama_tags": _load_drama_tags(script_id=meta.script_id, engine=engine),
        "plot_units": _load_plot_units(script_id=meta.script_id, engine=engine),
        # characters / character_relationships v1-mvp 由 character_pipeline 写入；
        # 直接从表读出，与 character_graph nodes 共享 UUID id-space。
        "characters": _load_characters(script_id=meta.script_id, engine=engine),
        "character_relationships": _load_character_relationships(
            script_id=meta.script_id, engine=engine
        ),
        "character_bios": _bios_to_payload(character_bios),
        "must_read_scene_ids": must_read_scene_ids,
        "evidence_refs": evidence_refs_payload,
        "highlights": highlights_payload,
        "coverage_card": asdict(coverage_card) if coverage_card is not None else None,
        "beat_sheet": beat_sheet.to_dict() if beat_sheet is not None else None,
        "character_graph": character_graph.to_dict() if character_graph is not None else None,
        "risk_flags": risk_flags,
        "pacing_curve": aggregate_pacing_curve(
            script_id=meta.script_id,
            reward_events=list(reward_events or []),
            beat_sheet=beat_sheet,
            engine=engine,
        ),
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
            "rewrite_seeds": [],
        },
    }


def _persist_report(
    *,
    script_id: str,
    run_id: str,
    dim_outputs: dict[str, ScoreOutput],
    report_payload: dict[str, Any],
    engine: Engine,
) -> None:
    """release/v1-mvp 持久化：reports + scoring_runs + script_scores（6 维）。

    rubric/signal/genre/actions 等 Batch3 体系字段填占位值，保持表结构兼容。
    scoring_improvement_actions 表 release/v1-mvp 不写入（actions 体系已废弃）。
    """
    now = datetime.utcnow()
    report_id = str(uuid.uuid4())
    overall_score = report_payload.get("overall_score")
    input_hash = hashlib.sha256(
        json.dumps(
            {
                "script_id": script_id,
                "score_ver": _SCORE_VER,
                "tag_set_ver": _TAG_SET_VER_NONE,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    quality_flags = {
        "insufficient_dimensions": [
            dim for dim, out in dim_outputs.items() if out.score is None
        ],
        "overall_score": overall_score,
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
                "rubric_version": _SCORE_VER,
                "tag_set_ver": _TAG_SET_VER_NONE,
                "input_hash": input_hash,
                "genre_scope": "default",
                "episode_count": 0,
                "plot_unit_count": 0,
                "quality_flags": json.dumps(quality_flags, ensure_ascii=False),
                "model_versions": json.dumps({"primary_model": "rule-only"}, ensure_ascii=False),
                "prompt_versions": json.dumps({}, ensure_ascii=False),
                "status": "done",
                "error": None,
                "created_at": now,
            },
        )

        score_rows = []
        for dim, out in dim_outputs.items():
            score_rows.append(
                {
            "id": str(uuid.uuid4()),
                    "script_id": script_id,
                    "run_id": run_id,
                    "dimension": dim,
                    "primary_dimension": dim,
                    "score": float(out.score if out.score is not None else 0.0),
                    "percentile": None,
                    "tier": _score_to_tier(out.score),
                    "confidence": _DIM_CONFIDENCE_FLOAT.get(
                        _LEVEL_TO_CONFIDENCE.get(out.level, "low"), 0.35
                    ),
                    "coverage_ratio": 1.0 if out.score is not None else 0.0,
                    "signals": json.dumps({"reason": out.reason}, ensure_ascii=False),
                    "weights": json.dumps(
                        {"base_weight": _DIM_BASE_WEIGHT.get(dim, 0.0)}, ensure_ascii=False
                    ),
                    "tag_set_ver": _TAG_SET_VER_NONE,
                    "score_ver": _SCORE_VER,
                    "model_ver": "rule-only",
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

        decision_payload = (
            report_payload.get("decision", {}).get("decision_inputs") or {}
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
