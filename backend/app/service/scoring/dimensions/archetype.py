"""ARCHETYPE 模板力维度。

signals:
- genre_archetype_match           rule（题材原型库匹配）
- character_archetype_match       rule（角色原型库匹配）
- differentiation_gap             llm_judge（模板内微差异）
"""

from __future__ import annotations

import logging

from service.scoring.aggregator import aggregate_dimension_score, map_signal_raw_to_score
from service.scoring.archetype_matcher import (
    match_character_archetype,
    match_genre_archetype,
)
from service.scoring.dimensions._common import (
    first_episode_scenes,
    make_failed_signal,
    make_signal,
    required_param,
)
from service.scoring.framework import (
    DimensionScore,
    ScoringContext,
    SignalResult,
    SignalSource,
)
from service.scoring.llm_judge import judge_with_schema
from service.scoring.prompts.archetype_differentiation import (
    ArchetypeDifferentiationPayload,
    build_prompt as build_arch_prompt,
)
from service.scoring.rubric_loader import (
    DimensionConfig,
    DimensionTierCutsConfig,
    SignalConfig,
)

logger = logging.getLogger(__name__)


async def score_dimension(
    ctx: ScoringContext,
    dim_cfg: DimensionConfig,
    tier_cuts: DimensionTierCutsConfig,
) -> DimensionScore:
    sig_by_key = {s.key: s for s in dim_cfg.signals}

    signals: list[SignalResult] = []
    genre_matches: list[str] = []

    if "genre_archetype_match" in sig_by_key:
        sig, matches = _signal_genre_archetype(ctx, sig_by_key["genre_archetype_match"])
        signals.append(sig)
        genre_matches = matches

    if "character_archetype_match" in sig_by_key:
        signals.append(
            _signal_character_archetype(ctx, sig_by_key["character_archetype_match"])
        )

    if "differentiation_gap" in sig_by_key:
        signals.append(
            await _signal_differentiation(
                ctx, sig_by_key["differentiation_gap"], genre_matches
            )
        )

    score, tier = aggregate_dimension_score(signals, dim_cfg, tier_cuts)

    evidence: list[str] = []
    for s in signals:
        evidence.extend(s.evidence_ref_ids)
    seen: set[str] = set()
    evidence = [x for x in evidence if not (x in seen or seen.add(x))]

    reason = _build_reason(signals)
    return DimensionScore(
        key="archetype",
        score=score,
        tier=tier,
        reason=reason,
        signals=signals,
        evidence_ref_ids=evidence[:5],
    )


# ============================================================
# signal: genre_archetype_match
# ============================================================


def _build_genre_text(ctx: ScoringContext, scene_count: int, chars_per_scene: int) -> str:
    """构造用于 genre 匹配的文本：前 N 集 + logline + synopsis + genre tags。"""
    parts: list[str] = []
    if ctx.coverage_card is not None:
        if getattr(ctx.coverage_card, "logline", ""):
            parts.append(ctx.coverage_card.logline)
        if getattr(ctx.coverage_card, "synopsis", ""):
            parts.append(ctx.coverage_card.synopsis)
        for g in getattr(ctx.coverage_card, "genre", []) or []:
            parts.append(str(g))
    first_eps = first_episode_scenes(ctx.scenes)
    for sc in first_eps[:scene_count]:
        parts.append((sc.text or "")[:chars_per_scene])
    return "\n".join(parts)


def _signal_genre_archetype(
    ctx: ScoringContext,
    cfg: SignalConfig,
) -> tuple[SignalResult, list[str]]:
    scene_count = required_param(cfg, "text_scene_count", int)
    chars_per_scene = required_param(cfg, "text_chars_per_scene", int)
    genre_text = _build_genre_text(ctx, scene_count, chars_per_scene)

    if not genre_text.strip():
        sig = make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="缺少 coverage/scenes 文本"
        )
        return sig, []

    library = required_param(cfg, "archetype_library", str)
    matches = match_genre_archetype(genre_text, library_name=library)

    # raw_value = top1 score（0-1）
    if matches:
        raw = matches[0].score
        top_names = [m.archetype.name for m in matches[:3]]
    else:
        raw = 0.0
        top_names = []

    score, tier = map_signal_raw_to_score(raw, cfg)
    detail_parts: list[str] = []
    if top_names:
        detail_parts.append("命中原型：" + " / ".join(top_names))
    else:
        detail_parts.append("未命中任何主流题材原型")
    detail = "；".join(detail_parts)
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        evidence_ref_ids=[],
        detail=detail,
    ), top_names


