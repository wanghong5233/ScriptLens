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

from dataclasses import dataclass

from service.score_registry import RubricConfig, get_genre_multiplier
from service.script_tools.dimension_aggregator import DimensionScore


@dataclass
class WeightedScore:
    overall_score: float | None
    normalized_weights: dict[str, float]
    genre: str


def infer_genre_scope(drama_tags: list[str]) -> str:
    lowered = [tag.lower() for tag in drama_tags]
    joined = " ".join(lowered)
    if any(key in joined for key in ("战神", "复仇", "逆袭")):
        return "战神_复仇_逆袭"
    if any(key in joined for key in ("甜宠", "爱情", "先婚后爱", "追妻")):
        return "甜宠_爱情"
    if any(key in joined for key in ("悬疑", "推理", "惊悚")):
        return "悬疑"
    return "default"


def apply_genre_weights(
    rubric: RubricConfig,
    dimension_scores: list[DimensionScore],
    *,
    genre_scope: str,
) -> WeightedScore:
    try:
        multiplier = get_genre_multiplier(rubric.rubric_id, genre_scope)
    except Exception:  # noqa: BLE001 - fallback for synthetic/in-memory rubric fixtures
        multiplier = dict(rubric.genre_multiplier.get(genre_scope) or rubric.genre_multiplier.get("default") or {})
    raw_weights: dict[str, float] = {}

    for dim in rubric.dimensions:
        base = rubric.base_weight.get(dim.id, 0.0)
        mult = multiplier.get(dim.id, 1.0)
        raw_weights[dim.id] = max(0.0, float(base) * float(mult))

    usable = [item for item in dimension_scores if item.score is not None and item.tier != "insufficient"]
    usable_weight = sum(raw_weights.get(item.dimension, 0.0) for item in usable)
    if usable_weight <= 0:
        return WeightedScore(overall_score=None, normalized_weights={}, genre=genre_scope)

    normalized = {
        item.dimension: raw_weights.get(item.dimension, 0.0) / usable_weight for item in usable
    }
    overall = sum((item.score or 0.0) * normalized.get(item.dimension, 0.0) for item in usable)
    return WeightedScore(
        overall_score=round(overall, 3),
        normalized_weights={k: round(v, 6) for k, v in normalized.items()},
        genre=genre_scope,
    )
