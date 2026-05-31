"""PAYOFF 爽感力维度。

signals:
- reward_density_per_episode      rule
- twist_density_per_episode       rule
- max_dry_streak_normalized       rule
- episode_reward_coverage         rule
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from service.scoring.aggregator import aggregate_dimension_score, map_signal_raw_to_score
from service.scoring.dimensions._common import (
    make_failed_signal,
    make_signal,
    safe_div,
)
from service.scoring.framework import (
    DimensionScore,
    ScoringContext,
    SignalResult,
    SignalSource,
)
from service.scoring.rubric_loader import (
    DimensionConfig,
    DimensionTierCutsConfig,
    SignalConfig,
)

if TYPE_CHECKING:
    from service.script_tools.reward_extractor import RewardEvent

logger = logging.getLogger(__name__)


# 与 reward_extractor 对齐的反转类型集合
_TWIST_TYPES: frozenset[str] = frozenset({"reversal", "face_slap", "scheme_exposed"})


async def score_dimension(
    ctx: ScoringContext,
    dim_cfg: DimensionConfig,
    tier_cuts: DimensionTierCutsConfig,
) -> DimensionScore:
    sig_by_key = {s.key: s for s in dim_cfg.signals}

    signals: list[SignalResult] = []

    if "reward_density_per_episode" in sig_by_key:
        signals.append(_signal_reward_density(ctx, sig_by_key["reward_density_per_episode"]))
    if "twist_density_per_episode" in sig_by_key:
        signals.append(_signal_twist_density(ctx, sig_by_key["twist_density_per_episode"]))
    if "max_dry_streak_normalized" in sig_by_key:
        signals.append(_signal_dry_streak(ctx, sig_by_key["max_dry_streak_normalized"]))
    if "episode_reward_coverage" in sig_by_key:
        signals.append(_signal_episode_coverage(ctx, sig_by_key["episode_reward_coverage"]))

    score, tier = aggregate_dimension_score(signals, dim_cfg, tier_cuts)
    evidence: list[str] = []
    for s in signals:
        evidence.extend(s.evidence_ref_ids)
    seen: set[str] = set()
    evidence = [x for x in evidence if not (x in seen or seen.add(x))]

    return DimensionScore(
        key="payoff",
        score=score,
        tier=tier,
        reason=_build_reason(signals),
        signals=signals,
        evidence_ref_ids=evidence[:5],
    )


def _check_reward_inputs(ctx: ScoringContext, key: str, source: SignalSource) -> SignalResult | None:
    if ctx.total_episodes <= 0:
        return make_failed_signal(
            key, source, fallback_reason="total_episodes <= 0，无法计算密度"
        )
    return None


# ============================================================
# signal: reward_density_per_episode
# ============================================================


def _signal_reward_density(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    err = _check_reward_inputs(ctx, cfg.key, SignalSource.RULE)
    if err is not None:
        return err

    rewards = ctx.reward_events
    raw = safe_div(float(len(rewards)), float(ctx.total_episodes))
    score, tier = map_signal_raw_to_score(raw, cfg)
    evidence = [r.scene_id for r in rewards[:3]]
    detail = f"全剧爽点密度 {raw:.2f}/集（共 {len(rewards)} 条）"
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        evidence_ref_ids=evidence,
        detail=detail,
    )


# ============================================================
# signal: twist_density_per_episode
# ============================================================


def _signal_twist_density(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    err = _check_reward_inputs(ctx, cfg.key, SignalSource.RULE)
    if err is not None:
        return err

    twists = [r for r in ctx.reward_events if r.event_type in _TWIST_TYPES]
    raw = safe_div(float(len(twists)), float(ctx.total_episodes))
    score, tier = map_signal_raw_to_score(raw, cfg)
    evidence = [r.scene_id for r in twists[:3]]
    detail = f"反转密度 {raw:.2f}/集（共 {len(twists)} 条）"
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        evidence_ref_ids=evidence,
        detail=detail,
    )


# ============================================================
# signal: max_dry_streak_normalized
# ============================================================


def _signal_dry_streak(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    err = _check_reward_inputs(ctx, cfg.key, SignalSource.RULE)
    if err is not None:
        return err

    max_dry = _max_dry_streak(ctx.reward_events, ctx.total_episodes)
    raw = 1.0 - safe_div(float(max_dry), float(ctx.total_episodes))
    raw = max(0.0, min(1.0, raw))
    score, tier = map_signal_raw_to_score(raw, cfg)
    detail = f"最长无爽点连续 {max_dry} 集 / 共 {ctx.total_episodes} 集"
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        detail=detail,
    )


def _max_dry_streak(events: list["RewardEvent"], total_eps: int) -> int:
    """与 dimension_scorer._max_dry_streak 对照同语义实现。"""
    if total_eps <= 0:
        return 0
    if not events:
        return total_eps
    eps_with_reward = sorted({ev.episode_no for ev in events if ev.episode_no is not None})
    if not eps_with_reward:
        return total_eps
    prev = 0
    max_gap = 0
    for ep in eps_with_reward:
        gap = ep - prev - 1
        max_gap = max(max_gap, gap)
        prev = ep
    tail = total_eps - prev
    max_gap = max(max_gap, tail)
    return max_gap


# ============================================================
# signal: episode_reward_coverage
# ============================================================


def _signal_episode_coverage(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    err = _check_reward_inputs(ctx, cfg.key, SignalSource.RULE)
    if err is not None:
        return err

    eps_with_reward = {r.episode_no for r in ctx.reward_events if r.episode_no is not None}
    raw = safe_div(float(len(eps_with_reward)), float(ctx.total_episodes))
    score, tier = map_signal_raw_to_score(raw, cfg)
    detail = f"有爽点的集数占比 {raw:.0%}"
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        detail=detail,
    )


def _build_reason(signals: list[SignalResult]) -> str:
    parts = [s.detail for s in signals if s.detail]
    return "；".join(parts[:3]) if parts else "信号缺失，无法形成 PAYOFF 判断"
