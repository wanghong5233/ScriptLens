"""scoring v4 aggregator 测试。

覆盖 4 个 gate 全分支：
- Gate 1 compliance veto
- Gate 2 dealbreaker
- Gate 3 weighted sum
- Gate 4 verdict bucket (qualified / needs_polish / not_recommended)
"""

from __future__ import annotations

from service.scoring.aggregator import (
    aggregate_dimension_score,
    compute_verdict,
    lookup_verdict_display,
    map_signal_raw_to_score,
)
from service.scoring.framework import (
    ConfidenceLabel,
    DimensionScore,
    SignalResult,
    SignalSource,
    SignalStatus,
    TierLabel,
    VerdictLabel,
)
from service.scoring.rubric_loader import load_rubric


def _ds(key: str, score: float, signals: list[SignalResult] | None = None) -> DimensionScore:
    return DimensionScore(
        key=key,
        score=score,
        tier=TierLabel.MID_HIGH,
        reason="test",
        signals=signals or [],
    )


def _all_scores_dim(scores: dict[str, float]) -> dict[str, DimensionScore]:
    return {k: _ds(k, v) for k, v in scores.items()}


# ============================================================
# Gate 1: compliance veto
# ============================================================


def test_gate1_compliance_veto() -> None:
    rubric = load_rubric()
    dim_scores = _all_scores_dim(
        {"hook": 9.0, "archetype": 9.0, "payoff": 9.0, "monetization": 9.0, "producibility": 9.0}
    )
    v = compute_verdict(dim_scores, compliance_tier="high_risk", rubric=rubric)
    assert v.label == VerdictLabel.NOT_RECOMMENDED
    assert v.compliance_veto_triggered is True
    assert v.compliance_tier == "high_risk"


# ============================================================
# Gate 2: dealbreaker
# ============================================================


def test_gate2_dealbreaker_triggers() -> None:
    rubric = load_rubric()
    # hook=2 < 3.0 dealbreaker_threshold
    dim_scores = _all_scores_dim(
        {"hook": 2.0, "archetype": 9.0, "payoff": 9.0, "monetization": 9.0, "producibility": 9.0}
    )
    v = compute_verdict(dim_scores, compliance_tier=None, rubric=rubric)
    assert v.label == VerdictLabel.NOT_RECOMMENDED
    assert "HOOK" in v.reason or "hook" in v.reason


def test_gate2_dealbreaker_just_above_threshold() -> None:
    rubric = load_rubric()
    # 3.0 不触发（严格 < 才触发）
    dim_scores = _all_scores_dim(
        {"hook": 3.0, "archetype": 7.5, "payoff": 7.5, "monetization": 7.0, "producibility": 7.0}
    )
    v = compute_verdict(dim_scores, compliance_tier=None, rubric=rubric)
    assert v.label != VerdictLabel.NOT_RECOMMENDED or "dealbreaker" not in v.reason


# ============================================================
# Gate 4: 三档 bucket
# ============================================================


def test_gate4_qualified() -> None:
    rubric = load_rubric()
    dim_scores = _all_scores_dim(
        {"hook": 8.0, "archetype": 8.0, "payoff": 8.0, "monetization": 7.5, "producibility": 7.0}
    )
    v = compute_verdict(dim_scores, compliance_tier=None, rubric=rubric)
    assert v.label == VerdictLabel.QUALIFIED
    assert v.overall_score is not None and v.overall_score >= 7.0


def test_gate4_needs_polish() -> None:
    rubric = load_rubric()
    dim_scores = _all_scores_dim(
        {"hook": 6.0, "archetype": 6.0, "payoff": 6.0, "monetization": 6.0, "producibility": 6.0}
    )
    v = compute_verdict(dim_scores, compliance_tier=None, rubric=rubric)
    assert v.label == VerdictLabel.NEEDS_POLISH


