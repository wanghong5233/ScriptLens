"""v4.1 improvement_planner 多样性约束回归测试。

回归点：
    线上 script_id=19de2370 出现 top_improvements 3/3 全是 producibility 维度，
    其它 dealbreaker 维度（hook/payoff）有同样低分 signal 却 0 命中。
    根因：旧实现纯按 (dim_weight × sig_weight × gap) 全局排序，单个维度的
    大 gap signal 集中爆发就会霸占全部 top N 槽位。

修复：dealbreaker-first 轮询 + per_dimension_cap 约束。

测试设计：使用 SimpleNamespace 构造轻量 rubric stub，避免触发 RubricConfig 的
V4 5 维强校验（plan_improvements 只读 rubric.dimensions / rubric.aggregation /
rubric.truncation 三个字段）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

from service.scoring.framework import (
    DimensionScore,
    ScoreVerdict,
    SignalResult,
    SignalStatus,
    VerdictLabel,
)
from service.scoring.improvement_planner import plan_improvements


def _sig_cfg(key: str, weight: float) -> SimpleNamespace:
    return SimpleNamespace(key=key, weight_in_dim=weight)


def _dim_cfg(key: str, weight: float, signals: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(key=key, weight=weight, signals=signals)


def _build_rubric_stub(
    *,
    per_dimension_cap: int = 1,
    max_actions: int = 3,
    dealbreaker_dims: Optional[list[str]] = None,
) -> tuple[SimpleNamespace, SimpleNamespace]:
    """构造 plan_improvements 所需的最小 rubric。

    复刻线上 19de2370 现场：producibility 多个 signal 大 gap，hook/payoff 小 gap。
    （signal weight 总和不需要 = 1.0，因为这里跳过了 DimensionConfig pydantic 校验）
    """
    dims = {
        "hook": _dim_cfg(
            "hook",
            weight=0.25,
            signals=[
                _sig_cfg("first_3_scene_hook_chain", 0.25),
                _sig_cfg("opening_30char_conflict", 0.25),
            ],
        ),
        "payoff": _dim_cfg(
            "payoff",
            weight=0.20,
            signals=[
                _sig_cfg("reward_density_per_episode", 0.25),
                _sig_cfg("twist_density_per_episode", 0.18),
            ],
        ),
        "producibility": _dim_cfg(
            "producibility",
            weight=0.15,
            signals=[
                _sig_cfg("concurrent_characters_max_inv", 0.20),
                _sig_cfg("multi_character_continuity_load", 0.20),
                _sig_cfg("dialogue_density_per_scene_inv", 0.10),
            ],
        ),
    }
    aggregation = SimpleNamespace(
        dealbreaker_dims=list(dealbreaker_dims) if dealbreaker_dims is not None else ["hook", "payoff"],
    )
    truncation = SimpleNamespace(improvement_rationale_max_chars=140)
    rubric = SimpleNamespace(
        dimensions=dims,
        aggregation=aggregation,
        truncation=truncation,
    )
    planner_cfg = SimpleNamespace(
        max_actions=max_actions,
        min_signal_score_to_recommend=6.0,
        per_dimension_cap=per_dimension_cap,
        expected_verdict_lift_template_cn="{from_verdict} → {to_verdict}",
    )
    return rubric, planner_cfg


def _sig_result(key: str, score: float) -> SignalResult:
    return SignalResult(
        key=key,
        source="rule",  # type: ignore[arg-type]
        status=SignalStatus.COMPUTED,
        score=score,
        raw_value=0.5,
        evidence_ref_ids=[],
        detail="test",
    )


def _scores_like_user_19de2370() -> dict[str, DimensionScore]:
    """复刻线上 script_id=19de2370 的 signal 分布。"""
    return {
        "hook": DimensionScore(
            key="hook",
            score=7.0,
            tier="mid_high",  # type: ignore[arg-type]
            reason="x",
            signals=[
                _sig_result("first_3_scene_hook_chain", 5.0),  # gap=1, priority=0.25*0.25*1=0.0625
                _sig_result("opening_30char_conflict", 9.0),  # not < 6, ignored
            ],
        ),
        "payoff": DimensionScore(
            key="payoff",
            score=5.2,
            tier="mid_low",  # type: ignore[arg-type]
            reason="x",
            signals=[
                _sig_result("reward_density_per_episode", 5.0),  # gap=1, priority=0.20*0.25*1=0.050
                _sig_result("twist_density_per_episode", 5.0),  # gap=1, priority=0.20*0.18*1=0.036
            ],
        ),
        "producibility": DimensionScore(
            key="producibility",
            score=5.2,
            tier="mid_low",  # type: ignore[arg-type]
            reason="x",
            signals=[
                # gap=3, priority=0.15*0.20*3=0.090  <- 旧实现下排名 #1
                _sig_result("concurrent_characters_max_inv", 3.0),
                # gap=3, priority=0.15*0.20*3=0.090  <- 旧实现下排名 #2 (并列)
                _sig_result("multi_character_continuity_load", 3.0),
                # gap=2, priority=0.15*0.10*2=0.030
                _sig_result("dialogue_density_per_scene_inv", 4.0),
            ],
        ),
    }


def _verdict_needs_polish() -> ScoreVerdict:
    return ScoreVerdict(
        label=VerdictLabel.NEEDS_POLISH,
        overall_score=5.8,
        reason="x",
    )


def test_dealbreaker_first_diversity_no_single_dim_monopoly() -> None:
    """v4.1 per_dimension_cap=1 + dealbreaker-first：max_actions=3 时，3 条
    improvement 必须分别来自 3 个不同维度，且 dealbreaker 维度优先各拿 1 条。
    """
    rubric, planner_cfg = _build_rubric_stub(per_dimension_cap=1, max_actions=3)
    out = plan_improvements(
        _scores_like_user_19de2370(),
        _verdict_needs_polish(),
        rubric,
        planner_cfg,
    )
    assert len(out) == 3
    dim_keys = [a.dimension_key for a in out]
    assert len(set(dim_keys)) == 3, f"per_dim_cap=1 must yield 3 distinct dims, got {dim_keys}"
    # dealbreaker 维度全部命中（hook + payoff）
    assert "hook" in dim_keys
    assert "payoff" in dim_keys
    # producibility 也命中（剩余 1 槽，按 priority 全局降序）
    assert "producibility" in dim_keys


def test_dealbreaker_first_hook_outranks_producibility_despite_lower_priority() -> None:
    """dealbreaker-first 阶段：即便 producibility 的 priority 远高于 hook，
    hook 仍应优先被发一个槽位（不让非 dealbreaker 维度抢占救命槽位）。
    """
    rubric, planner_cfg = _build_rubric_stub(per_dimension_cap=1, max_actions=2)
    out = plan_improvements(
        _scores_like_user_19de2370(),
        _verdict_needs_polish(),
        rubric,
        planner_cfg,
    )
    assert len(out) == 2
    dim_keys = [a.dimension_key for a in out]
    # max_actions=2 时，两个 dealbreaker（hook + payoff）必须吃满，producibility 排不进
    assert set(dim_keys) == {"hook", "payoff"}, f"got {dim_keys}"


def test_per_dimension_cap_zero_falls_back_to_legacy_global_priority() -> None:
    """per_dimension_cap=0：兼容旧行为 —— 纯按 priority 全局降序，允许单维度霸占。"""
    rubric, planner_cfg = _build_rubric_stub(per_dimension_cap=0, max_actions=3)
    out = plan_improvements(
        _scores_like_user_19de2370(),
        _verdict_needs_polish(),
        rubric,
        planner_cfg,
    )
    assert len(out) == 3
    dim_keys = [a.dimension_key for a in out]
    # 旧行为：producibility 两个 0.090 priority 的 signal 排前两位，hook 0.0625 第三
    assert dim_keys.count("producibility") == 2
    assert "hook" in dim_keys


def test_no_dealbreaker_dims_falls_back_to_pure_cap() -> None:
    """rubric.aggregation.dealbreaker_dims 为空时，dealbreaker-first 阶段跳过，
    只走 per_dim_cap 约束（不应该崩）。
    """
    rubric, planner_cfg = _build_rubric_stub(
        per_dimension_cap=1, max_actions=3, dealbreaker_dims=[]
    )
    out = plan_improvements(
        _scores_like_user_19de2370(),
        _verdict_needs_polish(),
        rubric,
        planner_cfg,
    )
    assert len(out) == 3
    # 纯 cap 约束：依然 3 维度各 1 条（按 priority 而非 dealbreaker-first）
    assert len({a.dimension_key for a in out}) == 3
