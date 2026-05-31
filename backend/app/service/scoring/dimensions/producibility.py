"""PRODUCIBILITY 可生成力维度。

signals 全部 rule 类（不需要 LLM 判断）：
- scene_count_per_episode_ratio_inv   单集场景数（反向）
- concurrent_characters_max_inv       单场同时在场角色峰值（反向）
- special_scene_ratio_inv             特殊场景占比（反向）
- outdoor_ratio_inv                   室外场景占比（反向）
- dialogue_density_per_scene_inv      对白密度（反向）
- multi_character_continuity_load     跨集复现角色数（反向）
"""

from __future__ import annotations

import logging
import re

from service.scoring.aggregator import aggregate_dimension_score, map_signal_raw_to_score
from service.scoring.dimensions._common import (
    make_failed_signal,
    make_signal,
    normalize_inverse,
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
from service.scoring.rubric_loader import (
    DimensionConfig,
    DimensionTierCutsConfig,
    SignalConfig,
    load_keywords,
)

logger = logging.getLogger(__name__)


_DIALOGUE_LINE_RE = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9·•_]{1,10}\s*[：:]")


async def score_dimension(
    ctx: ScoringContext,
    dim_cfg: DimensionConfig,
    tier_cuts: DimensionTierCutsConfig,
) -> DimensionScore:
    sig_by_key = {s.key: s for s in dim_cfg.signals}
    signals: list[SignalResult] = []

    if "scene_count_per_episode_ratio_inv" in sig_by_key:
        signals.append(_signal_scene_density(ctx, sig_by_key["scene_count_per_episode_ratio_inv"]))
    if "concurrent_characters_max_inv" in sig_by_key:
        signals.append(_signal_concurrent_chars(ctx, sig_by_key["concurrent_characters_max_inv"]))
    if "special_scene_ratio_inv" in sig_by_key:
        signals.append(_signal_special_scene_ratio(ctx, sig_by_key["special_scene_ratio_inv"]))
    if "outdoor_ratio_inv" in sig_by_key:
        signals.append(_signal_outdoor_ratio(ctx, sig_by_key["outdoor_ratio_inv"]))
    if "dialogue_density_per_scene_inv" in sig_by_key:
        signals.append(_signal_dialogue_density(ctx, sig_by_key["dialogue_density_per_scene_inv"]))
    if "multi_character_continuity_load" in sig_by_key:
        signals.append(
            _signal_character_continuity(ctx, sig_by_key["multi_character_continuity_load"])
        )

    score, tier = aggregate_dimension_score(signals, dim_cfg, tier_cuts)
    return DimensionScore(
        key="producibility",
        score=score,
        tier=tier,
        reason=_build_reason(signals),
        signals=signals,
    )


def _resolve_normalize_window(cfg: SignalConfig) -> tuple[float, float]:
    low = required_param(cfg, "normalize_low", float)
    high = required_param(cfg, "normalize_high", float)
    return low, high


def _signal_scene_density(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    if not ctx.has_scenes() or ctx.total_episodes <= 0:
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="scenes/total_episodes 缺失"
        )

    avg_scene_per_ep = safe_div(float(len(ctx.scenes)), float(ctx.total_episodes))
    low, high = _resolve_normalize_window(cfg)
    raw = normalize_inverse(avg_scene_per_ep, low, high)
    score, tier = map_signal_raw_to_score(raw, cfg)
    detail = f"平均 {avg_scene_per_ep:.1f} 场/集"
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        detail=detail,
    )


def _signal_concurrent_chars(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    if not ctx.has_scenes():
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="scenes 为空"
        )
    counts = [len(s.characters or []) for s in ctx.scenes]
    if not counts:
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="无 characters 数据"
        )
    max_concurrent = max(counts)
    low, high = _resolve_normalize_window(cfg)
    raw = normalize_inverse(float(max_concurrent), low, high)
    score, tier = map_signal_raw_to_score(raw, cfg)
    detail = f"单场最大同时在场角色数 {max_concurrent}"
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        detail=detail,
    )