# ============================================================
# signal: character_archetype_match
# ============================================================


def _signal_character_archetype(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    if ctx.character_graph is None or not ctx.character_graph.nodes:
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="character_graph 为空"
        )

    top_n = required_param(cfg, "top_n_characters", int)
    library = required_param(cfg, "archetype_library", str)

    sorted_nodes = sorted(
        ctx.character_graph.nodes, key=lambda n: -n.appearance_count
    )[:top_n]
    if not sorted_nodes:
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="character_graph 节点为空"
        )

    inputs: list[str] = []
    for node in sorted_nodes:
        parts = [node.name or ""]
        if getattr(node, "motivation", ""):
            parts.append(node.motivation)
        if getattr(node, "goal", ""):
            parts.append(node.goal)
        if getattr(node, "obstacle", ""):
            parts.append(node.obstacle)
        inputs.append(" ".join(p for p in parts if p))

    matches = match_character_archetype(inputs, library_name=library)
    matched_count = len(matches)
    raw = matched_count / max(top_n, 1)

    score, tier = map_signal_raw_to_score(raw, cfg)

    hit_descriptions = [
        f"{inputs[i].split()[0] if inputs[i] else '?'}={m.archetype.name}"
        for i, (_, m) in enumerate(matches[:3])
    ]
    detail = (
        f"前 {top_n} 角色匹配 {matched_count}/{top_n}"
        + ("；" + "、".join(hit_descriptions) if hit_descriptions else "")
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
# signal: differentiation_gap (LLM judge)
# ============================================================


async def _signal_differentiation(
    ctx: ScoringContext,
    cfg: SignalConfig,
    top_archetype_names: list[str],
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
    genre_hint = ""
    genres = getattr(ctx.coverage_card, "genre", []) or []
    if genres:
        genre_hint = str(genres[0])

    if not (logline or synopsis):
        return make_failed_signal(
            cfg.key,
            SignalSource.LLM_JUDGE,
            fallback_reason="coverage_card 无 logline/synopsis 文本",
        )

    system_msg, user_prompt = build_arch_prompt(
        archetype_hint=genre_hint,
        logline=logline,
        synopsis=synopsis,
        top_archetype_matches=top_archetype_names,
    )

    result = await judge_with_schema(
        caller=ctx.llm_caller,
        prompt=user_prompt,
        schema=ArchetypeDifferentiationPayload,
        system_message=system_msg,
        chain_name="scoring.archetype.differentiation",
    )

    if not result.success or not isinstance(result.parsed, ArchetypeDifferentiationPayload):
        return make_failed_signal(
            cfg.key,
            SignalSource.LLM_JUDGE,
            fallback_reason=result.error or "LLM judge 失败",
        )

    payload = result.parsed
    # recognizable=false 时强制降分（即使 quality 高）
    raw = (
        payload.differentiation_quality
        if payload.archetype_recognizable
        else min(payload.differentiation_quality, cfg.tier_anchor.mid_low)
    )
    score, tier = map_signal_raw_to_score(raw, cfg)
    return make_signal(
        key=cfg.key,
        source=SignalSource.LLM_JUDGE,
        score=score,
        tier=tier,
        raw_value=raw,
        detail=payload.rationale,
    )


def _build_reason(signals: list[SignalResult]) -> str:
    parts = [s.detail for s in signals if s.detail]
    return "；".join(parts[:3]) if parts else "数据不足，暂时无法判断模板力"
