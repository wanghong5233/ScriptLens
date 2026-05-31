"""Wave C-1：v4 评分字段在 ReportPayload 中的契约测试。

目标：守住 scoring.ScoringReport.to_dict() 产出的 dict 可以无损塞入
ReportPayload 的 verdict / investment_score / evaluation_v4 / top_improvements
4 个新字段，给 Wave D 前端一个稳定依赖。

不依赖 DB / LLM，纯走 Pydantic 模型验证 + scoring 类型 to_dict。
"""

from __future__ import annotations

from schemas.script import ReportCompliance, ReportDecision, ReportPayload, ReportScorecardItem
from service.scoring.framework import (
    ConfidenceLabel,
    DimensionScore,
    ImprovementAction,
    ScoreVerdict,
    ScoringReport,
    SignalResult,
    SignalSource,
    SignalStatus,
    TierLabel,
    VerdictLabel,
)


def _make_minimal_decision() -> ReportDecision:
    """v3 字段最小占位，让 ReportPayload 能构造成功。"""
    return ReportDecision(
        label="recommend_continue",
        confidence="medium",
        one_sentence_reason="占位",
        summary="",
        decision_inputs={},
    )


def _make_minimal_scorecard() -> list[ReportScorecardItem]:
    return [
        ReportScorecardItem(
            dimension="story",
            score=7.0,
            tier="good",
            confidence="medium",
            coverage_ratio=1.0,
            reason="占位",
        )
    ]


def _make_minimal_compliance() -> ReportCompliance:
    return ReportCompliance(status="pass", reason="")


def _make_signal(score: float = 7.0) -> SignalResult:
    return SignalResult(
        key="hook.opening_30char_conflict",
        source=SignalSource.RULE,
        status=SignalStatus.COMPUTED,
        score=score,
        raw_value=0.75,
        detail="规则命中冲突 keyword",
    )


def _make_dimension(key: str = "hook", score: float = 7.5) -> DimensionScore:
    return DimensionScore(
        key=key,
        score=score,
        tier=TierLabel.MID_HIGH,
        reason=f"{key} 维度占位",
        signals=[_make_signal()],
    )


def _make_scoring_report() -> ScoringReport:
    return ScoringReport(
        verdict=ScoreVerdict(
            label=VerdictLabel.QUALIFIED,
            reason="qualified 占位",
            overall_score=7.42,
            confidence=ConfidenceLabel.HIGH,
            compliance_tier="clean",
            compliance_veto_triggered=False,
        ),
        dimensions=[
            _make_dimension("hook", 7.5),
            _make_dimension("archetype", 7.0),
            _make_dimension("payoff", 7.6),
            _make_dimension("monetization", 7.2),
            _make_dimension("producibility", 7.0),
        ],
        top_improvements=[
            ImprovementAction(
                title="补足 first 3 scene 钩子链",
                rationale="占位",
                expected_verdict_lift="needs_polish -> qualified",
                dimension_key="hook",
                signal_key="hook.first_3_scene_hook_chain",
            )
        ],
        rubric_version="v4-cn-2026-05-31",
        coverage_ratio=0.95,
        chain_status_records=[{"dim_key": "hook", "status": "ok", "failed_signals": []}],
    )


# ============================================================
# 契约：ScoringReport.to_dict() 满足 ReportPayload 新字段
# ============================================================


def test_scoring_report_to_dict_matches_report_payload_v4_fields() -> None:
    report = _make_scoring_report()
    v4 = report.to_dict()

    payload = ReportPayload(
        script_id="test-script",
        title="占位",
        decision=_make_minimal_decision(),
        decision_reason="",
        overall_score=7.0,
        summary="",
        scorecard=_make_minimal_scorecard(),
        compliance=_make_minimal_compliance(),
        verdict=v4["verdict"],
        investment_score=v4["verdict"]["overall_score"],
        evaluation_v4=v4,
        top_improvements=v4["top_improvements"],
    )

    # verdict 字段断言（前端 Wave D 头部大字渲染依赖）
    assert payload.verdict is not None
    assert payload.verdict["label"] == "qualified"
    assert payload.verdict["overall_score"] == 7.42
    assert payload.verdict["compliance_veto_triggered"] is False

    # investment_score 展平字段（dashboard 列表卡片依赖）
    assert payload.investment_score == 7.42

    # evaluation_v4 全量挂载（前端 5 维卡片渲染依赖）
    assert payload.evaluation_v4 is not None
    assert payload.evaluation_v4["rubric_version"] == "v4-cn-2026-05-31"
    assert len(payload.evaluation_v4["dimensions"]) == 5
    assert payload.evaluation_v4["coverage_ratio"] == 0.95

    # top_improvements（前端"改进建议"卡片）
    assert len(payload.top_improvements) == 1
    assert payload.top_improvements[0]["signal_key"] == "hook.first_3_scene_hook_chain"


def test_report_payload_accepts_null_v4_fields_for_legacy_compatibility() -> None:
    """v4 评分失败 / 老报告无 v4 字段时，ReportPayload 必须接受全 None。"""
    payload = ReportPayload(
        script_id="legacy-script",
        title="占位",
        decision=_make_minimal_decision(),
        decision_reason="",
        overall_score=7.0,
        summary="",
        scorecard=_make_minimal_scorecard(),
        compliance=_make_minimal_compliance(),
        # 不传 verdict / investment_score / evaluation_v4 / top_improvements
    )
    assert payload.verdict is None
    assert payload.investment_score is None
    assert payload.evaluation_v4 is None
    assert payload.top_improvements == []


def test_investment_score_rejects_out_of_range() -> None:
    """investment_score Pydantic ge=0/le=10 守门。"""
    import pytest
    from pydantic import ValidationError

    for bad in (-1.0, 10.5, 100.0):
        with pytest.raises(ValidationError):
            ReportPayload(
                script_id="s",
                title="t",
                decision=_make_minimal_decision(),
                decision_reason="",
                overall_score=7.0,
                summary="",
                scorecard=_make_minimal_scorecard(),
                compliance=_make_minimal_compliance(),
                investment_score=bad,
            )
