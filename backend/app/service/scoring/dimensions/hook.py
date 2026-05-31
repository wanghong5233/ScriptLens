"""HOOK 抓人力维度。

signals:
- opening_30char_conflict        rule
- first_3_scene_hook_chain       rule
- episode_end_cliffhanger_rate   rule
- first_minute_inciting_incident llm_judge

所有阈值 / 关键词从 rubric YAML + _keywords.yaml 读，本文件无业务字面量。
"""

from __future__ import annotations

import logging

from service.scoring.aggregator import aggregate_dimension_score, map_signal_raw_to_score
from service.scoring.dimensions._common import (
    first_episode_scenes,
    make_failed_signal,
    make_signal,
    required_param,
    safe_div,
    scenes_by_episode,
)
from service.scoring.framework import (
    DimensionScore,
    ScoringContext,
    SignalResult,
    SignalSource,
)
from service.scoring.llm_judge import judge_with_schema
from service.scoring.prompts.hook_first_minute import (
    HookFirstMinutePayload,
    build_prompt as build_hook_prompt,
)
from service.scoring.rubric_loader import (
    DimensionConfig,
    DimensionTierCutsConfig,
    SignalConfig,
    load_keywords,
)

logger = logging.getLogger(__name__)


async def score_dimension(
    ctx: ScoringContext,
    dim_cfg: DimensionConfig,
    tier_cuts: DimensionTierCutsConfig,
) -> DimensionScore:
    sig_by_key = {s.key: s for s in dim_cfg.signals}

    signals: list[SignalResult] = []

    if "opening_30char_conflict" in sig_by_key:
        signals.append(_signal_opening_30char(ctx, sig_by_key["opening_30char_conflict"]))
    if "first_3_scene_hook_chain" in sig_by_key:
        signals.append(_signal_first_3_hook_chain(ctx, sig_by_key["first_3_scene_hook_chain"]))
    if "episode_end_cliffhanger_rate" in sig_by_key:
        signals.append(_signal_episode_end_cliffhanger(ctx, sig_by_key["episode_end_cliffhanger_rate"]))
    if "first_minute_inciting_incident" in sig_by_key:
        signals.append(
            await _signal_first_minute_incident(ctx, sig_by_key["first_minute_inciting_incident"])
        )

    score, tier = aggregate_dimension_score(signals, dim_cfg, tier_cuts)

    reason = _build_reason(signals)
    evidence: list[str] = []
    for s in signals:
        evidence.extend(s.evidence_ref_ids)
    # 去重保序
    seen: set[str] = set()
    evidence = [x for x in evidence if not (x in seen or seen.add(x))]

    return DimensionScore(
        key="hook",
        score=score,
        tier=tier,
        reason=reason,
        signals=signals,
        evidence_ref_ids=evidence[:5],
    )


# ============================================================
# signal 1: opening_30char_conflict
# ============================================================


def _signal_opening_30char(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    if not ctx.has_scenes():
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="scenes 为空，无法判定首场冲突"
        )

    char_window = required_param(cfg, "char_window", int)
    keywords = load_keywords().get("hook_keywords", [])

    first_scenes = first_episode_scenes(ctx.scenes)
    target_scene_count = max(1, required_param(cfg, "first_scene_count", int))
    target_scenes = first_scenes[:target_scene_count]

    hit_scenes: list[str] = []
    for sc in target_scenes:
        head = (sc.text or "")[:char_window]
        if any(kw in head for kw in keywords):
            hit_scenes.append(sc.id)

    raw = safe_div(float(len(hit_scenes)), float(target_scene_count))
    score, tier = map_signal_raw_to_score(raw, cfg)

    detail = f"前 {target_scene_count} 场首 {char_window} 字命中率 {raw:.0%}"
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        evidence_ref_ids=hit_scenes,
        detail=detail,
    )


# ============================================================
# signal 2: first_3_scene_hook_chain
# ============================================================


def _signal_first_3_hook_chain(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    if not ctx.has_scenes():
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="scenes 为空"
        )

    window = required_param(cfg, "window_scene_count", int)
    keywords = load_keywords().get("hook_keywords", [])

    first_scenes = first_episode_scenes(ctx.scenes)
    target = first_scenes[:window]

    hit_count = 0
    hit_scenes: list[str] = []
    for sc in target:
        if any(kw in (sc.text or "") for kw in keywords):
            hit_count += 1
            hit_scenes.append(sc.id)

    raw = safe_div(float(hit_count), float(max(window, 1)))
    score, tier = map_signal_raw_to_score(raw, cfg)
    detail = f"前 {window} 场钩子链命中 {hit_count}/{window}"
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        evidence_ref_ids=hit_scenes,
        detail=detail,
    )


