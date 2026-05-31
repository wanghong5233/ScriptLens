"""MONETIZATION 变现力维度。

signals:
- paywall_cliffhanger_strength    rule
- post_paywall_payoff_density     rule
- episode_end_hook_grade          rule
- paid_arc_twist_pacing           rule
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from service.scoring.aggregator import aggregate_dimension_score, map_signal_raw_to_score
from service.scoring.dimensions._common import (
    make_failed_signal,
    make_signal,
    required_param,
    safe_div,
    scenes_by_episode,
)
from service.scoring.dimensions.payoff import _max_dry_streak
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
    load_keywords,
)

if TYPE_CHECKING:
    from service.script_tools.reward_extractor import RewardEvent

logger = logging.getLogger(__name__)

_TWIST_TYPES: frozenset[str] = frozenset({"reversal", "face_slap", "scheme_exposed"})


async def score_dimension(
    ctx: ScoringContext,
    dim_cfg: DimensionConfig,
    tier_cuts: DimensionTierCutsConfig,
) -> DimensionScore:
    sig_by_key = {s.key: s for s in dim_cfg.signals}
    signals: list[SignalResult] = []

    if "paywall_cliffhanger_strength" in sig_by_key:
        signals.append(_signal_paywall(ctx, sig_by_key["paywall_cliffhanger_strength"]))
    if "post_paywall_payoff_density" in sig_by_key:
        signals.append(_signal_post_paywall(ctx, sig_by_key["post_paywall_payoff_density"]))
    if "episode_end_hook_grade" in sig_by_key:
        signals.append(_signal_end_hook(ctx, sig_by_key["episode_end_hook_grade"]))
    if "paid_arc_twist_pacing" in sig_by_key:
        signals.append(_signal_paid_twist_pacing(ctx, sig_by_key["paid_arc_twist_pacing"]))

    score, tier = aggregate_dimension_score(signals, dim_cfg, tier_cuts)

    evidence: list[str] = []
    for s in signals:
        evidence.extend(s.evidence_ref_ids)
    seen: set[str] = set()
    evidence = [x for x in evidence if not (x in seen or seen.add(x))]
    return DimensionScore(
        key="monetization",
        score=score,
        tier=tier,
        reason=_build_reason(signals),
        signals=signals,
        evidence_ref_ids=evidence[:5],
    )


def _resolve_paywall_episode(ctx: ScoringContext, cfg: SignalConfig) -> int:
    """从 params 得到付费拐点的"目标集"。

    优先取 ctx.total_episodes 与 [paywall_min_episode, paywall_max_episode] 的中点
    （兼顾"实际集数 < min" 的短剧）。
    """
    min_ep = required_param(cfg, "paywall_min_episode", int)
    max_ep = required_param(cfg, "paywall_max_episode", int)
    if ctx.total_episodes <= 0:
        return (min_ep + max_ep) // 2
    mid = (min_ep + max_ep) // 2
    return min(ctx.total_episodes, mid)


# ============================================================
# signal: paywall_cliffhanger_strength
# ============================================================


def _signal_paywall(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    if not ctx.has_scenes() or ctx.total_episodes <= 0:
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="scenes/total_episodes 缺失"
        )

    paywall_ep = _resolve_paywall_episode(ctx, cfg)
    eps_map = scenes_by_episode(ctx.scenes)
    target_scenes = eps_map.get(paywall_ep)
    if not target_scenes:
        return make_failed_signal(
            cfg.key,
            SignalSource.RULE,
            fallback_reason=f"无第 {paywall_ep} 集场景",
        )

    last = target_scenes[-1]
    tail_window = required_param(cfg, "tail_char_window", int)
    tail_text = (last.text or "")[-tail_window:] if tail_window > 0 else (last.text or "")

    cliffhanger_kws = load_keywords().get("cliffhanger_keywords", []) or []
    hook_kws = load_keywords().get("hook_keywords", []) or []
    combined = list(cliffhanger_kws) + list(hook_kws)
    hit_count = sum(1 for kw in combined if kw in tail_text)
    # 归一化：0 命中 -> 0.0；1 命中 -> 0.5；≥2 命中 -> 1.0
    if hit_count >= 2:
        raw = 1.0
    elif hit_count == 1:
        raw = 0.5
    else:
        raw = 0.0

    score, tier = map_signal_raw_to_score(raw, cfg)
    detail = f"第 {paywall_ep} 集集末钩子词命中 {hit_count} 处"
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        evidence_ref_ids=[last.id],
        detail=detail,
    )


# ============================================================
# signal: post_paywall_payoff_density
# ============================================================


def _signal_post_paywall(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    if ctx.total_episodes <= 0:
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="total_episodes <= 0"
        )

    paywall_ep = _resolve_paywall_episode(ctx, cfg)
    post_window = required_param(cfg, "post_paywall_window", int)
    end_ep = min(ctx.total_episodes, paywall_ep + post_window)
    post_rewards = [
        r for r in ctx.reward_events
        if r.episode_no is not None and paywall_ep < r.episode_no <= end_ep
    ]
    raw = safe_div(float(len(post_rewards)), float(max(post_window, 1)))
    score, tier = map_signal_raw_to_score(raw, cfg)
    detail = (
        f"付费首 {post_window} 集（第 {paywall_ep + 1}-{end_ep} 集）"
        f"爽点密度 {raw:.2f}/集"
    )
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        evidence_ref_ids=[r.scene_id for r in post_rewards[:3]],
        detail=detail,
    )


# ============================================================
# signal: episode_end_hook_grade
# ============================================================


def _signal_end_hook(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    """整剧"集末有钩子的集数占比"——比 HOOK 维度的 cliffhanger_rate 阈值更宽松，
    本维度算的是"付费节奏稳定性"，与 HOOK 维度的差异在 tier 切点。
    """
    if not ctx.has_scenes() or ctx.total_episodes <= 0:
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="scenes/total_episodes 缺失"
        )

    eps_map = scenes_by_episode(ctx.scenes)
    if not eps_map:
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="无 episode_no 数据"
        )

    cliffhanger_kws = load_keywords().get("cliffhanger_keywords", []) or []
    hook_kws = load_keywords().get("hook_keywords", []) or []
    combined = list(cliffhanger_kws) + list(hook_kws)
    tail_window = required_param(cfg, "tail_char_window", int)

    total = len(eps_map)
    hit = 0
    for ep, scenes in eps_map.items():
        if not scenes:
            continue
        last = scenes[-1]
        tail = (last.text or "")[-tail_window:] if tail_window > 0 else (last.text or "")
        if any(kw in tail for kw in combined):
            hit += 1
    raw = safe_div(float(hit), float(total))
    score, tier = map_signal_raw_to_score(raw, cfg)
    detail = f"集末钩子覆盖率 {raw:.0%}（{hit}/{total} 集）"
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        detail=detail,
    )


# ============================================================
# signal: paid_arc_twist_pacing
# ============================================================


def _signal_paid_twist_pacing(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    if ctx.total_episodes <= 0:
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="total_episodes <= 0"
        )

    paywall_ep = _resolve_paywall_episode(ctx, cfg)
    paid_twists: list[RewardEvent] = [
        r for r in ctx.reward_events
        if r.event_type in _TWIST_TYPES
        and r.episode_no is not None
        and r.episode_no > paywall_ep
    ]
    paid_arc_length = ctx.total_episodes - paywall_ep
    if paid_arc_length <= 0:
        return make_failed_signal(
            cfg.key,
            SignalSource.RULE,
            fallback_reason="付费段长度 <= 0（剧本太短或 paywall_ep 配置错）",
        )

    # raw_value = 反转密度的倒数感：反转间隔越小，raw 越大
    # 1 / 平均间隔（间隔 = paid_arc_length / 反转数）
    if not paid_twists:
        raw = 0.0
    else:
        avg_interval = paid_arc_length / len(paid_twists)
        raw = safe_div(1.0, avg_interval)
        raw = min(raw, 1.0)

    score, tier = map_signal_raw_to_score(raw, cfg)
    detail = (
        f"付费段共 {len(paid_twists)} 反转 / {paid_arc_length} 集 = 1/{paid_arc_length / max(len(paid_twists), 1):.1f} 集"
        if paid_twists
        else f"付费段（{paid_arc_length} 集）无反转"
    )
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        evidence_ref_ids=[r.scene_id for r in paid_twists[:3]],
        detail=detail,
    )


def _build_reason(signals: list[SignalResult]) -> str:
    parts = [s.detail for s in signals if s.detail]
    return "；".join(parts[:3]) if parts else "信号缺失，无法形成 MONETIZATION 判断"
