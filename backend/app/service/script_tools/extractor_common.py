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

from service.script_tools.v0_extractor_common import (
    PlotUnitContext,
    load_plot_unit_context,
    load_script_text,
    persist_episode_tags,
    persist_plot_unit_tags,
    persist_script_tags,
    render_prompt,
    resolve_plot_unit_id,
    resolve_script_id,
    stable_choice,
)
from service.script_tools.v1_extractor_common import (
    CharacterContext,
    EpisodeContext,
    RelationshipContext,
    load_character_context,
    load_episode_context,
    load_relationship_context,
)

__all__ = [
    "PlotUnitContext",
    "EpisodeContext",
    "CharacterContext",
    "RelationshipContext",
    "render_prompt",
    "stable_choice",
    "resolve_script_id",
    "resolve_plot_unit_id",
    "load_plot_unit_context",
    "load_script_text",
    "load_episode_context",
    "load_character_context",
    "load_relationship_context",
    "persist_script_tags",
    "persist_plot_unit_tags",
    "persist_episode_tags",
]
