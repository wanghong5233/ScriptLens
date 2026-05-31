"""scoring v4 改进建议生成器（top_improvements）。

策略：
1. 收集所有 score < cfg.min_signal_score_to_recommend 的 signal（按维度内权重×缺口排序）
2. 取 top N=cfg.max_actions 条
3. 每条给出 (signal-specific) 改进文案 + expected_verdict_lift（如该 signal 提升能否让
   dimension 升档 → verdict 升档）
4. 所有数字阈值来自 ImprovementPlannerConfig（YAML）；改进文案模板放本文件（属于"代码资产"
   而非"业务参数"，独立于 rubric 维护）
"""

from __future__ import annotations

from typing import Optional

from service.scoring.framework import (
    DimensionScore,
    ImprovementAction,
    ScoreVerdict,
    SignalResult,
    SignalStatus,
    VerdictLabel,
)
from service.scoring.rubric_loader import ImprovementPlannerConfig, RubricConfig

# ============================================================
# 改进文案模板（按 signal_key）
# ============================================================
#
# 模板设计原则：
# - 中文给业务用户看，**不要暴露 signal_key 等技术词**
# - 短句，可读，告诉用户"改什么 + 达到什么"
# - 与 LLM 改写链对接 ready：每条 title 应当能直接用作改写指令
# - 这是"代码资产"，独立于 rubric YAML（rubric 调阈值不应改文案）
# ============================================================

_TEMPLATE_BY_SIGNAL: dict[str, tuple[str, str]] = {
    # HOOK
    "opening_30char_conflict": (
        "首场前 30 字内加入强冲突 / 反转",
        "短剧爆款公式：用户 8 秒内决策；首场开场就要出现冲突词（重生 / 穿越 / 复仇 / 背叛 等）",
    ),
    "first_3_scene_hook_chain": (
        "前 3 场补全钩子链",
        "确保前 3 场每场都有强情节钩子，不要在第 2-3 场进入日常叙述",
    ),
    "episode_end_cliffhanger_rate": (
        "集末普遍补 cliffhanger",
        "提升集末有悬念 / 反转 / 威胁的集数占比，给用户留追下一集的动机",
    ),
    "first_minute_inciting_incident": (
        "把 inciting incident 前置到第一分钟",
        "短剧首集第一分钟必须出现强引爆点（穿越觉醒 / 死亡 / 强冲突），避免铺垫过长",
    ),
    # ARCHETYPE
    "genre_archetype_match": (
        "强化主导题材原型识别度",
        "在前 1-2 集明确投射到一个主流原型（战神归来 / 重生复仇 / 甜宠等），帮助算法分发",
    ),
    "character_archetype_match": (
        "强化角色原型标签",
        "让前 3 主要角色都能 1 秒识别原型（霸总 / 恶毒女配 / 团宠 等），简洁立人设",
    ),
    "differentiation_gap": (
        "在模板内增加微创新",
        "保留原型外壳的同时加入差异化记忆点（如穿越 + 修罗场组合）",
    ),
    # PAYOFF
    "reward_density_per_episode": (
        "提升爽点密度",
        "保证平均每集 ≥1.5 个爽点（打脸 / 反转 / 复仇 / 身份揭露），降低弃剧率",
    ),
    "twist_density_per_episode": (
        "提升反转密度",
        "每集至少有一次中等规模的反转，避免连续多集温水煮青蛙",
    ),
    "max_dry_streak_normalized": (
        "压缩连续无爽点集数",
        "连续无 reward 集数不要超过 2-3 集，避免完播率断崖",
    ),
    "episode_reward_coverage": (
        "提升有 reward 集数覆盖率",
        "尽量每集都要有一个爽点 / 反转 / 进展信号",
    ),
    # MONETIZATION
    "paywall_cliffhanger_strength": (
        "强化付费拐点悬念",
        "免费段末集（约 15-20 集）必须留强 cliffhanger，决定付费转化率",
    ),
    "post_paywall_payoff_density": (
        "付费首集强 payoff",
        "付费段开头 3 集必须密集给爽点，回报用户的付费决策",
    ),
    "episode_end_hook_grade": (
        "提升集末钩子均值",
        "所有集末都设计钩子（cliffhanger / twist / threat），保持追剧动机",
    ),
    "paid_arc_twist_pacing": (
        "付费段卡点节奏紧凑",
        "付费段每 5-8 集应有一次大反转，避免付费段松散",
    ),
    # PRODUCIBILITY
    "scene_count_per_episode_ratio_inv": (
        "压缩单集场景数",
        "单集场景数控制在 3-6 场为宜，过多会显著拉高 AI 视频生成成本",
    ),
    "concurrent_characters_max_inv": (
        "压缩单场同时在场角色数",
        "尽量避免单场 5+ 角色同时在场（多角色一致性是 AI 视频生成短板）",
    ),
    "special_scene_ratio_inv": (
        "降低特殊场景占比",
        "压低武打 / 魔法 / 大场面 / 特效场景比例，这些是 AI 视频质量灾难高发区",
    ),
    "outdoor_ratio_inv": (
        "降低室外场景占比",
        "AI 视频生成室外背景一致性成本更高，多用室内场景更稳",
    ),
    "dialogue_density_per_scene_inv": (
        "适度压缩对白密度",
        "嘴型一致性仍是 AI 视频短板，避免大段长对白",
    ),
    "multi_character_continuity_load": (
        "控制跨集复现角色数量",
        "压低需要 LoRA / reference image 复用的角色总数，减轻一致性负担",
    ),
}


