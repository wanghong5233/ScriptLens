"""scoring v4 聚合：gated multiplicative + weighted sum 混合。

设计原则：
- 所有阈值、verdict 文案、合规 veto tier、tier 切点全部从 RubricConfig 读取
- 本文件**禁止出现裸阈值字面量**（除 0/1 / 索引），是 zero-magic-number 策略关键守门员
- compute_verdict / compute_dimension_tier 是纯函数，便于单测覆盖所有 gate 分支
"""

from __future__ import annotations

from typing import Optional

from service.scoring.framework import (
    ConfidenceLabel,
    DimensionScore,
    ScoreVerdict,
    SignalResult,
    SignalStatus,
    TierLabel,
    VerdictLabel,
)
from service.scoring.rubric_loader import (
    AggregationConfig,
    DimensionTierCutsConfig,
    RubricConfig,
    SignalConfig,
)


# ============================================================
# Signal → score 落档
# ============================================================


def map_signal_raw_to_score(raw_value: float, signal_cfg: SignalConfig) -> tuple[float, str]:
    """根据 signal 的 tier_anchor + tier_scores，把 raw_value 映射到 0-10 分。

    返回 (score, tier_label_string)。
    所有阈值来自 signal_cfg.tier_anchor / signal_cfg.tier_scores，零硬编码。
    """
    anchor = signal_cfg.tier_anchor
    scores = signal_cfg.tier_scores

    if raw_value >= anchor.high:
        return scores.high, "high"
    if raw_value >= anchor.mid_high:
        return scores.mid_high, "mid_high"
    if raw_value >= anchor.mid_low:
        return scores.mid_low, "mid_low"
    return scores.low, "low"


# ============================================================
# Dimension 聚合：signal weighted sum + tier 落档
# ============================================================


def aggregate_dimension_score(
    signals: list[SignalResult],
    dim_cfg,  # DimensionConfig；circular import 用 forward ref
    tier_cuts: DimensionTierCutsConfig,
) -> tuple[float, TierLabel]:
    """对维度内 signals 加权聚合 → dimension score + tier。

    - 仅 status ∈ {COMPUTED, DEGRADED} 的 signal 参与归一化加权
    - status=FAILED 的 signal 跳过；权重重新归一化到仍有效的 signals
    - 全部 FAILED → 返回 (0.0, TierLabel.LOW)
    """
    sig_cfg_by_key = {s.key: s for s in dim_cfg.signals}

    weighted_sum = 0.0
    weight_sum = 0.0
    for sig in signals:
        if sig.status not in (SignalStatus.COMPUTED, SignalStatus.DEGRADED):
            continue
        cfg = sig_cfg_by_key.get(sig.key)
        if cfg is None:
            continue
        weighted_sum += sig.score * cfg.weight_in_dim
        weight_sum += cfg.weight_in_dim

    if weight_sum <= 0.0:
        return 0.0, TierLabel.LOW

    score = weighted_sum / weight_sum
    tier = _score_to_tier(score, tier_cuts)
    return score, tier


def _score_to_tier(score: float, cuts: DimensionTierCutsConfig) -> TierLabel:
    if score >= cuts.high:
        return TierLabel.HIGH
    if score >= cuts.mid_high:
        return TierLabel.MID_HIGH
    if score >= cuts.mid_low:
        return TierLabel.MID_LOW
    return TierLabel.LOW


# ============================================================
# Verdict 聚合：4-gate 决策
# ============================================================


def compute_verdict(
    dimension_scores: dict[str, DimensionScore],
    compliance_tier: Optional[str],
    rubric: RubricConfig,
    confidence: ConfidenceLabel = ConfidenceLabel.MEDIUM,
) -> ScoreVerdict:
    """4-gate 决策核心。

    Gate 1: compliance veto                     → not_recommended
    Gate 2: 任一 dealbreaker dim < 阈值         → not_recommended
    Gate 3: 加权 sum on remainder
    Gate 4: bucket into qualified/needs_polish/not_recommended

    所有阈值来自 rubric.aggregation；本函数不应出现任何裸数字。
    """
    agg = rubric.aggregation

    # ===== Gate 1: compliance veto =====
    if compliance_tier == rubric.compliance.veto_tier:
        return ScoreVerdict(
            label=VerdictLabel.NOT_RECOMMENDED,
            reason="合规审查不通过",
            overall_score=None,
            confidence=confidence,
            compliance_tier=compliance_tier,
            compliance_veto_triggered=True,
        )

    # ===== Gate 2: dealbreaker =====
    triggered: list[str] = []
    for d in agg.dealbreaker_dims:
        ds = dimension_scores.get(d)
        if ds is None:
            continue
        if ds.score < agg.dealbreaker_threshold:
            triggered.append(d)

    # ===== Gate 3: weighted sum =====
    overall = _weighted_sum(dimension_scores, rubric)

    if triggered:
        labels = "/".join(_dim_label(rubric, d) for d in triggered)
        return ScoreVerdict(
            label=VerdictLabel.NOT_RECOMMENDED,
            reason=f"{labels} 不达标（短板效应，无法被其他维度补偿）",
            overall_score=overall,
            confidence=confidence,
            compliance_tier=compliance_tier,
        )

    # ===== Gate 4: bucket =====
    cuts = agg.verdict_cuts
    floor = min(ds.score for ds in dimension_scores.values()) if dimension_scores else 0.0

    if overall >= cuts.qualified_overall_min and floor >= cuts.qualified_floor_min:
        return ScoreVerdict(
            label=VerdictLabel.QUALIFIED,
            reason="各维度均达到下一环节立项门槛",
            overall_score=overall,
            confidence=confidence,
            compliance_tier=compliance_tier,
        )
    if overall >= cuts.needs_polish_overall_min:
        return ScoreVerdict(
            label=VerdictLabel.NEEDS_POLISH,
            reason="整体接近门槛但有维度欠火，建议打磨后复评",
            overall_score=overall,
            confidence=confidence,
            compliance_tier=compliance_tier,
        )
    return ScoreVerdict(
        label=VerdictLabel.NOT_RECOMMENDED,
        reason="综合分低于推进门槛",
        overall_score=overall,
        confidence=confidence,
        compliance_tier=compliance_tier,
    )


def _weighted_sum(
    dimension_scores: dict[str, DimensionScore],
    rubric: RubricConfig,
) -> float:
    weighted_sum = 0.0
    weight_sum = 0.0
    for key, ds in dimension_scores.items():
        dim_cfg = rubric.dimensions.get(key)
        if dim_cfg is None:
            continue
        weighted_sum += ds.score * dim_cfg.weight
        weight_sum += dim_cfg.weight
    if weight_sum <= 0.0:
        return 0.0
    return weighted_sum / weight_sum


def _dim_label(rubric: RubricConfig, key: str) -> str:
    dim = rubric.dimensions.get(key)
    return dim.label if dim is not None else key


def lookup_verdict_display(rubric: RubricConfig, label: VerdictLabel, locale: str = "cn") -> str:
    """从 rubric verdicts 字段查 verdict 显示文案。前端 / 报告均用此函数。"""
    entry = rubric.aggregation.verdicts.get(label.value)
    if entry is None:
        return label.value
    if locale == "en":
        return entry.display_en
    return entry.display_cn


__all__ = [
    "AggregationConfig",
    "aggregate_dimension_score",
    "compute_verdict",
    "lookup_verdict_display",
    "map_signal_raw_to_score",
]
