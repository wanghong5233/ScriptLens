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

from service.score_registry import RubricConfig, get_tier_cuts

TIER_ORDER = ("insufficient", "poor", "weak", "good", "excellent")


@dataclass
class TierResult:
    tier: str
    confidence: str
    cuts: dict[str, float]


def _confidence_from_sample(sample_size: int | None) -> str:
    if sample_size is None:
        return "medium"
    if sample_size >= 100:
        return "high"
    if sample_size >= 30:
        return "medium"
    return "low"


def resolve_tier(
    rubric: RubricConfig,
    *,
    dimension: str,
    score: float | None,
    genre_scope: str,
    sample_size: int | None = None,
) -> TierResult:
    if score is None:
        return TierResult(
            tier="insufficient",
            confidence="low",
            cuts={"p25": 4.0, "p50": 6.0, "p75": 8.0},
        )

    try:
        scoped_cuts = get_tier_cuts(rubric.rubric_id, genre_scope)
        default_cuts = get_tier_cuts(rubric.rubric_id, "default")
    except Exception:  # noqa: BLE001 - fallback for synthetic/in-memory rubric fixtures
        scoped_cuts = dict(rubric.tier_cuts.get(genre_scope) or {})
        default_cuts = dict(rubric.tier_cuts.get("default") or {})
    cuts = scoped_cuts.get(dimension) or default_cuts.get(dimension) or {}
    p25 = float(cuts.get("p25", 4.0))
    p50 = float(cuts.get("p50", 6.0))
    p75 = float(cuts.get("p75", 8.0))

    if score >= p75:
        tier = "excellent"
    elif score >= p50:
        tier = "good"
    elif score >= p25:
        tier = "weak"
    else:
        tier = "poor"
    return TierResult(
        tier=tier,
        confidence=_confidence_from_sample(sample_size),
        cuts={"p25": p25, "p50": p50, "p75": p75},
    )


def resolve_industry_proxy_tier(
    *,
    value: float | None,
    cuts: dict[str, float] | None = None,
) -> str:
    if value is None:
        return "insufficient"
    use_cuts = cuts or {"p25": 4.0, "p50": 6.0, "p75": 8.0}
    if value >= float(use_cuts.get("p75", 8.0)):
        return "excellent"
    if value >= float(use_cuts.get("p50", 6.0)):
        return "good"
    if value >= float(use_cuts.get("p25", 4.0)):
        return "weak"
    return "poor"