def _signal_special_scene_ratio(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    if not ctx.has_scenes():
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="scenes 为空"
        )
    kws_data = load_keywords().get("special_scene_keywords", {}) or {}
    all_kws: list[str] = []
    for group_kws in kws_data.values():
        all_kws.extend(group_kws or [])

    total = len(ctx.scenes)
    hit = 0
    hit_scenes: list[str] = []
    for sc in ctx.scenes:
        combined = (sc.scene_label or "") + "\n" + (sc.text or "")
        if any(kw in combined for kw in all_kws):
            hit += 1
            hit_scenes.append(sc.id)
    ratio = safe_div(float(hit), float(total))
    raw = 1.0 - ratio
    score, tier = map_signal_raw_to_score(raw, cfg)
    detail = f"特殊场景（武打/魔法/古风/特效/大场面）占比 {ratio:.0%}"
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        evidence_ref_ids=hit_scenes[:3],
        detail=detail,
    )


def _signal_outdoor_ratio(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    if not ctx.has_scenes():
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="scenes 为空"
        )
    outdoor_kws = load_keywords().get("outdoor_scene_keywords", []) or []
    indoor_kws = load_keywords().get("indoor_scene_keywords", []) or []

    head_chars = required_param(cfg, "head_chars", int)
    total = 0
    outdoor = 0
    for sc in ctx.scenes:
        label = sc.scene_label or ""
        head_text = (sc.text or "")[:head_chars]
        haystack = label + " " + head_text
        is_outdoor = any(kw in haystack for kw in outdoor_kws)
        is_indoor = any(kw in haystack for kw in indoor_kws)
        if is_outdoor or is_indoor:
            total += 1
            if is_outdoor and not is_indoor:
                outdoor += 1
    if total == 0:
        return make_failed_signal(
            cfg.key,
            SignalSource.RULE,
            fallback_reason="所有场景标签都无室内外信息",
        )
    ratio = safe_div(float(outdoor), float(total))
    raw = 1.0 - ratio
    score, tier = map_signal_raw_to_score(raw, cfg)
    detail = f"室外场景占比 {ratio:.0%}（{outdoor}/{total}）"
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        detail=detail,
    )


def _signal_dialogue_density(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    if not ctx.has_scenes():
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="scenes 为空"
        )
    line_counts: list[int] = []
    for sc in ctx.scenes:
        count = 0
        for line in (sc.text or "").splitlines():
            if _DIALOGUE_LINE_RE.match(line.strip()):
                count += 1
        line_counts.append(count)
    if not line_counts:
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="无对白可解析"
        )
    avg = sum(line_counts) / len(line_counts)
    low, high = _resolve_normalize_window(cfg)
    raw = normalize_inverse(avg, low, high)
    score, tier = map_signal_raw_to_score(raw, cfg)
    detail = f"平均每场对白 {avg:.1f} 行"
    return make_signal(
        key=cfg.key,
        source=SignalSource.RULE,
        score=score,
        tier=tier,
        raw_value=raw,
        detail=detail,
    )


def _signal_character_continuity(ctx: ScoringContext, cfg: SignalConfig) -> SignalResult:
    if not ctx.has_scenes():
        return make_failed_signal(
            cfg.key, SignalSource.RULE, fallback_reason="scenes 为空"
        )
    eps_by_char: dict[str, set[int]] = {}
    for sc in ctx.scenes:
        if sc.episode_no is None:
            continue
        for c in sc.characters or []:
            eps_by_char.setdefault(c, set()).add(sc.episode_no)
    cross_eps_char_count = sum(1 for eps in eps_by_char.values() if len(eps) >= 2)
    low, high = _resolve_normalize_window(cfg)
    raw = normalize_inverse(float(cross_eps_char_count), low, high)
    score, tier = map_signal_raw_to_score(raw, cfg)
    detail = f"跨集复现角色 {cross_eps_char_count} 个"
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
    return "；".join(parts[:3]) if parts else "信号缺失，无法形成 PRODUCIBILITY 判断"
