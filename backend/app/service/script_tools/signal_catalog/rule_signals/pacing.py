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

import statistics

from service.script_tools.signal_catalog import SignalContext, SignalValue, register_signal


def narrative_intensity(ctx: SignalContext, plot_unit_id: str) -> int:
    """Lock the documented intensity formula for pacing v3."""
    plot_hook = ctx.unit_value(plot_unit_id, "plot_hook", default="none")
    conflict_type = ctx.unit_value(plot_unit_id, "conflict_type", default="none")
    payoff_type = ctx.unit_value(plot_unit_id, "payoff_type", default="none")
    emotional_driver = ctx.unit_value(plot_unit_id, "emotional_driver", default="none")
    intensity = (
        int(plot_hook != "none") * 2
        + int(conflict_type != "none") * 2
        + int(payoff_type != "none") * 3
        + int(emotional_driver != "none") * 1
    )
    return min(intensity, 8)


def _intensity_series(ctx: SignalContext) -> list[int]:
    out: list[int] = []
    for unit in ctx.plot_units:
        unit_id = str(unit.get("id") or "").strip()
        if not unit_id:
            continue
        out.append(narrative_intensity(ctx, unit_id))
    return out


def _score_from_range(value: float, *, low: float, mid: float, high: float) -> float:
    if value >= high:
        return 10.0
    if value >= mid:
        return 8.0
    if value >= low:
        return 6.0
    return 3.0


@register_signal("opening_speed", scope="script", source="rule", primary_dim="pacing")
def compute_opening_speed(ctx: SignalContext) -> SignalValue:
    series = _intensity_series(ctx)
    if not series:
        return SignalValue(
            key="opening_speed",
            value={"opening_index": None},
            score=2.0,
            source="rule",
            confidence=0.3,
        )
    first_idx = 1
    for idx, value in enumerate(series, start=1):
        if value >= 3:
            first_idx = idx
            break
    total_units = len(series)
    opening_ratio = first_idx / max(total_units, 1)
    if opening_ratio <= 0.05:
        score = 10.0
    elif opening_ratio <= 0.12:
        score = 8.0
    elif opening_ratio <= 0.20:
        score = 6.0
    else:
        score = 3.0
    return SignalValue(
        key="opening_speed",
        value={"opening_index": first_idx, "total_units": total_units},
        score=score,
        source="rule",
        confidence=0.85,
    )


@register_signal("plot_unit_density", scope="script", source="rule", primary_dim="pacing")
def compute_plot_unit_density(ctx: SignalContext) -> SignalValue:
    density = ctx.plot_unit_count / max(ctx.episode_count, 1)
    score = _score_from_range(density, low=1.5, mid=3.0, high=5.0)
    return SignalValue(
        key="plot_unit_density",
        value={"units_per_episode": round(density, 4)},
        score=score,
        source="rule",
        confidence=0.9,
    )


@register_signal("narrative_intensity_distribution", scope="script", source="rule", primary_dim="pacing")
def compute_narrative_intensity_distribution(ctx: SignalContext) -> SignalValue:
    series = _intensity_series(ctx)
    if not series:
        return SignalValue(
            key="narrative_intensity_distribution",
            value={"intensity_avg": 0.0},
            score=2.0,
            source="rule",
            confidence=0.3,
        )
    avg = statistics.fmean(series)
    score = _score_from_range(avg, low=2.0, mid=3.0, high=4.5)
    return SignalValue(
        key="narrative_intensity_distribution",
        value={"intensity_avg": round(avg, 4), "intensity_max": max(series)},
        score=score,
        source="rule",
        confidence=0.85,
    )


@register_signal("intensity_cv", scope="script", source="rule", primary_dim="pacing")
def compute_intensity_cv(ctx: SignalContext) -> SignalValue:
    series = _intensity_series(ctx)
    if not series:
        return SignalValue(
            key="intensity_cv",
            value={"cv": 0.0},
            score=2.0,
            source="rule",
            confidence=0.3,
        )
    mean = statistics.fmean(series)
    if mean <= 0:
        cv = 0.0
    else:
        cv = statistics.pstdev(series) / mean
    if cv <= 0.6:
        score = 10.0
    elif cv <= 0.8:
        score = 8.0
    elif cv <= 1.0:
        score = 6.0
    else:
        score = 3.0
    return SignalValue(
        key="intensity_cv",
        value={"cv": round(cv, 4)},
        score=score,
        source="rule",
        confidence=0.8,
    )