# ============================================================
# signal 3: episode_end_cliffhanger_rate
# ============================================================


def _signal_episode_end_cliffhanger(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    if not ctx.has_scenes():
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="scenes 为空"
        )

    episodes = scenes_by_episode(ctx.scenes)
    if not episodes:
        return make_failed_signal(
            cfg.key,
            SignalSource.RULE,
            fallback_reason="无 episode_no 数据，无法判定集末钩子",
        )

    keywords = load_keywords().get("cliffhanger_keywords", [])
    hook_keywords = load_keywords().get("hook_keywords", [])
    combined = list(keywords) + list(hook_keywords)

    total_eps = len(episodes)
    hit_eps = 0
    evidence: list[str] = []
    tail_window = required_param(cfg, "tail_char_window", int)
    for ep, scenes in episodes.items():
        if not scenes:
            continue
        last = scenes[-1]
        tail_text = (last.text or "")[-tail_window:] if tail_window > 0 else (last.text or "")
        if any(kw in tail_text for kw in combined):
            hit_eps += 1
            evidence.append(last.id)

    raw = safe_div(float(hit_eps), float(total_eps))
    score, tier = map_signal_raw_to_score(raw, cfg)
    detail = f"集末有 cliffhanger 的集占比 {raw:.0%}（{hit_eps}/{total_eps}）"
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        evidence_ref_ids=evidence[:5],
        detail=detail,
    )


# ============================================================
# signal 4: first_minute_inciting_incident (LLM judge)
# ============================================================


async def _signal_first_minute_incident(
    ctx: ScoringContext,
    cfg: SignalConfig,
) -> SignalResult:
    if ctx.llm_caller is None:
        return make_failed_signal(
            cfg.key,
            SignalSource.LLM_JUDGE,
            fallback_reason="未注入 llm_caller，跳过 LLM judge",
        )
    if not ctx.has_scenes():
        return make_failed_signal(
            cfg.key, SignalSource.LLM_JUDGE, fallback_reason="scenes 为空"
        )

    judge_scene_count = required_param(cfg, "judge_scene_count", int)
    judge_excerpt_chars = required_param(cfg, "judge_excerpt_chars_per_scene", int)
    first_scenes = first_episode_scenes(ctx.scenes)[:judge_scene_count]
    if not first_scenes:
        return make_failed_signal(
            cfg.key, SignalSource.LLM_JUDGE, fallback_reason="第一集无场景"
        )
    excerpt = "\n\n".join((sc.text or "")[:judge_excerpt_chars] for sc in first_scenes)

    system_msg, user_prompt = build_hook_prompt(excerpt)
    result = await judge_with_schema(
        caller=ctx.llm_caller,
        prompt=user_prompt,
        schema=HookFirstMinutePayload,
        system_message=system_msg,
        chain_name="scoring.hook.first_minute_incident",
    )

    if not result.success or not isinstance(result.parsed, HookFirstMinutePayload):
        return make_failed_signal(
            cfg.key,
            SignalSource.LLM_JUDGE,
            fallback_reason=result.error or "LLM judge 失败",
        )

    payload = result.parsed
    raw = payload.incident_strength if payload.incident_present else 0.0
    score, tier = map_signal_raw_to_score(raw, cfg)
    detail = payload.rationale
    evidence = [sc.id for sc in first_scenes]
    return make_signal(
        key=cfg.key,
        source=SignalSource.LLM_JUDGE,
        score=score,
        tier=tier,
        raw_value=raw,
        evidence_ref_ids=evidence,
        detail=detail,
    )


# ============================================================
# reason 组装
# ============================================================


def _build_reason(signals: list[SignalResult]) -> str:
    parts: list[str] = []
    for sig in signals:
        if sig.detail and sig.score >= 0:
            parts.append(sig.detail)
    if not parts:
        return "信号缺失，无法形成 HOOK 抓人力判断"
    return "；".join(parts[:3])
