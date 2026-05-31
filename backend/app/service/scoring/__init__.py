"""短剧投资决策评分系统 v4。

第一性原理：判断剧本是否**达标进入下一环节**（人物 / 分镜 / AI 视频生成 / 后期 / 投放），
而不是评"剧本工艺"或"投不投放"。

详细设计文档：docs/2026-05-31-投资决策评分框架-v4.md

公共入口：
- score_script(ctx, rubric_version=...) -> ScoringReport
- score_dimension(dim_key, ctx, cfg) -> DimensionScore     # 用于 rewrite_chain 单维重评分

不要直接导入 scoring.dimensions.* / scoring.signals.* —— 改用本文件暴露的入口。
"""

from __future__ import annotations

from service.scoring.framework import (
    DimensionScore,
    ScoreVerdict,
    ScoringContext,
    ScoringReport,
    SignalResult,
    SignalStatus,
)
from service.scoring.main_chain import (
    lookup_rubric,
    score_dimension,
    score_script,
)
from service.scoring.rubric_loader import (
    AggregationConfig,
    ComplianceConfig,
    ConfidenceConfig,
    DimensionConfig,
    ImprovementPlannerConfig,
    RubricConfig,
    RubricLegacyDimensionError,
    RubricSchemaError,
    SignalConfig,
    TruncationConfig,
    assert_valid_v4_dimension,
    load_rubric,
)

__all__ = [
    "AggregationConfig",
    "ComplianceConfig",
    "ConfidenceConfig",
    "DimensionConfig",
    "DimensionScore",
    "ImprovementPlannerConfig",
    "RubricConfig",
    "RubricLegacyDimensionError",
    "RubricSchemaError",
    "ScoreVerdict",
    "ScoringContext",
    "ScoringReport",
    "SignalConfig",
    "SignalResult",
    "SignalStatus",
    "TruncationConfig",
    "assert_valid_v4_dimension",
    "load_rubric",
    "lookup_rubric",
    "score_dimension",
    "score_script",
]
