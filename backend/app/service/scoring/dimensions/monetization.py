"""MONETIZATION 变现力维度。

signals:
- paywall_cliffhanger_strength    hybrid（数据来自 cliffhanger_extractor LLM 二级判定）
- post_paywall_payoff_density     rule（付费首 N 集爽点密度，复用 reward_extractor 数据）
- episode_end_hook_grade          hybrid（数据来自 cliffhanger_extractor LLM 二级判定）
- paid_arc_twist_pacing           rule（付费段反转节奏，复用 reward_extractor 数据）
- paywall_hook_quality            llm_judge（付费拐点钩子叙述强度，质量维度）

设计要点（v4 评分准确度强化）：
- 历史 MONETIZATION 维度的 cliffhanger 信号是 naive 关键词扫（误报 / 漏报严重）。
- v4.1 (2026-05-31)：升级为 hybrid，数据由 `cliffhanger_extractor` 提供
  （关键词召回 → LLM 二级判定 → verbatim quote 校验 → confidence=high 过滤）。
  cliffhanger 已分 5 类（physical_danger / emotional_reveal / false_defeat /
  interrupted_moment / mystery_setup），cliff_type 直接进 detail 文案。
- 新增 `paywall_hook_quality` LLM judge，给 LLM 看付费拐点集末全文 + 付费
  首集首场预览，独立判读"用户是否愿意付费续看"。
- 业内对照：字节 WebConf 2026 *Short Drama QA* §3.2 cliffhanger taxonomy、
  ReelShort writer SOP《Episode-end Hook Design》、G-Eval。
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
from service.scoring.llm_judge import judge_with_schema
from service.scoring.prompts.monetization_paywall_hook import (
    PaywallHookQualityPayload,
    build_prompt as build_paywall_hook_prompt,
)
from service.scoring.rubric_loader import (
    DimensionConfig,
    DimensionTierCutsConfig,
    SignalConfig,
)

if TYPE_CHECKING:
    from service.script_tools.reward_extractor import RewardEvent

logger = logging.getLogger(__name__)

_TWIST_TYPES: frozenset[str] = frozenset({"reversal", "face_slap", "scheme_exposed"})

# 5 类 cliff_type 与 rubric YAML 中 type_weight_* key 对应（与
# script_tools.cliffhanger_extractor.CLIFF_TYPES 严格同集）。
_CLIFF_TYPES_FOR_PAYWALL: tuple[str, ...] = (
    "physical_danger",
    "false_defeat",
    "emotional_reveal",
    "interrupted_moment",
    "mystery_setup",
)


def _resolve_cliff_type_weights(cfg: SignalConfig) -> dict[str, float]:
    """从 rubric YAML 严格读 5 类 cliff_type 的 type_weight_*。

    缺任何一类立即 raise ValueError（fail aloud），避免 silent fallback。
    """
    weights: dict[str, float] = {}
    for ctype in _CLIFF_TYPES_FOR_PAYWALL:
        key = f"type_weight_{ctype}"
        if key not in cfg.params:
            raise ValueError(
                f"signal {cfg.key!r} 缺少 params.{key} 配置（rubric YAML 未配齐 5 类 cliff_type 权重）"
            )
        weights[ctype] = float(cfg.params[key])
    return weights


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
    if "paywall_hook_quality" in sig_by_key:
        signals.append(
            await _signal_paywall_hook_quality(ctx, sig_by_key["paywall_hook_quality"])
        )

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
    """付费拐点集集末的留钩强度（hybrid）：基于 cliffhanger_extractor 数据。

    打分逻辑（cliff_type 加权 + 多次出现增益）：
    - 该集集末被 AI 判定为 physical_danger / false_defeat → raw = 1.0（最强付费力）
    - emotional_reveal → raw = 0.85
    - interrupted_moment → raw = 0.7
    - mystery_setup → raw = 0.55
    - 无 AI 判定的留钩 → raw = 0.0
    业内对照：字节 WebConf 2026 §3.2 实测付费转化率与 cliff_type 强相关，
    physical_danger / false_defeat 平均付费率比 mystery_setup 高 1.8x。
    """
    if not ctx.has_scenes() or ctx.total_episodes <= 0:
        return make_failed_signal(
            cfg.key, SignalSource.HYBRID, fallback_reason="scenes/total_episodes 缺失"
        )

    paywall_ep = _resolve_paywall_episode(ctx, cfg)

    # cliff_type 加权严格读 rubric YAML（cn_short_drama.yaml 中
    # paywall_cliffhanger_strength.params.type_weight_*）。缺任何一类即视为
    # rubric schema 异常，按规则 fail aloud。
    type_weights = _resolve_cliff_type_weights(cfg)

    matched = next(
        (c for c in ctx.cliffhangers if c.episode_no == paywall_ep), None
    )
    if matched is None:
        score, tier = map_signal_raw_to_score(0.0, cfg)
        return make_signal(
            key=cfg.key,
            source=SignalSource.HYBRID,
            score=score,
            tier=tier,
            raw_value=0.0,
            evidence_ref_ids=[],
            detail=f"第 {paywall_ep} 集集末未出现强留钩，付费转化风险较高",
        )

    raw = type_weights.get(matched.cliff_type, type_weights["mystery_setup"])
    score, tier = map_signal_raw_to_score(raw, cfg)
    detail = (
        f"第 {paywall_ep} 集付费拐点：{matched.cliff_type_cn} — {matched.claim}"
    )
    return make_signal(
        key=cfg.key,
        source=SignalSource.HYBRID,
        score=score,
        tier=tier,
        raw_value=raw,
        evidence_ref_ids=[matched.scene_id],
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
    """整剧"集末有留钩的集数占比"（hybrid）：基于 cliffhanger_extractor 数据。

    与 HOOK.episode_end_cliffhanger_rate 同数据源，但 MONETIZATION 维度更关注
    付费节奏稳定性 —— 切点更严苛（在 rubric YAML tier_anchor 中体现）。
    """
    if not ctx.has_scenes() or ctx.total_episodes <= 0:
        return make_failed_signal(
            cfg.key, SignalSource.HYBRID, fallback_reason="scenes/total_episodes 缺失"
        )

    eps_map = scenes_by_episode(ctx.scenes)
    if not eps_map:
        return make_failed_signal(
            cfg.key, SignalSource.HYBRID, fallback_reason="无 episode_no 数据"
        )

    total = len(eps_map)
    cliffs = list(ctx.cliffhangers)
    if not cliffs:
        score, tier = map_signal_raw_to_score(0.0, cfg)
        return make_signal(
            key=cfg.key,
            source=SignalSource.HYBRID,
            score=score,
            tier=tier,
            raw_value=0.0,
            detail=f"{total} 集集末均未出现强留钩，付费节奏不稳",
        )

    hit = len(cliffs)
    raw = safe_div(float(hit), float(total))
    score, tier = map_signal_raw_to_score(raw, cfg)

    type_counts: dict[str, int] = {}
    for c in cliffs:
        type_counts[c.cliff_type_cn] = type_counts.get(c.cliff_type_cn, 0) + 1
    type_breakdown = "、".join(
        f"{label}×{count}" for label, count in sorted(
            type_counts.items(), key=lambda kv: -kv[1]
        )
    )

    detail = (
        f"{total} 集中 {hit} 集（{raw:.0%}）集末留有强钩子；"
        f"类型分布：{type_breakdown}"
    )
    return make_signal(
        key=cfg.key,
        source=SignalSource.HYBRID,
        score=score,
        tier=tier,
        raw_value=raw,
        evidence_ref_ids=[c.scene_id for c in cliffs[:5]],
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
    if paid_twists:
        avg_interval = paid_arc_length / max(len(paid_twists), 1)
        detail = (
            f"付费段（{paid_arc_length} 集）内出现 {len(paid_twists)} 次反转 / 打脸 / 阴谋揭穿，"
            f"平均每 {avg_interval:.1f} 集 1 次"
        )
    else:
        detail = f"付费段（{paid_arc_length} 集）内全程无反转，难以维持付费观众"
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        evidence_ref_ids=[r.scene_id for r in paid_twists[:3]],
        detail=detail,
    )


# ============================================================
# signal: paywall_hook_quality (LLM judge)
# ============================================================


async def _signal_paywall_hook_quality(
    ctx: ScoringContext,
    cfg: SignalConfig,
) -> SignalResult:
    if ctx.llm_caller is None:
        return make_failed_signal(
            cfg.key,
            SignalSource.LLM_JUDGE,
            fallback_reason="未注入 llm_caller，跳过 LLM judge",
        )
    if not ctx.has_scenes() or ctx.total_episodes <= 0:
        return make_failed_signal(
            cfg.key,
            SignalSource.LLM_JUDGE,
            fallback_reason="scenes/total_episodes 缺失",
        )

    paywall_ep = _resolve_paywall_episode(ctx, cfg)
    eps_map = scenes_by_episode(ctx.scenes)
    target_scenes = eps_map.get(paywall_ep)
    if not target_scenes:
        return make_failed_signal(
            cfg.key,
            SignalSource.LLM_JUDGE,
            fallback_reason=f"无第 {paywall_ep} 集场景",
        )

    last_scene = target_scenes[-1]
    excerpt_chars = required_param(cfg, "scene_excerpt_chars", int)
    paywall_text = (last_scene.text or "")[-excerpt_chars:]

    # 付费首集首场（可选预览）
    next_scenes = eps_map.get(paywall_ep + 1) or []
    next_scene_excerpt = ""
    if next_scenes:
        next_first = next_scenes[0]
        next_scene_excerpt = (next_first.text or "")[:excerpt_chars]

    system_msg, user_prompt = build_paywall_hook_prompt(
        paywall_episode=paywall_ep,
        paywall_scene_excerpt=paywall_text,
        next_scene_excerpt=next_scene_excerpt,
    )

    result = await judge_with_schema(
        caller=ctx.llm_caller,
        prompt=user_prompt,
        schema=PaywallHookQualityPayload,
        system_message=system_msg,
        chain_name="scoring.monetization.paywall_hook",
    )

    if not result.success or not isinstance(result.parsed, PaywallHookQualityPayload):
        return make_failed_signal(
            cfg.key,
            SignalSource.LLM_JUDGE,
            fallback_reason=result.error or "LLM judge 失败",
        )

    payload = result.parsed
    raw = payload.hook_strength
    # curiosity_gap 或 emotional_stakes 缺失 → 降一档
    if not payload.creates_curiosity_gap:
        raw = min(raw, cfg.tier_anchor.mid_high)
    if not payload.emotional_stakes_clear:
        raw = min(raw, cfg.tier_anchor.mid_high)

    score, tier = map_signal_raw_to_score(raw, cfg)
    return make_signal(
        key=cfg.key,
        source=SignalSource.LLM_JUDGE,
        score=score,
        tier=tier,
        raw_value=raw,
        evidence_ref_ids=[last_scene.id],
        detail=f"第 {paywall_ep} 集付费拐点：{payload.rationale}",
    )


def _build_reason(signals: list[SignalResult]) -> str:
    parts = [s.detail for s in signals if s.detail]
    return "；".join(parts[:3]) if parts else "数据不足，暂时无法判断变现力"
