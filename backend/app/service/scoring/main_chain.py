"""scoring v4 主链入口（LLM-first，2026-05-31 翻盘）。

调用约定：
- script_report_service.generate_report 准备好上游 chain 输出后，注入 ScoringContext，
  调用 score_script(ctx, rubric_version) 拿到 ScoringReport
- rewrite_chain.score_one_dimension 调用 score_dimension(dim_key, ctx) 单维重评分

2026-05-31 翻盘后：
- 5 维评分**统一由 LLM judge 执行**（见 llm_dimension_judge.score_dimension_via_llm）
- rubric yaml 的 source 字段（rule/hybrid/llm_judge）已不再被调度逻辑消费，但
  保留作为 spec 文档与历史兼容（下次 PR 清理）
- 老的 dimensions/{hook,archetype,payoff,monetization,producibility}.py 仍存在
  但已不被本主链调用，下个 PR 一并清理
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from service.scoring.aggregator import compute_verdict
from service.scoring.confidence import compute_confidence
from service.scoring.framework import (
    DimensionScore,
    ScoringContext,
    ScoringReport,
)
from service.scoring.improvement_planner import plan_improvements
from service.scoring.llm_dimension_judge import score_dimension_via_llm
from service.scoring.provenance import log_chain_record, make_record
from service.scoring.rubric_loader import (
    RubricConfig,
    assert_valid_v4_dimension,
    load_rubric,
)
from service.scoring.script_summary import build_script_summary

logger = logging.getLogger(__name__)


async def score_script(
    ctx: ScoringContext,
    *,
    rubric_version: str = "v4-cn-2026-05-31",
    compliance_tier: Optional[str] = None,
) -> ScoringReport:
    """主入口：跑全部 5 维 + 聚合 verdict + 改进建议。

    2026-05-31 翻盘：5 维统一走 LLM judge，rule signal 计算已废弃。
    script_summary 一次性算好供 5 维共享，避免重复拼装。
    """
    rubric = load_rubric(rubric_version)

    script_summary = build_script_summary(ctx)

    tasks = []
    keys: list[str] = []
    for key, dim_cfg in rubric.dimensions.items():
        tasks.append(
            score_dimension_via_llm(
                dim_key=key,
                ctx=ctx,
                dim_cfg=dim_cfg,
                tier_cuts=rubric.dimension_tier_cuts,
                script_summary=script_summary,
            )
        )
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
    2026-05-31 翻盘后同样走 LLM judge。
    """
    assert_valid_v4_dimension(dim_key)
    rubric = load_rubric(rubric_version)
    dim_cfg = rubric.dimensions[dim_key]
    script_summary = build_script_summary(ctx)
    return await score_dimension_via_llm(
        dim_key=dim_key,
        ctx=ctx,
        dim_cfg=dim_cfg,
        tier_cuts=rubric.dimension_tier_cuts,
        script_summary=script_summary,
    )


def lookup_rubric(rubric_version: str = "v4-cn-2026-05-31") -> RubricConfig:
    return load_rubric(rubric_version)


__all__ = ["lookup_rubric", "score_dimension", "score_script"]