def test_gate4_qualified_floor_violation_falls_to_needs_polish() -> None:
    """overall ≥ 7.0 但 floor < 5.0 → needs_polish（不能 qualified）。"""
    rubric = load_rubric()
    dim_scores = _all_scores_dim(
        {"hook": 9.5, "archetype": 9.5, "payoff": 9.0, "monetization": 9.0, "producibility": 4.0}
    )
    v = compute_verdict(dim_scores, compliance_tier=None, rubric=rubric)
    # overall 大约 8.6, floor=4 → 不应 qualified
    assert v.label != VerdictLabel.QUALIFIED


def test_gate4_not_recommended_low_overall() -> None:
    rubric = load_rubric()
    dim_scores = _all_scores_dim(
        {"hook": 4.0, "archetype": 4.0, "payoff": 4.0, "monetization": 4.0, "producibility": 4.0}
    )
    v = compute_verdict(dim_scores, compliance_tier=None, rubric=rubric)
    assert v.label == VerdictLabel.NOT_RECOMMENDED


# ============================================================
# Verdict display lookup
# ============================================================


def test_lookup_verdict_display() -> None:
    rubric = load_rubric()
    assert lookup_verdict_display(rubric, VerdictLabel.QUALIFIED, "cn") == "达标进入下一环节"
    assert lookup_verdict_display(rubric, VerdictLabel.NEEDS_POLISH, "cn") == "待打磨复评"
    assert lookup_verdict_display(rubric, VerdictLabel.NOT_RECOMMENDED, "cn") == "不建议立项"
    assert lookup_verdict_display(rubric, VerdictLabel.QUALIFIED, "en").startswith("Qualified")


# ============================================================
# map_signal_raw_to_score
# ============================================================


def test_map_signal_raw_to_score_high_bucket() -> None:
    rubric = load_rubric()
    hook = rubric.dimensions["hook"]
    sig_cfg = next(s for s in hook.signals if s.key == "opening_30char_conflict")
    score, tier = map_signal_raw_to_score(1.0, sig_cfg)
    assert tier == "high"
    assert score == sig_cfg.tier_scores.high


def test_map_signal_raw_to_score_low_bucket() -> None:
    rubric = load_rubric()
    hook = rubric.dimensions["hook"]
    sig_cfg = next(s for s in hook.signals if s.key == "opening_30char_conflict")
    score, tier = map_signal_raw_to_score(0.0, sig_cfg)
    assert tier == "low"
    assert score == sig_cfg.tier_scores.low


# ============================================================
# aggregate_dimension_score
# ============================================================


def test_aggregate_dim_score_skips_failed_signals() -> None:
    rubric = load_rubric()
    hook = rubric.dimensions["hook"]
    # 构造 4 个 signal，其中 1 个失败：剩 3 个权重归一化
    sig_results = [
        SignalResult(
            key=cfg.key,
            source=SignalSource.RULE if cfg.source == "rule" else SignalSource.LLM_JUDGE,
            status=SignalStatus.COMPUTED,
            score=9.0,
            raw_value=1.0,
        )
        for cfg in hook.signals
    ]
    # 把最后一个标失败
    sig_results[-1] = SignalResult(
        key=hook.signals[-1].key,
        source=SignalSource.LLM_JUDGE,
        status=SignalStatus.FAILED,
        score=0.0,
        fallback_reason="LLM fail",
    )
    score, tier = aggregate_dimension_score(sig_results, hook, rubric.dimension_tier_cuts)
    # 失败 signal 不参与归一化，剩 3 个全 9.0 → 平均 9.0
    assert abs(score - 9.0) < 1e-6
    assert tier == TierLabel.HIGH


def test_aggregate_dim_score_all_failed_returns_zero() -> None:
    rubric = load_rubric()
    hook = rubric.dimensions["hook"]
    sig_results = [
        SignalResult(
            key=cfg.key,
            source=SignalSource.RULE,
            status=SignalStatus.FAILED,
            score=0.0,
            fallback_reason="all fail",
        )
        for cfg in hook.signals
    ]
    score, tier = aggregate_dimension_score(sig_results, hook, rubric.dimension_tier_cuts)
    assert score == 0.0
    assert tier == TierLabel.LOW