def plan_improvements(
    dimension_scores: dict[str, DimensionScore],
    verdict: ScoreVerdict,
    rubric: RubricConfig,
    planner_cfg: ImprovementPlannerConfig,
) -> list[ImprovementAction]:
    """返回 top N 改进建议。

    候选筛选：
    - signal.score < cfg.min_signal_score_to_recommend
    - signal.status ∈ {COMPUTED, DEGRADED}（FAILED 的不推改进——还不知道好坏）
    - dim.weight * (target_score - signal.score) 作为排序权重
    """
    target = planner_cfg.min_signal_score_to_recommend
    candidates: list[tuple[float, DimensionScore, SignalResult]] = []

    for ds in dimension_scores.values():
        dim_cfg = rubric.dimensions.get(ds.key)
        if dim_cfg is None:
            continue
        for sig in ds.signals:
            if sig.status not in (SignalStatus.COMPUTED, SignalStatus.DEGRADED):
                continue
            if sig.score >= target:
                continue
            gap = target - sig.score
            # weight: dim_weight * signal_weight_in_dim * gap
            sig_cfg = next((sc for sc in dim_cfg.signals if sc.key == sig.key), None)
            if sig_cfg is None:
                continue
            priority = dim_cfg.weight * sig_cfg.weight_in_dim * gap
            candidates.append((priority, ds, sig))

    candidates.sort(key=lambda kv: kv[0], reverse=True)

    out: list[ImprovementAction] = []
    expected_lift = _expected_lift_label(verdict, planner_cfg)
    rationale_max = rubric.truncation.improvement_rationale_max_chars

    for _, ds, sig in candidates[: planner_cfg.max_actions]:
        title, rationale = _TEMPLATE_BY_SIGNAL.get(
            sig.key,
            (f"提升 {ds.key} 维度信号", f"{sig.key} 评分 {sig.score:.1f}/10，需要打磨"),
        )
        out.append(
            ImprovementAction(
                title=title,
                rationale=_truncate(rationale, rationale_max),
                expected_verdict_lift=expected_lift,
                dimension_key=ds.key,
                signal_key=sig.key,
                evidence_ref_ids=list(sig.evidence_ref_ids),
            )
        )
    return out


def _expected_lift_label(
    verdict: ScoreVerdict, planner_cfg: ImprovementPlannerConfig
) -> Optional[str]:
    template = planner_cfg.expected_verdict_lift_template_cn
    if verdict.label == VerdictLabel.NEEDS_POLISH:
        return template.format(from_verdict="待打磨复评", to_verdict="达标进入下一环节")
    if verdict.label == VerdictLabel.NOT_RECOMMENDED:
        return template.format(from_verdict="不建议立项", to_verdict="待打磨复评")
    return None  # qualified 不给 lift 提示


def _truncate(s: str, max_chars: int) -> str:
    if not s:
        return ""
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1] + "…"


__all__ = ["plan_improvements"]
