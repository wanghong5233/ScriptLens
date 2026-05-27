from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from service.script_tools.dimension_aggregator import DimensionScore
from service.script_tools.genre_weights import WeightedScore


@dataclass
class DecisionResult:
    decision: str
    confidence: str
    one_sentence_reason: str
    payload: dict[str, Any]


_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def _min_confidence(values: list[str]) -> str:
    if not values:
        return "low"
    min_rank = min(_CONF_RANK.get(value, 0) for value in values)
    for label, rank in _CONF_RANK.items():
        if rank == min_rank:
            return label
    return "low"


def decide(
    dimension_scores: list[DimensionScore],
    weighted: WeightedScore,
    *,
    compliance: dict[str, Any] | None = None,
) -> DecisionResult:
    overall = weighted.overall_score
    poor_dims = [item.dimension for item in dimension_scores if item.tier in {"poor", "insufficient"}]
    core_dims = {"story", "character", "emotion"}
    weak_core = [item.dimension for item in dimension_scores if item.dimension in core_dims and item.tier in {"poor", "insufficient"}]

    compliance_state = (compliance or {}).get("status")
    if compliance_state == "blocked":
        decision = "not_recommended"
        reason = "内容合规存在阻断项，需先完成整改。"
    elif overall is None:
        decision = "insufficient_data"
        reason = "可用信号覆盖率不足，暂不输出最终推荐。"
    elif overall >= 7.5 and not weak_core:
        decision = "recommended"
        reason = "核心维度稳定且综合分高，可进入下一轮开发。"
    elif overall >= 6.0:
        decision = "conditional_recommend"
        reason = "有潜力但存在短板，建议按改写动作先修复再推进。"
    else:
        decision = "not_recommended"
        reason = "核心质量未达标，建议先做结构性重写。"

    confidence = _min_confidence([item.confidence for item in dimension_scores if item.tier != "insufficient"] or ["low"])
    payload = {
        "overall_score": overall,
        "genre_scope": weighted.genre,
        "normalized_weights": weighted.normalized_weights,
        "poor_dimensions": poor_dims,
        "weak_core_dimensions": weak_core,
        "compliance_status": compliance_state or "unknown",
        "dimensions": [item.to_dict() for item in dimension_scores],
    }
    return DecisionResult(decision=decision, confidence=confidence, one_sentence_reason=reason, payload=payload)
