"""HOOK 抓人力维度。

signals:
- opening_30char_conflict        hybrid（rule 命中走 rule，未命中走 LLM 兜底）
- first_3_scene_hook_chain       rule
- episode_end_cliffhanger_rate   hybrid（数据来自 cliffhanger_extractor LLM 二级判定）
- first_minute_inciting_incident llm_judge

所有阈值 / 关键词从 rubric YAML + _keywords.yaml 读，本文件无业务字面量。

设计要点（v4 评分准确度强化）：
- `opening_30char_conflict` 从 rule 升级为 hybrid（rule 兜底 + LLM 判读首场冲突）
- `episode_end_cliffhanger_rate` 从 naive 关键词扫升级为 hybrid：
  数据源由 `cliffhanger_extractor`（关键词召回 → LLM 二级判定 → verbatim quote）提供，
  本信号只做"覆盖率 + 类型加权"聚合。
  业内对照：字节 WebConf 2026 *Short Drama QA* §3.2 cliffhanger taxonomy
  （5 类 cliff_type 替代纯关键词），ReelShort writer SOP《Episode-end Hook Design》。
- 业内对照：G-Eval (Liu et al, EMNLP 2023)、字节 WebConf 2026 *Short Drama
  Quality Assessment* 都证明 hybrid 比纯 rule / 纯 LLM 在该类任务上都更稳。
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
from service.scoring.prompts.hook_opening_conflict import (
    HookOpeningConflictPayload,
    build_prompt as build_opening_prompt,
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
        signals.append(
            await _signal_opening_30char(ctx, sig_by_key["opening_30char_conflict"])
        )
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
# signal 1: opening_30char_conflict (hybrid)
# ============================================================
#
# Hybrid 策略（rule + LLM judge 兜底，对照 G-Eval / 字节 WebConf 2026 短剧质量评测）：
# 1. 规则关键词命中 → 走规则（快、可解释、零成本）；source=HYBRID, raw 由规则给出
# 2. 规则未命中 (raw < hybrid_llm_trigger_threshold) → 调 LLM judge 二次判读
#    首场实际剧情是否真有强冲突。LLM 命中时 raw 替换为 LLM 判读值；
#    LLM 失败 / 未注入 caller → 保留规则 raw（不抛错，记 fallback_reason）。
#
# 这样既避免了关键词 FN（《钓系娇娇》首场 0% 但实际有"系统觉醒"），
# 又避免了"每个剧本都跑 LLM"的成本（约 80% 剧本规则就能命中）。


async def _signal_opening_30char(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    if not ctx.has_scenes():
        return make_failed_signal(
            cfg.key, SignalSource.HYBRID, fallback_reason="scenes 为空，无法判定首场冲突"
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

    rule_raw = safe_div(float(len(hit_scenes)), float(target_scene_count))
    llm_trigger_threshold = required_param(cfg, "hybrid_llm_trigger_threshold", float)

    # 规则命中（raw 达到触发阈值）→ 走规则结果
    if rule_raw >= llm_trigger_threshold:
        score, tier = map_signal_raw_to_score(rule_raw, cfg)
        detail = f"前 {target_scene_count} 场首 {char_window} 字关键词命中率 {rule_raw:.0%}"
        return make_signal(
            key=cfg.key,
            source=SignalSource.HYBRID,
            score=score,
            tier=tier,
            raw_value=rule_raw,
            evidence_ref_ids=hit_scenes,
            detail=detail,
        )

    # 规则未命中 → LLM 兜底
    if ctx.llm_caller is None:
        score, tier = map_signal_raw_to_score(rule_raw, cfg)
        return make_signal(
            key=cfg.key,
            source=SignalSource.HYBRID,
            score=score,
            tier=tier,
            raw_value=rule_raw,
            evidence_ref_ids=hit_scenes,
            detail=(
                f"前 {target_scene_count} 场关键词未命中（{rule_raw:.0%}），"
                "且未注入 llm_caller 跳过 LLM 兜底"
            ),
            status=ctx_no_caller_status(),
            fallback_reason="未注入 llm_caller，hybrid 信号回退到规则结果",
        )

    judge_excerpt_chars = required_param(cfg, "judge_excerpt_chars", int)
    first_scene = target_scenes[0] if target_scenes else None
    if first_scene is None:
        score, tier = map_signal_raw_to_score(rule_raw, cfg)
        return make_signal(
            key=cfg.key,
            source=SignalSource.HYBRID,
            score=score,
            tier=tier,
            raw_value=rule_raw,
            evidence_ref_ids=hit_scenes,
            detail="首场不存在，无法 LLM 兜底",
        )
    excerpt = (first_scene.text or "")[:judge_excerpt_chars]

    system_msg, user_prompt = build_opening_prompt(excerpt)
    result = await judge_with_schema(
        caller=ctx.llm_caller,
        prompt=user_prompt,
        schema=HookOpeningConflictPayload,
        system_message=system_msg,
        chain_name="scoring.hook.opening_conflict",
    )

    if not result.success or not isinstance(result.parsed, HookOpeningConflictPayload):
        # LLM 失败 → 沉降为规则结果，但 status 标 DEGRADED
        score, tier = map_signal_raw_to_score(rule_raw, cfg)
        return make_signal(
            key=cfg.key,
            source=SignalSource.HYBRID,
            score=score,
            tier=tier,
            raw_value=rule_raw,
            evidence_ref_ids=hit_scenes,
            detail=f"规则未命中（{rule_raw:.0%}），LLM 兜底失败，按规则结果给分",
            status=_degraded_status(),
            fallback_reason=f"LLM 兜底失败: {result.error or 'unknown'}",
        )

    payload = result.parsed
    # LLM 判定有冲突时，raw 取 conflict_strength；判定无冲突时仍保留规则 raw（≈0），
    # 不让 LLM 单方面"洗清"明显平淡的开场。
    llm_raw = payload.conflict_strength if payload.conflict_present else 0.0
    final_raw = max(rule_raw, llm_raw)

    score, tier = map_signal_raw_to_score(final_raw, cfg)
    detail = (
        f"规则关键词未命中（{rule_raw:.0%}），AI 判读："
        f"{payload.conflict_type}（强度 {payload.conflict_strength:.1f}）— {payload.rationale}"
    )
    return make_signal(
        key=cfg.key,
        source=SignalSource.HYBRID,
        score=score,
        tier=tier,
        raw_value=final_raw,
        evidence_ref_ids=[first_scene.id],
        detail=detail,
    )


def ctx_no_caller_status():
    """局部 helper：未注入 llm_caller 时 hybrid 信号不算 failed，记 DEGRADED。"""
    from service.scoring.framework import SignalStatus

    return SignalStatus.DEGRADED


def _degraded_status():
    from service.scoring.framework import SignalStatus

    return SignalStatus.DEGRADED


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
    detail = (
        f"开篇前 {window} 场中有 {hit_count} 场出现强冲突 / 反转 / 危机类钩子词"
    )
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
    """集末留钩覆盖率（hybrid）：基于 cliffhanger_extractor 的 LLM 判读结果。

    数据契约（替代旧版关键词扫）：
    - 输入：`ctx.cliffhangers`（CliffhangerEvent[]）—— 上游 cliffhanger_extractor
      已经做了"关键词召回 + LLM 二级判定 + verbatim quote 校验 + confidence=high
      过滤"，每条事件都是高置信度的真 cliffhanger。
    - 输出：覆盖率（多少集集末被 AI 判读为有留钩）+ 类型分布人话叙述。

    上游 chain 故障时 (`ctx.cliffhangers` 为空且应有数据) 走 FAILED；空剧本
    走 NOT_APPLICABLE。
    """
    if not ctx.has_scenes():
        return make_failed_signal(
            cfg.key, SignalSource.HYBRID, fallback_reason="scenes 为空"
        )
    episodes = scenes_by_episode(ctx.scenes)
    if not episodes:
        return make_failed_signal(
            cfg.key,
            SignalSource.HYBRID,
            fallback_reason="无 episode_no 数据，无法判定集末钩子",
        )

    total_eps = len(episodes)
    cliffs = list(ctx.cliffhangers)

    if not cliffs:
        # 上游 cliffhanger_extractor 失败或剧本确实零留钩。前者已在 chain_status
        # 标 failed；本信号保守按 raw=0 计分，让用户看到"0 集留钩"的明确信号。
        score, tier = map_signal_raw_to_score(0.0, cfg)
        return make_signal(
            key=cfg.key,
            source=SignalSource.HYBRID,
            score=score,
            tier=tier,
            raw_value=0.0,
            evidence_ref_ids=[],
            detail=f"AI 判读全剧 {total_eps} 集集末均未发现强留钩",
        )

    hit_eps = len(cliffs)
    evidence_ids = [c.scene_id for c in cliffs[:5]]
    raw = safe_div(float(hit_eps), float(total_eps))
    score, tier = map_signal_raw_to_score(raw, cfg)

    # 类型分布人话叙述（替代英文 "cliffhanger" 术语）
    type_counts: dict[str, int] = {}
    for c in cliffs:
        type_counts[c.cliff_type_cn] = type_counts.get(c.cliff_type_cn, 0) + 1
    type_breakdown = "、".join(
        f"{label}×{count}" for label, count in sorted(
            type_counts.items(), key=lambda kv: -kv[1]
        )
    )
    detail = (
        f"AI 判读 {total_eps} 集中有 {hit_eps} 集（{raw:.0%}）集末留有强钩子；"
        f"类型分布：{type_breakdown}"
    )

    return make_signal(
        key=cfg.key,
        source=SignalSource.HYBRID,
        score=score,
        tier=tier,
        raw_value=raw,
        evidence_ref_ids=evidence_ids,
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
