"""scoring v4 主链入口。

调用约定：
- script_report_service.generate_report 准备好上游 chain 输出后，注入 ScoringContext，
  调用 score_script(ctx, rubric_version) 拿到 ScoringReport
- rewrite_chain.score_one_dimension 调用 score_dimension(dim_key, ctx) 单维重评分
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from service.scoring.aggregator import compute_verdict
from service.scoring.confidence import compute_confidence
from service.scoring.dimensions import DIMENSION_FUNCS
from service.scoring.framework import (
    DimensionScore,
    ScoringContext,
    ScoringReport,
)
from service.scoring.improvement_planner import plan_improvements
from service.scoring.provenance import log_chain_record, make_record
from service.scoring.rubric_loader import (
    RubricConfig,
    assert_valid_v4_dimension,
    load_rubric,
)

logger = logging.getLogger(__name__)


async def score_script(
    ctx: ScoringContext,
    *,
    rubric_version: str = "v4-cn-2026-05-31",
    compliance_tier: Optional[str] = None,
) -> ScoringReport:
    """主入口：跑全部 5 维 + 聚合 verdict + 改进建议。"""
    rubric = load_rubric(rubric_version)

    # 并行 5 维
    tasks = []
    keys: list[str] = []
    for key, dim_cfg in rubric.dimensions.items():
        func = DIMENSION_FUNCS.get(key)
        if func is None:
            logger.error("scoring.main_chain unknown dim key=%s", key)
            continue
        tasks.append(func(ctx, dim_cfg, rubric.dimension_tier_cuts))
        keys.append(key)

    results = await asyncio.gather(*tasks, return_exceptions=True)

    dimension_scores: dict[str, DimensionScore] = {}
    chain_records: list[dict] = []
    for key, res in zip(keys, results):
        if isinstance(res, Exception):
            logger.exception(
                "scoring.main_chain dim=%s 抛异常 err=%s", key, res
            )
            # 抛异常视为整维 failed；构造空 DimensionScore 让 verdict 走 dealbreaker
            from service.scoring.framework import DimensionScore, TierLabel

            ds = DimensionScore(
                key=key,
                score=0.0,
                tier=TierLabel.LOW,
                reason=f"维度计算异常: {type(res).__name__}",
                signals=[],
            )
        else:
            ds = res
        dimension_scores[key] = ds
        record = make_record(ds)
        log_chain_record(record, ctx.script_id)
        chain_records.append(record.to_dict())

    # confidence
    confidence_label, coverage_ratio = compute_confidence(
        dimension_scores, rubric.confidence
    )

    # verdict
    actual_compliance_tier = compliance_tier
    if actual_compliance_tier is None and ctx.compliance is not None:
        actual_compliance_tier = getattr(ctx.compliance, "tier", None)

    verdict = compute_verdict(
        dimension_scores=dimension_scores,
        compliance_tier=actual_compliance_tier,
        rubric=rubric,
        confidence=confidence_label,
    )

    # mark dealbreaker on dimension scores (UI 用)
    for d in rubric.aggregation.dealbreaker_dims:
        ds = dimension_scores.get(d)
        if ds is None:
            continue
        if ds.score < rubric.aggregation.dealbreaker_threshold:
            ds.is_dealbreaker_triggered = True

    # improvements
    top_improvements = plan_improvements(
        dimension_scores, verdict, rubric, rubric.improvement_planner
    )

    return ScoringReport(
        verdict=verdict,
        dimensions=list(dimension_scores.values()),
        top_improvements=top_improvements,
        rubric_version=rubric.version,
        coverage_ratio=coverage_ratio,
        chain_status_records=chain_records,
    )


async def score_dimension(
    dim_key: str,
    ctx: ScoringContext,
    *,
    rubric_version: str = "v4-cn-2026-05-31",
) -> DimensionScore:
    """单维重评分入口（供 rewrite_chain 用）。

    旧 v3 6 维 key 在 rubric_loader.assert_valid_v4_dimension 处显式抛错。
    """
    assert_valid_v4_dimension(dim_key)
    rubric = load_rubric(rubric_version)
    dim_cfg = rubric.dimensions[dim_key]
    func = DIMENSION_FUNCS[dim_key]
    return await func(ctx, dim_cfg, rubric.dimension_tier_cuts)


def lookup_rubric(rubric_version: str = "v4-cn-2026-05-31") -> RubricConfig:
    return load_rubric(rubric_version)


__all__ = ["lookup_rubric", "score_dimension", "score_script"]
