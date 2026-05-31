"""Script scoring report pipeline — Wave C-3c (v4 投资决策评分主链路).

报告生成流程（self-contained，零 tag_pipeline 依赖）：
  1. 叙事层 5 chain（reward / coverage / beat / character_graph / motivation）
  2. 合规扫描（compliance_scorer.screen_compliance，独立 gate）
  3. v4 投资决策评分（service.scoring.score_script —
     hook / archetype / payoff / monetization / producibility 五维 + compliance 一票否决）
  4. 报告 payload 组装（v4 verdict / investment_score / evaluation_v4 / top_improvements）
  5. 落库 reports + scoring_runs

v3 6 维规则评分（service/script_tools/dimension_scorer.py）已于 Wave C-3a 删除；
v3 schema 字段（decision / overall_score / scorecard / evaluation）已于 Wave C-3c
从 ReportPayload 移除。service.scoring/ 模块是唯一评分入口。
详见 docs/2026-05-31-投资决策评分框架-v4.md。

历史 cleanup 残留（这些模块已废弃但 grep 仍可见）：
- score_registry / tag_pipeline / decision_aggregator / dimension_aggregator /
  improvement_action_generator / bundle_extractor / plot_unit_segmenter / 等模块
  进入 dead-code 隔离区，下次 cleanup PR 统一清理。
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
from typing import Any, Awaitable, Dict, List, Optional

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
from service.script_tools.compliance_scorer import ComplianceResult, screen_compliance
from service.script_tools.coverage_chain import CoverageCard, extract_coverage_card
from service.script_tools.pacing_aggregator import aggregate_pacing_curve
from service.script_tools.scene_repo import get_all_scenes
from service.script_tools.llm_caller import LlmCaller, ScoreLLMError
from service.script_tools.motivation_chain import MotivationResult, score_motivation
from service.script_tools.reward_extractor import RewardEvent, extract_reward_events
from service.scoring import ScoringContext, score_script
from utils.database import engine as default_engine

logger = logging.getLogger(__name__)

# Wave C-3a (2026-05-31)：v3 6 维评分常量已删除，正式切到 v4 投资决策 5 维。
# 详见 docs/2026-05-31-投资决策评分框架-v4.md 与 service/scoring/ 模块。
_SCORE_VER = "v4-cn-2026-05-31"
_TAG_SET_VER_NONE = "none"


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


# W1.3 (2026-05-31)：全链路 provenance 透传到 report.meta.chain_status。
# 每个 chain 完成（或失败）后通过 _record_chain_status 把状态写入 _ChainStatusCollector，
# 最终在 _build_report_payload 里序列化进 payload，前端据此渲染降级提示条。
_ChainStatusCollector = Dict[str, Dict[str, Any]]


def _new_chain_status_collector() -> _ChainStatusCollector:
    return {}


def _record_chain_status(
    collector: _ChainStatusCollector,
    chain_name: str,
    *,
    status: str,  # ok | degraded | failed
    source: str,  # llm | hybrid | rule_fallback
    fallback_reasons: Optional[List[str]] = None,
    partial_failure_fields: Optional[List[str]] = None,
) -> None:
    """把 chain 的 provenance 写入 collector。

    禁止 silent success：每个 chain 调用结束**必须**调一次这个 helper，
    哪怕是 status="ok"。否则就会出现「chain 跑了但 chain_status 漏了」的 invisibility。
    """
    if status not in ("ok", "degraded", "failed"):
        raise ValueError(f"invalid chain status: {status!r}")
    if source not in ("llm", "hybrid", "rule_fallback"):
        raise ValueError(f"invalid chain source: {source!r}")
    collector[chain_name] = {
        "status": status,
        "source": source,
        "fallback_reasons": list(fallback_reasons or []),
        "partial_failure_fields": list(partial_failure_fields or []),
    }


async def _optional_chain(
    name: str,
    coro: Awaitable[Any],
    *,
    chain_status: Optional[_ChainStatusCollector] = None,
) -> Any:
    """叙事层 chain 可降级为 None；只吞已知业务失败，避免一个 LLM JSON 解析失败把整份报告拖崩。

    W1.3：失败时同时写入 chain_status（status=failed, source=rule_fallback, reasons=异常类型）。
    成功时**不**写入 status——由调用方根据 chain 自身的 source 字段写入 ok/degraded。
    """
    try:
        return await coro
    except (ScoreLLMError, ValueError) as exc:
        logger.exception("%s failed and will be stored as null: %s", name, exc)
        if chain_status is not None:
            _record_chain_status(
                chain_status,
                name,
                status="failed",
                source="rule_fallback",
                fallback_reasons=[f"{type(exc).__name__}: {str(exc)[:200]}"],
            )
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


def _trim_oneliner(s: str) -> str:
    """主要看点 oneliner：只做空白规范化，不再截断。

    v3.7.5c 根因：此前 max_len=40/90 与 beat_chain _extract_plot_excerpt 的 [:38]
    叠加，导致「开场抓人 · …」在半句处断掉。前端 highlightOneliner 已支持多行
    完整展示，后端应传完整语义，不做 UI 层截断。
    """
    return (s or "").strip().replace("\n", " ")


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
    scenes_by_id: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """从 reward_events + 开场节拍派生 highlights[]。

    highlight.id 复用对应 evidence_ref.id（同空间），前端点击高亮即可定位锚点。

    Args:
        scenes_by_id: 可选 scene 查找表（id → Scene 对象），用于补齐 hook 类
            highlight 的 episode_no / scene_no / scene_label —— 不然前端只能
            fallback 到 scene_id 前 6 位字符串（"9ad1e2" 这种垃圾定位符）。
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
                    # v3.7.2：从 scenes_by_id 反查补齐定位字段（episode_no / scene_no /
                    # scene_label），避免前端 fallback 到 scene_id 前 6 位。
                    scene = (scenes_by_id or {}).get(beat.anchor_scene_id)
                    out.append(
                        {
                            "id": f"evi_hook_{beat.anchor_scene_id}",
                            "type": "hook",
                            "scene_id": beat.anchor_scene_id,
                            "episode_no": getattr(scene, "episode_no", None) if scene else None,
                            "scene_no": getattr(scene, "scene_no", None) if scene else None,
                            "scene_label": getattr(scene, "scene_label", None) if scene else None,
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
                "gender": getattr(bio, "gender", "unknown") or "unknown",
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


# ============================================================
# v4 投资决策评分 — 单维复评白名单
# ============================================================
# Wave C-2 (2026-05-31)：v3 6 维（story / character / concept / emotion / pacing / dialogue）
# 已被 v4 5 维（hook / archetype / payoff / monetization / producibility）整体替换。
# 维度概念**不正交映射**（v3 = 剧本工艺；v4 = 投资决策），不做"自动翻译"，
# v3 dim_key 显式抛 ValueError 让上游升级，避免静默错误。
# COMPLIANCE 是独立 gate，不进 5 维，但允许作为 dimension 参数复跑合规审核。
_V4_DIMENSIONS: frozenset[str] = frozenset(
    {"hook", "archetype", "payoff", "monetization", "producibility", "compliance"}
)


async def score_one_dimension(
    *,
    script_id: str,
    dimension: str,
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> dict[str, Any]:
    """v4 投资决策单维度复评。

    入口场景：
      - doc-studio rewrite 后的对比评分（改完一场后看维度分有没有动）
      - agent 工具 `score_dimension_tool`（用户追问"为什么 HOOK 给 4 分"）

    实现：拉取该维度依赖的上游 chain（reward / coverage / character_graph / motivation），
    装配 ScoringContext，调用 scoring.score_dimension(dim_key, ctx) 单维重算。
    COMPLIANCE 走 screen_compliance（独立 gate，与 5 维不同 pipeline）。

    返回：
      {
        dimension, score, tier, reason, evidence_scene_ids,
        is_dealbreaker_triggered,  # v4 新增
        signals,                    # v4 新增：每个 signal 的 score/source/status/detail
        baseline,                   # 上次报告同维度的分数（用于改写前后对比）
      }
    """
    caller = caller or LlmCaller()
    if dimension not in _V4_DIMENSIONS:
        raise ValueError(
            f"unknown dimension={dimension!r}; valid={sorted(_V4_DIMENSIONS)}. "
            "v3 6 维已被 v4 5 维替换，详见 docs/2026-05-31-投资决策评分框架-v4.md"
        )
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
            "is_dealbreaker_triggered": compliance.tier == "high_risk",
            "signals": [],
            "baseline": baseline,
        }

    # ============================================================
    # 装配上游依赖（按维度需要选择性触发，避免全量拉链）
    # 依赖矩阵（详见 service/scoring/dimensions/*.py）：
    #   hook         : scenes + llm_caller
    #   archetype    : scenes + coverage_card + character_graph + llm_caller
    #   payoff       : scenes + reward_events + beat_sheet
    #   monetization : scenes + reward_events + total_episodes
    #   producibility: scenes + total_episodes
    # ============================================================
    scenes = get_all_scenes(script_id=script_id, engine=engine)
    reward_events: list[RewardEvent] = []
    coverage_card: Optional[CoverageCard] = None
    beat_sheet: Optional[BeatSheet] = None
    character_graph: Optional[CharacterGraph] = None

    needs_reward = dimension in {"payoff", "monetization"}
    needs_beat = dimension == "payoff"
    needs_graph = dimension == "archetype"
    needs_coverage = dimension == "archetype"

    if needs_reward:
        reward_events = (
            await _optional_chain(
                "reward_extractor",
                extract_reward_events(script_id=script_id, caller=caller),
            )
            or []
        )
    if needs_beat:
        beat_sheet = await _optional_chain(
            "beat_chain",
            extract_beat_sheet(
                script_id=script_id,
                reward_events=reward_events,
                caller=caller,
                engine=engine,
            ),
        )
    if needs_graph:
        character_graph = await _optional_chain(
            "character_graph_chain",
            extract_character_graph(
                script_id=script_id, caller=caller, engine=engine
            ),
        )
    if needs_coverage:
        coverage_card = await _optional_chain(
            "coverage_chain",
            extract_coverage_card(
                title=meta.title,
                total_episodes=meta.total_episodes or 0,
                total_scenes=meta.total_scenes or 0,
                reward_events=reward_events,
                beat_sheet=beat_sheet,
                characters=_load_characters(script_id=script_id, engine=engine),
                relationships=_load_character_relationships(script_id=script_id, engine=engine),
                compliance_payload={},
                drama_tags=_load_drama_tags(script_id=script_id, engine=engine),
                caller=caller,
            ),
        )

    # 延迟 import 避免循环依赖（scoring 模块已经 import 了上游 chain 类型）
    from service.scoring import score_dimension as scoring_score_dimension

    scoring_ctx = ScoringContext(
        script_id=script_id,
        scenes=scenes,
        total_episodes=meta.total_episodes or 0,
        beat_sheet=beat_sheet,
        reward_events=reward_events,
        character_graph=character_graph,
        coverage_card=coverage_card,
        motivation_result=None,  # v4 5 维都不依赖 motivation_result
        compliance=None,  # 单维复评不需要 compliance（独立 gate 走另一分支）
        llm_caller=caller,
    )
    dim_score = await scoring_score_dimension(dimension, scoring_ctx)
    return {
        "dimension": dimension,
        "score": dim_score.score,
        "tier": dim_score.tier.value,
        "reason": dim_score.reason,
        "evidence_scene_ids": _scene_ids_from_evidence(list(dim_score.evidence_ref_ids)),
        "is_dealbreaker_triggered": dim_score.is_dealbreaker_triggered,
        "signals": [
            {
                "key": s.key,
                "score": s.score,
                "source": s.source.value,
                "status": s.status.value,
                "detail": s.detail,
                "fallback_reason": s.fallback_reason,
            }
            for s in dim_score.signals
        ],
        "baseline": baseline,
    }


async def generate_report(
    *,
    script_id: str,
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> dict[str, Any]:
    """Wave C-3a 报告生成流水线（self-contained，零 tag_pipeline 依赖，v4 投资决策主链路）。

    阶段：
      1. loading_meta              —— 读取剧本元数据
      2. extracting_narrative      —— 并行跑 reward → coverage/beat/graph/motivation 5 chain
      3. compliance                —— 独立合规扫描（独立 gate，high_risk 一票否决）
      4. scoring_v4                —— v4 投资决策评分（5 维 + compliance gate）
      5. building_payload          —— 组装 report payload（v3 字段从 v4 verdict 兼容映射）
      6. persisting                —— 落库 reports / scoring_runs

    W1.7 (2026-05-31)：per-script DB advisory lock。
    阻止同一 script_id 被并发分析（reanalyze 重入、双开 worker、用户连点重新诊断）。
    旧实现允许两个 generate_report 同时跑，会产生：
      - persist_entities 盲删旧 character_entities → 互踩 ID
      - 双份 scoring_runs 写入
      - report_json 互覆盖
    advisory_lock 是 transaction-bound，连接关闭自动释放，零运维成本。
    """
    caller = caller or LlmCaller()
    progress_tracker.start(script_id)

    # W1.3 (2026-05-31)：收集每个 chain 的 provenance。最终写入 report.meta.chain_status。
    chain_status: _ChainStatusCollector = _new_chain_status_collector()

    # W1.6 (2026-05-31)：run_id 提到流水线开头，立刻 INSERT scoring_runs status='running'。
    # 用户从 dashboard 能看到一行「analysis_in_progress」记录，而不是「沉默几分钟」。
    run_id = str(uuid.uuid4())

    # W1.7 (2026-05-31)：尝试拿 advisory lock；拿不到说明已有其他 generate_report 在跑。
    lock_acquired = _try_acquire_script_lock(script_id, engine=engine)
    if not lock_acquired:
        raise ValueError(
            f"script_id={script_id} 正在被其他分析任务处理，请等待当前任务结束后再触发"
        )

    _insert_scoring_run_running(
        run_id=run_id, script_id=script_id, engine=engine,
    )
    # W1.5：在 scripts.last_analysis_status 标记 running，让 dashboard 列能立刻显示
    # 「正在分析」徽章。scripts.status 保持不变（ingest 维度）。
    _mark_analysis_status(script_id=script_id, status="running", engine=engine)

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
                chain_status=chain_status,
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
                chain_status=chain_status,
            )
        )
        motivation_task = asyncio.create_task(
            _optional_chain(
            "motivation_chain",
            score_motivation(script_id=script_id, caller=caller),
            chain_status=chain_status,
        )
        )
        # 小传与 graph / beat / motivation 并发：bio 单点失败不影响其他链。
        # W1.8 (2026-05-31): bios 不走 _optional_chain 因为 write_bios_concurrent
        # 内部已有 return_exceptions=True + _empty_bio 占位降级，无需重复包装。
        bios_task = asyncio.create_task(
            write_bios_concurrent(
                entities, scenes=scenes, caller=caller, semaphore_size=4
            )
        )
        # v3.6：合规扫描提前到 narrative 阶段并行——只依赖 scenes 表，不依赖任何 chain
        # 结果，并发收益 30-60s。compliance 阶段下方仅等 task 完成（大概率已 done）。
        # W1.8: compliance 也走 _optional_chain，与叙事 chain 一致 best-effort，
        # 一个 LLM 抖动不再拖垮整报告。
        compliance_task = asyncio.create_task(
            _optional_chain(
                "compliance_chain",
                screen_compliance(script_id=script_id, caller=caller),
                chain_status=chain_status,
            )
        )

        # reward 一完成立刻启动 beat（reward 通常 ~30s，bios 通常更长）
        reward_events: list[RewardEvent] = (await reward_task) or []
        # W1.3: reward 成功的话写 ok（_optional_chain 只写 failed）
        if "reward_extractor" not in chain_status:
            _record_chain_status(
                chain_status, "reward_extractor", status="ok", source="llm"
            )
        beat_task = asyncio.create_task(
            _optional_chain(
                "beat_chain",
                extract_beat_sheet(
                    script_id=script_id,
                    reward_events=reward_events,
                    caller=caller,
                    engine=engine,
                ),
                chain_status=chain_status,
            )
        )

        # W1.8 (2026-05-31): gather 加 return_exceptions=True 防止 sibling chain
        # 被 cancel；任一非 _optional_chain 包装的 task 抛出时也只是返回 Exception
        # 实例，不再 propagate。
        gathered = await asyncio.gather(
            beat_task, graph_task, motivation_task, bios_task,
            return_exceptions=True,
        )

        def _safe_unwrap(result: Any, chain_name: str) -> Any:
            if isinstance(result, BaseException):
                logger.exception(
                    "%s raised unexpectedly through gather: %s", chain_name, result,
                )
                if chain_name not in chain_status:
                    _record_chain_status(
                        chain_status, chain_name,
                        status="failed", source="rule_fallback",
                        fallback_reasons=[
                            f"{type(result).__name__}: {str(result)[:200]}"
                        ],
                    )
                return None
            return result

        beat_sheet = _safe_unwrap(gathered[0], "beat_chain")
        character_graph = _safe_unwrap(gathered[1], "character_graph_chain")
        motivation_result = _safe_unwrap(gathered[2], "motivation_chain")
        bios_raw = _safe_unwrap(gathered[3], "bios")
        bios = bios_raw if bios_raw is not None else []

        # W1.3：根据每个 chain 自带的 source/fallback_reasons 字段写 chain_status。
        # 失败已在 _optional_chain / _safe_unwrap 写入；这里只补 ok / degraded。
        if beat_sheet is not None and "beat_chain" not in chain_status:
            _record_chain_status(
                chain_status, "beat_chain",
                status="degraded" if beat_sheet.fallback_reasons else "ok",
                source=beat_sheet.source,
                fallback_reasons=beat_sheet.fallback_reasons,
            )
        if character_graph is not None and "character_graph_chain" not in chain_status:
            graph_failed = (character_graph.enrichment_status == "failed")
            _record_chain_status(
                chain_status, "character_graph_chain",
                status="degraded" if graph_failed or character_graph.enrichment_status == "degraded" else "ok",
                source="rule_fallback" if graph_failed else "llm",
                fallback_reasons=character_graph.enrichment_failed_reasons,
            )
        if motivation_result is not None and "motivation_chain" not in chain_status:
            mot_reasons: List[str] = []
            mot_partial: List[str] = []
            if motivation_result.partial_failure:
                mot_reasons.append(
                    f"partial_judge_failure:{motivation_result.judged_count}/{motivation_result.attempted_count}"
                )
                mot_partial.append("judged_decisions")
            if motivation_result.filter_degraded:
                mot_reasons.append(
                    f"filter_degraded:{motivation_result.filter_degraded_reason or 'top_k_fallback'}"
                )
            _record_chain_status(
                chain_status, "motivation_chain",
                status="degraded" if mot_reasons else "ok",
                source="hybrid" if mot_reasons else "llm",
                fallback_reasons=mot_reasons,
                partial_failure_fields=mot_partial,
            )
        if bios is not None and "bios" not in chain_status:
            failed_bios = [
                b for b in bios
                if isinstance(b.evidence, dict) and b.evidence.get("status") == "failed"
            ]
            _record_chain_status(
                chain_status, "bios",
                status="degraded" if failed_bios else "ok",
                source="hybrid" if failed_bios else "llm",
                fallback_reasons=(
                    [f"failed_bio_count={len(failed_bios)}"] if failed_bios else []
                ),
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
        # W1.8: compliance 走了 _optional_chain，失败会返回 None；这里给降级口径。
        if compliance is None:
            compliance = ComplianceResult.empty()  # 降级为空报告占位
        else:
            if "compliance_chain" not in chain_status:
                _record_chain_status(
                    chain_status, "compliance_chain", status="ok", source="llm"
                )
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
            chain_status=chain_status,
        )
        # W1.3: coverage 成功的话根据自身 source 写 ok/degraded
        if coverage_card is not None and "coverage_chain" not in chain_status:
            _record_chain_status(
                chain_status, "coverage_chain",
                status="degraded" if coverage_card.fallback_reasons else "ok",
                source=coverage_card.source,
                fallback_reasons=coverage_card.fallback_reasons,
                partial_failure_fields=(
                    (["strengths"] if coverage_card.strengths_rule_filled_count > 0 else [])
                    + (["concerns"] if coverage_card.concerns_rule_filled_count > 0 else [])
                ),
            )
        progress_tracker.update_stage(
            script_id,
            "composing_coverage",
            "done",
            detail=f"速览{'已生成' if coverage_card else '降级'}",
        )

        # Wave C-3c (2026-05-31)：v4 投资决策评分是唯一主评分链路；v3 6 维 schema
        # 字段（decision / overall_score / scorecard / evaluation）已全部从 ReportPayload
        # 移除。v4 失败回退到 chain_status['scoring_v4']，verdict=None，前端展示「未评分」。
        progress_tracker.update_stage(
            script_id,
            "scoring_v4",
            "running",
            detail="HOOK / ARCHETYPE / PAYOFF / MONETIZATION / PRODUCIBILITY 五维 + 合规 gate",
        )
        v4_report_dict = await _run_scoring_v4(
            script_id=script_id,
            scenes=scenes,
            total_episodes=meta.total_episodes or 0,
            beat_sheet=beat_sheet,
            reward_events=reward_events or [],
            character_graph=character_graph,
            coverage_card=coverage_card,
            motivation_result=motivation_result,
            compliance=compliance,
            caller=caller,
            chain_status=chain_status,
        )
        if v4_report_dict is None:
            progress_tracker.update_stage(
                script_id, "scoring_v4", "done", detail="v4 评分降级（详见 chain_status）"
            )
        else:
            verdict_label = (v4_report_dict.get("verdict") or {}).get("label", "?")
            inv_score = (v4_report_dict.get("verdict") or {}).get("overall_score")
            inv_score_text = f"{inv_score:.2f}" if isinstance(inv_score, (int, float)) else "—"
            progress_tracker.update_stage(
                script_id,
                "scoring_v4",
                "done",
                detail=f"verdict={verdict_label} investment_score={inv_score_text}",
            )

        progress_tracker.update_stage(
            script_id, "building_payload", "running", detail="组装报告 payload"
        )
        # W1.6: run_id 已在流水线入口 INSERT (status='running')，此处复用。
        report_payload = _build_report_payload(
            meta=meta,
            compliance_payload=compliance.to_dict(),
            coverage_card=coverage_card,
            beat_sheet=beat_sheet,
            character_graph=character_graph,
            character_bios=bios,
            reward_events=reward_events,
            engine=engine,
            scenes_by_id={s.id: s for s in scenes},
            chain_status=chain_status,
            v4_report=v4_report_dict,
        )
        progress_tracker.update_stage(script_id, "building_payload", "done")

        progress_tracker.update_stage(
            script_id,
            "persisting",
            "running",
            detail="写入 reports / scoring_runs",
        )
        _persist_report(
            script_id=script_id,
            run_id=run_id,
            report_payload=report_payload,
            engine=engine,
        )
        _mark_script_status(script_id=script_id, status="ready", failure_reason=None, engine=engine)
        # W1.5: analysis 维度独立标 done。
        _mark_analysis_status(script_id=script_id, status="done", engine=engine)
        progress_tracker.update_stage(script_id, "persisting", "done", detail="report persisted")
        progress_tracker.finalize(script_id)
        return report_payload
    except Exception as exc:  # noqa: BLE001
        logger.exception("generate_report failed script_id=%s: %s", script_id, exc)
        # W1.6 (2026-05-31)：scoring_runs 标 failed + 写入 error message，让用户能在
        # script 列表看到「上一次分析失败」徽章并知道为什么。
        _mark_scoring_run_failed(
            run_id=run_id,
            error=f"{type(exc).__name__}: {exc}",
            engine=engine,
        )
        # W1.5 (2026-05-31)：分析失败不再把 scripts.status 翻成 failed，避免
        # 「上传成功（status=ready）→ 分析失败（status=failed）」语义错位。
        # ingest 阶段失败仍会通过 ingestion_service 写 status=failed；这里只标
        # failure_reason 让前端能在 dashboard 显示「分析失败，请重新诊断」。
        _mark_script_status(
            script_id=script_id,
            status="ready",  # ingest 仍然有效；分析重试入口走 reanalyze
            failure_reason=f"analysis_failed:{type(exc).__name__}: {str(exc)[:200]}",
            engine=engine,
        )
        # W1.5: analysis 维度独立标 failed。dashboard 列能显示「上次分析失败」。
        _mark_analysis_status(script_id=script_id, status="failed", engine=engine)
        progress_tracker.finalize(script_id, error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        # W1.7: 无论成功失败都要释放 advisory lock。
        if lock_acquired:
            _release_script_lock(script_id, engine=engine)


# ============================================================
# v4 verdict / 合规 → 报告级一句话摘要（C-3c：v3 verdict / overall_score / scorecard
# 字段已从 schema 移除，仅保留 decision_reason / summary 兼容字段）
# ============================================================


def _derive_decision_reason(
    v4_verdict: Optional[dict[str, Any]],
    compliance_payload: dict[str, Any],
) -> str:
    """v4 verdict / 合规结果 → 报告级一句话摘要（前端列表卡片 / summary 兼容字段）。

    优先级：
      1. compliance.tier == 'high_risk' → 合规一票否决文案
      2. v4 verdict.reason → 直接使用
      3. fallback：评分未完成提示
    """
    if str(compliance_payload.get("tier") or "").strip() == "high_risk":
        return "合规扫描发现高风险红线命中，建议先做内容整改。"
    if v4_verdict:
        reason = str(v4_verdict.get("reason") or "").strip()
        if reason:
            return reason
    return "v4 投资决策评分未完成，建议人工复核后再定。"


async def _run_scoring_v4(
    *,
    script_id: str,
    scenes: list[Any],
    total_episodes: int,
    beat_sheet: Optional[BeatSheet],
    reward_events: list[RewardEvent],
    character_graph: Optional[CharacterGraph],
    coverage_card: Optional[CoverageCard],
    motivation_result: Optional[MotivationResult],
    compliance: ComplianceResult,
    caller: LlmCaller,
    chain_status: _ChainStatusCollector,
) -> Optional[dict[str, Any]]:
    """跑 scoring v4 评分链。

    失败时返回 None 并把失败原因写到 chain_status['scoring_v4']，绝不重抛——
    v4 是与 v3 并行的"投资决策评分"分支，单点失败不能拖垮整份报告。

    成功时返回 ScoringReport.to_dict()，调用方塞进 ReportPayload.evaluation_v4 等字段。
    """
    try:
        scoring_ctx = ScoringContext(
            script_id=script_id,
            scenes=scenes,
            total_episodes=total_episodes,
            beat_sheet=beat_sheet,
            reward_events=reward_events,
            character_graph=character_graph,
            coverage_card=coverage_card,
            motivation_result=motivation_result,
            compliance=compliance,
            llm_caller=caller,
        )
        report = await score_script(scoring_ctx)
        report_dict = report.to_dict()
        # 成功也写一条 chain_status，便于 BI 跟踪每次运行的来源 / fallback 链
        fallback_reasons: list[str] = []
        for rec in report.chain_status_records:
            failed = rec.get("failed_signals") or []
            if failed:
                fallback_reasons.append(
                    f"{rec.get('dim_key')}:failed_signals={','.join(failed)}"
                )
        _record_chain_status(
            chain_status,
            "scoring_v4",
            status="degraded" if fallback_reasons else "ok",
            source="hybrid",
            fallback_reasons=fallback_reasons,
        )
        return report_dict
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "scoring v4 failed (non-fatal, v3 报告继续) script_id=%s err=%s",
            script_id,
            exc,
        )
        _record_chain_status(
            chain_status,
            "scoring_v4",
            status="failed",
            source="rule_fallback",
            fallback_reasons=[f"{type(exc).__name__}: {str(exc)[:200]}"],
        )
        return None


def _build_report_payload(
    *,
    meta: _ScriptMeta,
    compliance_payload: dict[str, Any],
    engine: Engine,
    coverage_card: Optional[CoverageCard] = None,
    beat_sheet: Optional[BeatSheet] = None,
    character_graph: Optional[CharacterGraph] = None,
    character_bios: Optional[list[CharacterBio]] = None,
    reward_events: Optional[list[RewardEvent]] = None,
    scenes_by_id: Optional[dict[str, Any]] = None,
    chain_status: Optional[_ChainStatusCollector] = None,
    v4_report: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Wave C-3c 报告 payload 组装。

    主链路：v4 投资决策评分（verdict / investment_score / evaluation_v4 / top_improvements）。

    C-3c 起 v3 6 维字段（decision / overall_score / scorecard / evaluation）全部移除，
    仅保留 `decision_reason` / `summary` 作为前端列表卡片的一句话摘要（从 v4 verdict 派生）。

    其它字段不变：
      - compliance / risk_flags             : 独立合规扫描
      - drama_tags / plot_units / characters
        / character_relationships           : 透传 ingest 期写入的标签
      - coverage_card / beat_sheet /
        character_graph                     : chain 输出
      - reward_events / evidence_refs /
        highlights / must_read_scene_ids    : 看点 + 证据锚点
      - pacing_curve                        : v4 emotion-arc
      - meta.chain_status                   : 报告级 provenance（W1.3）
    """
    reward_events = reward_events or []
    compliance_hits = compliance_payload.get("hits") or []
    must_read_scene_ids = _select_beat_anchor_scenes(beat_sheet, top_k=3)
    risk_flags = _derive_risk_flags(compliance_payload)
    evidence_refs_payload = _build_evidence_refs_minimal(reward_events, compliance_hits)
    highlights_payload = _build_highlights_minimal(
        reward_events, beat_sheet, evidence_refs_payload, scenes_by_id=scenes_by_id
    )

    v4_verdict = (v4_report or {}).get("verdict") or {}
    v4_overall_score = v4_verdict.get("overall_score") if v4_verdict else None
    decision_reason = _derive_decision_reason(v4_verdict, compliance_payload)

    return {
        "script_id": meta.script_id,
        "title": meta.title,
        # 一句话摘要（兼容字段，前端列表卡片用）。v4 verdict.reason 派生。
        "decision_reason": decision_reason,
        "summary": decision_reason,
        # 主链路 v4 字段
        "verdict": v4_verdict or None,
        "investment_score": v4_overall_score,
        "evaluation_v4": v4_report,
        "top_improvements": (v4_report or {}).get("top_improvements") or [],
        # 合规独立 gate
        "compliance": compliance_payload,
        "risk_flags": risk_flags,
        # 标签层 / 人物层（与 v4 评分无关，透传）
        "drama_tags": _load_drama_tags(script_id=meta.script_id, engine=engine),
        "plot_units": _load_plot_units(script_id=meta.script_id, engine=engine),
        "characters": _load_characters(script_id=meta.script_id, engine=engine),
        "character_relationships": _load_character_relationships(
            script_id=meta.script_id, engine=engine
        ),
        "character_bios": _bios_to_payload(character_bios),
        # 看点 / 证据锚点
        "must_read_scene_ids": must_read_scene_ids,
        "evidence_refs": evidence_refs_payload,
        "highlights": highlights_payload,
        # chain 输出（用于前端故事 / 人物 / 节奏 tab）
        "coverage_card": asdict(coverage_card) if coverage_card is not None else None,
        "beat_sheet": beat_sheet.to_dict() if beat_sheet is not None else None,
        "character_graph": character_graph.to_dict() if character_graph is not None else None,
        "pacing_curve": aggregate_pacing_curve(
            script_id=meta.script_id,
            reward_events=list(reward_events or []),
            beat_sheet=beat_sheet,
            engine=engine,
        ),
        # 报告级 provenance（W1.3）
        "meta": {
            "chain_status": dict(chain_status or {}),
            "overall_status": _aggregate_chain_status(chain_status or {}),
            "score_ver": _SCORE_VER,
        },
    }


def _aggregate_chain_status(chain_status: _ChainStatusCollector) -> str:
    """聚合各 chain status 到报告总 status。

    规则（与 chain_result.aggregate_overall_status 一致）：
      - 任一 chain=failed → overall=degraded（仍能出报告但有降级）
      - 任一 chain=degraded → overall=degraded
      - 否则 ok
    """
    if not chain_status:
        return "ok"
    statuses = [s.get("status") for s in chain_status.values()]
    if any(s == "failed" for s in statuses):
        return "degraded"
    if any(s == "degraded" for s in statuses):
        return "degraded"
    return "ok"


def _persist_report(
    *,
    script_id: str,
    run_id: str,
    report_payload: dict[str, Any],
    engine: Engine,
) -> None:
    """Wave C-3c 持久化：reports + scoring_runs（v4 主链路）。

    - scoring_runs.{verdict, investment_score} 由 Wave C-1 引入；C-3c 起为主信号源。
    - reports.decision_payload 列内容口径切换为 v4 verdict 完整 dict。
    - script_scores 6 维表已停止写入（alembic/11 会随 schema 一起 drop）。
    """
    now = datetime.utcnow()
    report_id = str(uuid.uuid4())
    v4_verdict = report_payload.get("verdict") or {}
    v4_verdict_label: Optional[str] = v4_verdict.get("label") if v4_verdict else None
    v4_investment_score = report_payload.get("investment_score")
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
    # Wave C-3a：6 维 insufficient_dimensions 字段已无意义（5 维 dealbreaker 走 v4 verdict）。
    # 仍保留 quality_flags 字段以便 BI 跟踪 v4 verdict 历史。
    quality_flags = {
        "v4_verdict": v4_verdict_label,
        "v4_investment_score": v4_investment_score,
    }

    with engine.begin() as conn:
        # W1.6 (2026-05-31)：scoring_runs 已在 generate_report 入口 INSERT (status='running')。
        # 这里改成 UPSERT（ON CONFLICT 由 input_hash / id PK 决定）：
        #   - 入口同 run_id 存在 → 走 UPDATE 分支，把 status 改为 'done'、补齐数据字段
        #   - 入口 INSERT 失败（DB 故障）→ 这里 INSERT 兜底，仍能写入完整记录
        # Wave C-1: 新增 verdict / investment_score 列写入，alembic/10 已建好 CHECK 约束。
        conn.execute(
            text(
                """
                INSERT INTO scriptlens.scoring_runs (
                    id, script_id, rubric_version, tag_set_ver, input_hash, genre_scope,
                    episode_count, plot_unit_count, quality_flags, model_versions,
                    prompt_versions, status, error, created_at,
                    verdict, investment_score
                )
                VALUES (
                    :id, :script_id, :rubric_version, :tag_set_ver, :input_hash, :genre_scope,
                    :episode_count, :plot_unit_count, CAST(:quality_flags AS jsonb),
                    CAST(:model_versions AS jsonb), CAST(:prompt_versions AS jsonb),
                    :status, :error, :created_at,
                    :verdict, :investment_score
                )
                ON CONFLICT (id) DO UPDATE SET
                    input_hash = EXCLUDED.input_hash,
                    quality_flags = EXCLUDED.quality_flags,
                    model_versions = EXCLUDED.model_versions,
                    prompt_versions = EXCLUDED.prompt_versions,
                    status = EXCLUDED.status,
                    verdict = EXCLUDED.verdict,
                    investment_score = EXCLUDED.investment_score,
                    error = NULL
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
                "verdict": v4_verdict_label,
                "investment_score": v4_investment_score,
            },
        )

        # Wave C-3a：script_scores 6 维行已停止写入（v4 5 维存在 reports.evaluation_v4 + scoring_runs）。
        # Wave C-3b 会删表，此处不再 INSERT 残留 6 维记录。

        # Wave C-3c：v3 ReportDecision 字段已删除，decision_payload 列改为存
        # v4 verdict 完整结构（label / overall_score / confidence / dimension_breakdown 等）。
        # alembic/11 不改列定义，仅做内容口径切换。
        decision_payload: dict[str, Any] = dict(v4_verdict) if v4_verdict else {
            "label": None,
            "score_ver": _SCORE_VER,
            "note": "v4 投资决策评分未生成（v4 失败或被跳过）",
        }
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
    """加载上一次报告里该维度的分数作为复评 baseline（前端对比改写前后用）。

    维度路由：
      - compliance       → reports.report_json.compliance
      - v4 5 维（hook/...） → reports.report_json.evaluation_v4.dimensions[]
      - 其它（含 v3 残留） → 返回 None baseline（Wave C-2 不再回退到 v3 scorecard，
                          v3 残留报告升级到 v4 前不提供基线，避免维度概念错配）
    """
    empty = {"score": None, "tier": None, "reason": None, "evidence_scene_ids": []}
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
        return empty
    payload = row.get("report_json")
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {}
    if not isinstance(payload, dict):
        return empty

    if dimension == "compliance":
        compliance = payload.get("compliance") if isinstance(payload.get("compliance"), dict) else {}
        return {
            "score": compliance.get("score"),
            "tier": compliance.get("tier") or compliance.get("level"),
            "reason": compliance.get("reason"),
            "evidence_scene_ids": list(compliance.get("evidence_ref_ids") or []),
        }

    # v4 5 维 → evaluation_v4.dimensions[]
    if dimension in _V4_DIMENSIONS:
        evaluation_v4 = payload.get("evaluation_v4")
        if not isinstance(evaluation_v4, dict):
            return empty
        dims = evaluation_v4.get("dimensions")
        if not isinstance(dims, list):
            return empty
        for item in dims:
            if not isinstance(item, dict):
                continue
            if str(item.get("key") or "").strip() != dimension:
                continue
            return {
                "score": item.get("score"),
                "tier": item.get("tier"),
                "reason": item.get("reason"),
                "evidence_scene_ids": list(item.get("evidence_ref_ids") or []),
            }
        return empty

    return empty


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


