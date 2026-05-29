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

"""Score registry loader/validator for scoring rubrics."""

from service.score_registry.compat_check import (
    CompatIssue,
    CompatResult,
    check_rubric_compatibility,
    compare_rubrics,
)
from service.score_registry.loader import (
    DimensionConfig,
    LlmBundleConfig,
    RubricConfig,
    SignalConfig,
    get_genre_multiplier,
    get_tier_cuts,
    list_llm_bundles,
    list_signals,
    load_prompt_by_bundle,
    load_rubric,
)

__all__ = [
    "SignalConfig",
    "DimensionConfig",
    "LlmBundleConfig",
    "RubricConfig",
    "load_rubric",
    "load_prompt_by_bundle",
    "list_signals",
    "list_llm_bundles",
    "get_genre_multiplier",
    "get_tier_cuts",
    "CompatIssue",
    "CompatResult",
    "compare_rubrics",
    "check_rubric_compatibility",
]
