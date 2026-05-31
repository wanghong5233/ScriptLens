"""PAYOFF 爽感力维度。

signals:
- reward_density_per_episode      rule（爽点密度计数）
- twist_density_per_episode       rule（反转密度计数）
- max_dry_streak_normalized       rule（最长干涸段）
- episode_reward_coverage         rule（爽点集覆盖率）
- emotion_payoff_quality          llm_judge（爽感叙述强度，质量维度）

设计要点（v4 评分准确度强化）：
- 历史 PAYOFF 维度 LLM judge 占比 = 0%，所有信号都是 reward_extractor
  关键词计数的衍生。但短剧"爽感"本质是主观质量判断（同样 N 个 reward，
  强度 / 节奏 / 是否主线驱动可能差 3 倍），仅靠计数 FN 率高。
- 新增 `emotion_payoff_quality` LLM judge signal，给 LLM 看 logline +
  synopsis + 3-5 段 reward 采样，输出 intensity_score / has_strong_arc
  / main_arc_driven 三元判读。
- 业内对照：抖音《短剧爆款公式 2024》§4、ReelShort writer SOP
  《Reward design》、G-Eval (Liu et al, EMNLP 2023)。
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
)
from service.scoring.framework import (
    DimensionScore,
    ScoringContext,
    SignalResult,
    SignalSource,
)
from service.scoring.llm_judge import judge_with_schema
from service.scoring.prompts.payoff_emotion_intensity import (
    PayoffEmotionIntensityPayload,
    build_prompt as build_payoff_prompt,
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


# 与 reward_extractor 对齐的反转类型集合
_TWIST_TYPES: frozenset[str] = frozenset({"reversal", "face_slap", "scheme_exposed"})


def _reward_type_cn_labels() -> dict[str, str]:
    """从 signals/_keywords.yaml 读 reward type 中文标签。"""
    return load_keywords().get("reward_type_cn_labels", {}) or {}


def _reward_type_priority() -> dict[str, int]:
    """从 signals/_keywords.yaml 读 reward type 强度优先级。"""
    raw = load_keywords().get("reward_type_priority", {}) or {}
    return {k: int(v) for k, v in raw.items()}


def _reward_type_breakdown(rewards: list["RewardEvent"]) -> str:
    """把 reward_events 按 event_type 计数 + 中文化，返回 "打脸×9、反转×3" 字串。"""
    cn_labels = _reward_type_cn_labels()
    counts: dict[str, int] = {}
    for r in rewards:
        counts[r.event_type] = counts.get(r.event_type, 0) + 1
    items = sorted(counts.items(), key=lambda kv: -kv[1])
    return "、".join(
        f"{cn_labels.get(t, t)}×{n}" for t, n in items
    )


def _top_reward_claim(rewards: list["RewardEvent"]) -> str:
    """返回最强 reward 事件的中文诠释（reward_extractor LLM 输出的 claim）。"""
    type_priority = _reward_type_priority()
    sorted_rewards = sorted(
        rewards, key=lambda r: -type_priority.get(r.event_type, 0)
    )
    for r in sorted_rewards:
        if r.claim:
            return r.claim
    return ""


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
    if "emotion_payoff_quality" in sig_by_key:
        signals.append(
            await _signal_emotion_payoff_quality(ctx, sig_by_key["emotion_payoff_quality"])
        )

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

    rewards = list(ctx.reward_events)
    raw = safe_div(float(len(rewards)), float(ctx.total_episodes))
    score, tier = map_signal_raw_to_score(raw, cfg)
    evidence = [r.scene_id for r in rewards[:3]]

    if not rewards:
        detail = f"AI 在全剧 {ctx.total_episodes} 集中未识别出任何爽点事件"
    else:
        breakdown = _reward_type_breakdown(rewards)
        top_claim = _top_reward_claim(rewards)
        detail = (
            f"AI 在 {ctx.total_episodes} 集中识别出 {len(rewards)} 个爽点事件（平均 {raw:.2f}/集），"
            f"类型分布：{breakdown}"
        )
        if top_claim:
            detail += f"；其中最强一处：「{top_claim}」"
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

    if not twists:
        detail = f"AI 在全剧 {ctx.total_episodes} 集中未识别出反转 / 打脸 / 阴谋揭穿事件"
    else:
        breakdown = _reward_type_breakdown(twists)
        top_claim = _top_reward_claim(twists)
        detail = (
            f"AI 识别出 {len(twists)} 个反转级事件（平均 {raw:.2f}/集），"
            f"类型：{breakdown}"
        )
        if top_claim:
            detail += f"；代表场景：「{top_claim}」"
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
    detail = (
        f"全剧最长连续 {max_dry} 集没有爽点 "
        f"（占 {ctx.total_episodes} 集中的 {max_dry / max(ctx.total_episodes, 1):.0%}）"
    )
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
    detail = (
        f"{ctx.total_episodes} 集中有 {len(eps_with_reward)} 集（{raw:.0%}）"
        f"出现爽点事件"
    )
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        detail=detail,
    )


# ============================================================
# signal: emotion_payoff_quality (LLM judge)
# ============================================================


async def _signal_emotion_payoff_quality(
    ctx: ScoringContext,
    cfg: SignalConfig,
) -> SignalResult:
    if ctx.llm_caller is None:
        return make_failed_signal(
            cfg.key,
            SignalSource.LLM_JUDGE,
            fallback_reason="未注入 llm_caller，跳过 LLM judge",
        )
    if ctx.coverage_card is None:
        return make_failed_signal(
            cfg.key,
            SignalSource.LLM_JUDGE,
            fallback_reason="coverage_card 为空，无 logline/synopsis 可判定",
        )

    logline = getattr(ctx.coverage_card, "logline", "") or ""
    synopsis = getattr(ctx.coverage_card, "synopsis", "") or ""
    if not (logline or synopsis):
        return make_failed_signal(
            cfg.key,
            SignalSource.LLM_JUDGE,
            fallback_reason="coverage_card 无 logline/synopsis 文本",
        )

    sample_count = required_param(cfg, "reward_sample_count", int)
    excerpt_chars = required_param(cfg, "reward_sample_chars", int)

    # 选 top N 个 reward 周边场景做采样（按 episode 早 → 晚顺序，保证覆盖全剧）
    # 注意：reward_extractor 没采到 reward 时 ctx.reward_events 为空 → LLM 只看 logline+synopsis
    sample_excerpts = _collect_reward_sample_excerpts(ctx, sample_count, excerpt_chars)

    system_msg, user_prompt = build_payoff_prompt(
        logline=logline,
        synopsis=synopsis,
        reward_sample_excerpts=sample_excerpts,
        total_episodes=ctx.total_episodes,
        reward_count=len(ctx.reward_events),
    )

    result = await judge_with_schema(
        caller=ctx.llm_caller,
        prompt=user_prompt,
        schema=PayoffEmotionIntensityPayload,
        system_message=system_msg,
        chain_name="scoring.payoff.emotion_intensity",
    )

    if not result.success or not isinstance(result.parsed, PayoffEmotionIntensityPayload):
        return make_failed_signal(
            cfg.key,
            SignalSource.LLM_JUDGE,
            fallback_reason=result.error or "LLM judge 失败",
        )

    payload = result.parsed
    # 没有强 payoff arc 或不是主线驱动 → 降一档（即使 intensity_score 高）
    raw = payload.intensity_score
    if not payload.has_strong_payoff_arc:
        raw = min(raw, cfg.tier_anchor.mid_high)
    if not payload.main_arc_driven:
        raw = min(raw, cfg.tier_anchor.mid_high)

    score, tier = map_signal_raw_to_score(raw, cfg)
    return make_signal(
        key=cfg.key,
        source=SignalSource.LLM_JUDGE,
        score=score,
        tier=tier,
        raw_value=raw,
        detail=payload.rationale,
    )


def _collect_reward_sample_excerpts(
    ctx: ScoringContext, sample_count: int, excerpt_chars: int
) -> list[str]:
    """从 reward_events 中采样 sample_count 个，取其 scene_id 对应场景文本前 excerpt_chars 字。

    采样策略：按 episode_no 均匀采样，覆盖全剧不偏前/偏后。
    """
    if not ctx.reward_events or sample_count <= 0:
        return []
    scenes_by_id = {sc.id: sc for sc in ctx.scenes}
    rewards_sorted = sorted(
        [r for r in ctx.reward_events if r.episode_no is not None],
        key=lambda r: r.episode_no,  # type: ignore[arg-type, return-value]
    )
    if not rewards_sorted:
        return []
    step = max(1, len(rewards_sorted) // sample_count)
    picked = rewards_sorted[::step][:sample_count]
    excerpts: list[str] = []
    for r in picked:
        sc = scenes_by_id.get(r.scene_id)
        if sc is None or not sc.text:
            continue
        excerpts.append(sc.text[:excerpt_chars])
    return excerpts


def _build_reason(signals: list[SignalResult]) -> str:
    parts = [s.detail for s in signals if s.detail]
    return "；".join(parts[:3]) if parts else "信号缺失，无法形成 PAYOFF 判断"