@register_signal("middle_sag", scope="script", source="rule", primary_dim="pacing")
def compute_middle_sag(ctx: SignalContext) -> SignalValue:
    series = _intensity_series(ctx)
    if len(series) < 3:
        return SignalValue(
            key="middle_sag",
            value={"middle_ratio": 0.0},
            score=4.0,
            source="rule",
            confidence=0.4,
        )
    total_mean = statistics.fmean(series)
    n = len(series)
    lo = n // 3
    hi = max(lo + 1, 2 * n // 3)
    middle = series[lo:hi]
    middle_mean = statistics.fmean(middle) if middle else total_mean
    ratio = middle_mean / max(total_mean, 1e-9)
    if ratio >= 0.9:
        score = 10.0
    elif ratio >= 0.8:
        score = 8.0
    elif ratio >= 0.7:
        score = 6.0
    else:
        score = 3.0
    return SignalValue(
        key="middle_sag",
        value={"middle_ratio": round(ratio, 4)},
        score=score,
        source="rule",
        confidence=0.75,
    )


@register_signal("downtime_penalty", scope="script", source="rule", primary_dim="pacing")
def compute_downtime_penalty(ctx: SignalContext) -> SignalValue:
    series = _intensity_series(ctx)
    longest = 0
    current = 0
    for value in series:
        if value <= 2:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    if longest <= 1:
        score = 10.0
    elif longest <= 3:
        score = 7.0
    elif longest <= 5:
        score = 5.0
    else:
        score = 2.0
    return SignalValue(
        key="downtime_penalty",
        value={"longest_low_intensity_streak": longest},
        score=score,
        source="rule",
        confidence=0.75,
    )


@register_signal("dialogue_action_ratio", scope="script", source="rule", primary_dim="pacing")
def compute_dialogue_action_ratio(ctx: SignalContext) -> SignalValue:
    values = [value for value in ctx.plot_values("dialogue_density") if value]
    if not values:
        return SignalValue(
            key="dialogue_action_ratio",
            value={"dense_ratio": 0.0},
            score=5.0,
            source="rule",
            confidence=0.3,
        )
    dense_ratio = sum(1 for value in values if value == "dense") / len(values)
    sparse_ratio = sum(1 for value in values if value == "sparse") / len(values)
    # pacing favors balanced action/dialogue; too dense or too sparse both hurt.
    balance_penalty = abs(dense_ratio - sparse_ratio)
    score = round(max(2.0, 10.0 - balance_penalty * 10.0), 2)
    return SignalValue(
        key="dialogue_action_ratio",
        value={"dense_ratio": round(dense_ratio, 4), "sparse_ratio": round(sparse_ratio, 4)},
        score=score,
        source="rule",
        confidence=0.65,
    )


@register_signal("beats_per_episode", scope="script", source="rule", primary_dim="pacing")
def compute_beats_per_episode(ctx: SignalContext) -> SignalValue:
    beats = [value for value in ctx.plot_values("story_stage") if value and value != "none"]
    beats_per_ep = len(beats) / max(ctx.episode_count, 1)
    score = _score_from_range(beats_per_ep, low=1.0, mid=2.0, high=3.0)
    return SignalValue(
        key="beats_per_episode",
        value={"beats_per_episode": round(beats_per_ep, 4)},
        score=score,
        source="rule",
        confidence=0.8,
    )


@register_signal("effective_narrative_rate", scope="script", source="rule", primary_dim="pacing")
def compute_effective_narrative_rate(ctx: SignalContext) -> SignalValue:
    series = _intensity_series(ctx)
    if not series:
        return SignalValue(
            key="effective_narrative_rate",
            value={"effective_ratio": 0.0},
            score=2.0,
            source="rule",
            confidence=0.3,
        )
    effective_ratio = sum(1 for value in series if value >= 3) / len(series)
    score = _score_from_range(effective_ratio, low=0.45, mid=0.65, high=0.8)
    return SignalValue(
        key="effective_narrative_rate",
        value={"effective_ratio": round(effective_ratio, 4)},
        score=score,
        source="rule",
        confidence=0.8,
    )


@register_signal("scene_length_distribution", scope="script", source="rule", primary_dim="pacing")
def compute_scene_length_distribution(ctx: SignalContext) -> SignalValue:
    lengths = [len(str(scene.get("text") or "")) for scene in ctx.scenes if str(scene.get("text") or "").strip()]
    if not lengths:
        return SignalValue(
            key="scene_length_distribution",
            value={"cv": 0.0},
            score=3.0,
            source="rule",
            confidence=0.2,
        )
    mean = statistics.fmean(lengths)
    cv = statistics.pstdev(lengths) / max(mean, 1e-9)
    if cv <= 0.6:
        score = 9.0
    elif cv <= 0.9:
        score = 7.0
    elif cv <= 1.2:
        score = 5.0
    else:
        score = 3.0
    return SignalValue(
        key="scene_length_distribution",
        value={"cv": round(cv, 4), "scene_count": len(lengths)},
        score=score,
        source="rule",
        confidence=0.7,
    )