def _mark_analysis_status(
    *,
    script_id: str,
    status: str,  # running | done | failed
    engine: Engine,
) -> None:
    """W1.5：写 scripts.last_analysis_status，与 ingest 维度的 status 解耦。

    new column 由 09 migration 加；DB 未升级时 UPDATE 会失败但不抛——保留兼容。
    """
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE scriptlens.scripts
                    SET last_analysis_status = :status
                    WHERE id = :sid
                    """
                ),
                {"status": status, "sid": script_id},
            )
    except Exception:  # noqa: BLE001
        # DB 未跑 migration 09 时此处会报 undefined column；不要让流水线挂掉。
        logger.warning(
            "failed to mark analysis status (migration 09 未运行?) script_id=%s status=%s",
            script_id, status,
        )


# ============================================================
# W1.6 + W1.7：scoring_runs 状态机 + per-script advisory lock
# ============================================================


# advisory lock key: 用 zlib.crc32(script_id) 把 UUID 字符串映射到 bigint。
# PG advisory lock 是 session-scope（连接关闭自动释放）。
# 选 advisory 而非行级 lock 是因为：① 不阻塞 SELECT，② 跨多个 UPDATE/INSERT/DELETE
# 仍是同一把锁，③ 同 session 内可重入，④ 不需要 schema migration。
def _script_lock_key(script_id: str) -> int:
    """把 UUID 字符串映射到 bigint advisory lock key。

    用 PG 内置 hashtext()-equivalent（Python hash 会跨进程不一致）。
    这里用 zlib.crc32 保证跨进程一致性；命名空间 fixed = 8101（"sl" + 报告链）。
    """
    import zlib
    return (8101 << 32) | (zlib.crc32(script_id.encode("utf-8")) & 0xFFFF_FFFF)


# advisory lock 是 session-scope；必须用一个**长 connection** 持锁直到 generate_report
# 结束。这里把 connection 实例放进 module 级 dict，key=script_id。
# 注意：此 dict 仅用于「记得退出时去哪个 connection 上释放锁」，不是用作 lock 本身。
_active_lock_connections: Dict[str, Any] = {}


def _try_acquire_script_lock(script_id: str, *, engine: Engine) -> bool:
    """尝试拿到 per-script advisory lock。拿到 True；已被占 False。

    实现策略：
      - 开一个长 connection（**不进事务**，避免被外层逻辑误 commit/rollback）
      - SELECT pg_try_advisory_lock(key)
      - 把 connection 存入 _active_lock_connections，generate_report 结束时再释放
    """
    key = _script_lock_key(script_id)
    if script_id in _active_lock_connections:
        # 同进程里已经有一份持锁，说明流水线 reentry → 拒绝
        return False
    try:
        conn = engine.connect()
        row = conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": key}
        ).fetchone()
        acquired = bool(row and row[0])
        if acquired:
            _active_lock_connections[script_id] = conn
        else:
            conn.close()
        return acquired
    except Exception:  # noqa: BLE001
        logger.exception("failed to acquire advisory lock script_id=%s", script_id)
        return False


def _release_script_lock(script_id: str, *, engine: Engine) -> None:
    key = _script_lock_key(script_id)
    conn = _active_lock_connections.pop(script_id, None)
    if conn is None:
        return
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    except Exception:  # noqa: BLE001
        logger.exception("failed to release advisory lock script_id=%s", script_id)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _insert_scoring_run_running(
    *,
    run_id: str,
    script_id: str,
    engine: Engine,
) -> None:
    """W1.6：流水线入口立刻插一行 status='running'，让 dashboard 立刻能看到任务在跑。

    最终成功会被 _persist_report 内的 INSERT 改写（同 run_id 唯一 → 走 upsert）。
    失败会被 _mark_scoring_run_failed 更新。
    """
    now = datetime.utcnow()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO scriptlens.scoring_runs (
                        id, script_id, rubric_version, tag_set_ver, input_hash,
                        genre_scope, episode_count, plot_unit_count,
                        quality_flags, model_versions, prompt_versions,
                        status, error, created_at
                    )
                    VALUES (
                        :id, :script_id, :rubric_version, :tag_set_ver, :input_hash,
                        :genre_scope, 0, 0,
                        CAST('{}' AS jsonb), CAST('{}' AS jsonb), CAST('{}' AS jsonb),
                        'running', NULL, :created_at
                    )
                    ON CONFLICT (id) DO NOTHING
                    """
                ),
                {
                    "id": run_id,
                    "script_id": script_id,
                    "rubric_version": _SCORE_VER,
                    "tag_set_ver": _TAG_SET_VER_NONE,
                    "input_hash": "pending",
                    "genre_scope": "default",
                    "created_at": now,
                },
            )
    except Exception:  # noqa: BLE001
        # 写不进 scoring_runs 不应阻塞流水线（DB 故障下 LLM 任务仍可跑完出报告）。
        logger.exception(
            "failed to insert running scoring_run run_id=%s script_id=%s",
            run_id, script_id,
        )


def _mark_scoring_run_failed(
    *,
    run_id: str,
    error: str,
    engine: Engine,
) -> None:
    """W1.6：generate_report 失败时把对应 scoring_run 标 failed + error。"""
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE scriptlens.scoring_runs
                    SET status = 'failed',
                        error = :err
                    WHERE id = :rid
                    """
                ),
                {"rid": run_id, "err": error[:1000]},
            )
    except Exception:  # noqa: BLE001
        logger.exception("failed to mark scoring_run failed run_id=%s", run_id)
