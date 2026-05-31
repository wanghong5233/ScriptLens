"""Confidence 计算：基于 signal 覆盖率 + LLM judge 失败率。

confidence 给前端看的不是"我对剧本质量多自信"，而是"这次评分跑得多稳"。
覆盖率低 / LLM judge 失败多 → confidence 降级。

所有阈值来自 ConfidenceConfig（YAML），零硬编码。
"""

from __future__ import annotations

from service.scoring.framework import (
    ConfidenceLabel,
    DimensionScore,
    SignalResult,
    SignalSource,
    SignalStatus,
)
from service.scoring.rubric_loader import ConfidenceConfig


def compute_confidence(
    dimension_scores: dict[str, DimensionScore],
    cfg: ConfidenceConfig,
) -> tuple[ConfidenceLabel, float]:
    """返回 (label, coverage_ratio)。

    coverage_ratio = 计算成功的 signal / 总 signal
    （COMPUTED + DEGRADED 视为"算出"；FAILED 视为没算出；NOT_APPLICABLE 不计入分母）
    """
    total = 0
    computed = 0
    llm_judge_failed = 0

    for ds in dimension_scores.values():
        for sig in ds.signals:
            if sig.status == SignalStatus.NOT_APPLICABLE:
                continue
            total += 1
            if sig.status in (SignalStatus.COMPUTED, SignalStatus.DEGRADED):
                computed += 1
            if sig.status == SignalStatus.FAILED and sig.source == SignalSource.LLM_JUDGE:
                llm_judge_failed += 1

    if total == 0:
        return ConfidenceLabel.LOW, 0.0

    coverage = computed / total

    if (
        coverage >= cfg.high_min_coverage
        and llm_judge_failed <= cfg.max_llm_judge_failures_for_high
    ):
        return ConfidenceLabel.HIGH, coverage
    if (
        coverage >= cfg.medium_min_coverage
        and llm_judge_failed <= cfg.max_llm_judge_failures_for_medium
    ):
        return ConfidenceLabel.MEDIUM, coverage
    return ConfidenceLabel.LOW, coverage


__all__ = ["compute_confidence"]
