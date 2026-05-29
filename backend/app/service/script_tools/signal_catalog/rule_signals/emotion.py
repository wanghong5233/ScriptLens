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

import math

from service.script_tools.signal_catalog import SignalContext, SignalValue, register_signal


def _score_from_thresholds(value: float, thresholds: tuple[float, float, float]) -> float:
    high, medium, low = thresholds
    if value >= high:
        return 10.0
    if value >= medium:
        return 8.0
    if value >= low:
        return 6.0
    return 3.0


@register_signal("payoff_density", scope="script", source="rule", primary_dim="emotion")
def compute_payoff_density(ctx: SignalContext) -> SignalValue:
    payoffs = [value for value in ctx.plot_values("payoff_type") if value != "none"]
    density = len(payoffs) / max(ctx.episode_count, 1)
    score = _score_from_thresholds(density, (1.5, 0.8, 0.3))
    return SignalValue(
        key="payoff_density",
        value={"density": round(density, 4), "payoff_count": len(payoffs)},
        score=score,
        source="rule",
        confidence=0.85,
    )


@register_signal("emotional_driver_distribution", scope="script", source="rule", primary_dim="emotion")
def compute_emotional_driver_distribution(ctx: SignalContext) -> SignalValue:
    drivers = [value for value in ctx.plot_values("emotional_driver") if value != "none"]
    if not drivers:
        return SignalValue(
            key="emotional_driver_distribution",
            value={"distribution": {}},
            score=3.0,
            source="rule",
            confidence=0.4,
        )
    counts: dict[str, int] = {}
    for value in drivers:
        counts[value] = counts.get(value, 0) + 1
    max_ratio = max(counts.values()) / len(drivers)
    score = round(max(2.0, 10.0 - max_ratio * 5.0), 2)
    return SignalValue(
        key="emotional_driver_distribution",
        value={
            "distribution": {key: round(value / len(drivers), 4) for key, value in sorted(counts.items())}
        },
        score=score,
        source="rule",
        confidence=0.7,
    )


@register_signal("emotional_variety", scope="script", source="rule", primary_dim="emotion")
def compute_emotional_variety(ctx: SignalContext) -> SignalValue:
    drivers = [value for value in ctx.plot_values("emotional_driver") if value != "none"]
    if not drivers:
        return SignalValue(
            key="emotional_variety",
            value={"entropy": 0.0},
            score=2.0,
            source="rule",
            confidence=0.3,
        )
    counts: dict[str, int] = {}
    for value in drivers:
        counts[value] = counts.get(value, 0) + 1
    total = len(drivers)
    entropy = 0.0
    for count in counts.values():
        p = count / total
        entropy -= p * math.log(max(p, 1e-9), 2)
    max_entropy = math.log(max(len(counts), 1), 2)
    normalized = entropy / max(max_entropy, 1e-9)
    score = round(min(10.0, max(1.0, normalized * 10.0)), 2)
    return SignalValue(
        key="emotional_variety",
        value={"entropy": round(entropy, 4), "normalized_entropy": round(normalized, 4)},
        score=score,
        source="rule",
        confidence=0.75,
    )


@register_signal("emotional_contrast", scope="script", source="rule", primary_dim="emotion")
def compute_emotional_contrast(ctx: SignalContext) -> SignalValue:
    positive = {"tenderness", "pity", "desire", "curiosity"}
    negative = {"humiliation", "jealousy", "regret", "anger", "fear"}
    drivers = [value for value in ctx.plot_values("emotional_driver") if value != "none"]
    has_positive = any(value in positive for value in drivers)
    has_negative = any(value in negative for value in drivers)
    if has_positive and has_negative:
        score = 9.0
    elif has_positive or has_negative:
        score = 6.0
    else:
        score = 3.0
    return SignalValue(
        key="emotional_contrast",
        value={"has_positive": has_positive, "has_negative": has_negative},
        score=score,
        source="rule",
        confidence=0.7,
    )


@register_signal("dry_streak_max", scope="script", source="rule", primary_dim="emotion")
def compute_dry_streak_max(ctx: SignalContext) -> SignalValue:
    max_streak = 0
    streak = 0
    for unit in ctx.plot_units:
        unit_id = str(unit.get("id") or "")
        payoff = ctx.unit_value(unit_id, "payoff_type", default="none")
        if payoff == "none":
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    if max_streak <= 2:
        score = 10.0
    elif max_streak <= 4:
        score = 8.0
    elif max_streak <= 6:
        score = 6.0
    else:
        score = 3.0
    return SignalValue(
        key="dry_streak_max",
        value={"max_streak_units": max_streak},
        score=score,
        source="rule",
        confidence=0.8,
    )


@register_signal("episode_end_hook_rate", scope="script", source="rule", primary_dim="emotion")
def compute_episode_end_hook_rate(ctx: SignalContext) -> SignalValue:
    hooks = [value for value in ctx.episode_values("episode_end_hook") if value and value != "none"]
    rate = len(hooks) / max(ctx.episode_count, 1)
    score = _score_from_thresholds(rate, (0.8, 0.5, 0.25))
    return SignalValue(
        key="episode_end_hook_rate",
        value={"hook_rate": round(rate, 4), "hook_count": len(hooks)},
        score=score,
        source="rule",
        confidence=0.8,
    )


@register_signal("paid_break_potential", scope="script", source="rule", primary_dim="emotion")
def compute_paid_break_potential(ctx: SignalContext) -> SignalValue:
    break_signals = [value for value in ctx.episode_values("paid_break_position") if value and value != "none"]
    if not break_signals:
        break_signals = [value for value in ctx.script_values("paid_break_pattern") if value and value != "no_break"]
    rate = len(break_signals) / max(ctx.episode_count, 1)
    score = _score_from_thresholds(rate, (0.8, 0.4, 0.15))
    return SignalValue(
        key="paid_break_potential",
        value={"break_rate": round(rate, 4), "signal_count": len(break_signals)},
        score=score,
        source="rule",
        confidence=0.7,
    )
