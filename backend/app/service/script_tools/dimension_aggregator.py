# ============================================================
# DEPRECATED — release/v1-mvp (2026-05-29)
# ============================================================
#
# 本文件属于已废弃的「整剧抽情节打标签 → rubric/signal/aggregator
# 评分」流水线（Batch3 体系）。release/v1-mvp 已切回 self-contained
# 6 维规则评分，主流程入口：
#   - service/script_tools/dimension_scorer.py
#   - service/script_report_service.py（generate_report）
# 当前已不再调用本模块任何函数。
#
# 保留原因：避免 git history 大面积污染、便于必要时回收实现细节。
# 清理时机：下次 cleanup PR 统一删除（含本文件、其测试、CLI 入口
# 与 score_registry/rubric_sets/v3.yaml 等配套资产）。
#
# 不要在本文件内再做任何功能性修改。如需新评分能力，请扩展
# dimension_scorer.py。
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from service.score_registry import RubricConfig
from service.script_tools.signal_catalog import SignalValue


@dataclass
class DimensionScore:
    dimension: str
    score: float | None
    coverage_ratio: float
    confidence: str
    tier: str = "insufficient"
    primary_dimension: str | None = None
    signal_refs: list[dict[str, Any]] = field(default_factory=list)
    top_signals: list[dict[str, Any]] = field(default_factory=list)
    tier_cuts: dict[str, float] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "score": self.score,
            "tier": self.tier,
            "confidence": self.confidence,
            "coverage_ratio": self.coverage_ratio,
            "primary_dimension": self.primary_dimension,
            "signal_refs": self.signal_refs,
            "top_signals": self.top_signals,
            "tier_cuts": self.tier_cuts,
            "reason": self.reason,
        }


def _confidence_label(value: float) -> str:
    if value >= 0.75:
        return "high"
    if value >= 0.45:
        return "medium"
    return "low"


def _build_top_signals(signal_refs: list[dict[str, Any]], *, limit: int = 3) -> list[dict[str, Any]]:
    ranked: list[tuple[float, dict[str, Any]]] = []
    for ref in signal_refs:
        score = ref.get("score")
        weight = ref.get("weight_in_dim")
        if score is None or weight is None:
            continue
        try:
            score_f = float(score)
            weight_f = float(weight)
        except (TypeError, ValueError):
            continue
        contribution = score_f * weight_f
        ranked.append(
            (
                contribution,
                {
                    "signal_key": ref.get("signal_key"),
                    "value": ref.get("value"),
                    "score": score_f,
                    "weight_in_dim": weight_f,
                    "source": ref.get("source"),
                    "confidence": float(ref.get("confidence") or 0.0),
                    "contribution": round(contribution, 6),
                },
            )
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in ranked[:limit]]


def aggregate(
    rubric: RubricConfig,
    signal_values: dict[str, SignalValue],
    *,
    coverage_threshold: float = 0.5,
) -> list[DimensionScore]:
    out: list[DimensionScore] = []
    for dim in rubric.dimensions:
        total_weight = sum(signal.weight_in_dim for signal in dim.signals)
        weighted_sum = 0.0
        covered_weight = 0.0
        confidence_weighted = 0.0
        signal_refs: list[dict[str, Any]] = []
        primary_dimension = None

        for signal in dim.signals:
            resolved = signal_values.get(signal.id)
            signal_score = None
            if resolved is not None and resolved.score is not None:
                signal_score = float(resolved.score)
                signal_score = max(0.0, min(10.0, signal_score))
                weighted_sum += signal_score * signal.weight_in_dim
                covered_weight += signal.weight_in_dim
                confidence_weighted += float(resolved.confidence or 0.0) * signal.weight_in_dim
            signal_refs.append(
                {
                    "signal_key": signal.id,
                    "score": signal_score,
                    "value": resolved.value if resolved is not None else None,
                    "source": resolved.source if resolved is not None else signal.source,
                    "confidence": float(resolved.confidence or 0.0) if resolved is not None else 0.0,
                    "weight_in_dim": signal.weight_in_dim,
                    "primary_dimension": resolved.primary_dimension if resolved is not None else None,
                    "evidence_refs": list(resolved.evidence_refs or []) if resolved is not None else [],
                }
            )
            if signal.primary and primary_dimension is None:
                primary_dimension = dim.id

        coverage_ratio = covered_weight / max(total_weight, 1e-9)
        top_signals = _build_top_signals(signal_refs)
        if coverage_ratio < coverage_threshold:
            out.append(
                DimensionScore(
                    dimension=dim.id,
                    score=None,
                    tier="insufficient",
                    confidence="low",
                    coverage_ratio=round(coverage_ratio, 4),
                    primary_dimension=primary_dimension or dim.id,
                    signal_refs=signal_refs,
                    top_signals=top_signals,
                    reason=f"coverage_ratio={coverage_ratio:.2f} below threshold {coverage_threshold:.2f}",
                )
            )
            continue

        score = weighted_sum / max(covered_weight, 1e-9)
        conf_float = confidence_weighted / max(covered_weight, 1e-9)
        out.append(
            DimensionScore(
                dimension=dim.id,
                score=round(score, 3),
                tier="insufficient",
                confidence=_confidence_label(conf_float),
                coverage_ratio=round(coverage_ratio, 4),
                primary_dimension=primary_dimension or dim.id,
                signal_refs=signal_refs,
                top_signals=top_signals,
                reason=f"{dim.id} weighted aggregation over {len(dim.signals)} signals",
            )
        )
    return out
