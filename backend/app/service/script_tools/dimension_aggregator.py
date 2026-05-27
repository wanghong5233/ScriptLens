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
            "reason": self.reason,
        }


def _confidence_label(value: float) -> str:
    if value >= 0.75:
        return "high"
    if value >= 0.45:
        return "medium"
    return "low"


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
                reason=f"{dim.id} weighted aggregation over {len(dim.signals)} signals",
            )
        )
    return out
