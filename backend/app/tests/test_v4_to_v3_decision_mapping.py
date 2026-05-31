"""Wave C-3a 契约测试：v4 verdict → v3 decision/overall_score 兼容映射。

保证：
1. 3 档 verdict label 映射到正确的 v3 decision label
2. compliance high_risk 优先于 verdict（一票否决）
3. v4 verdict 缺失（v4 失败 / 老报告） → 安全 fallback 到 cautious_continue
4. confidence 缺失 / 非法 → 默认 medium
5. 未知 verdict label → 保守 fallback 到 not_recommended

这些测试在 Wave C-3b 删除 ReportPayload.decision 字段时一并删除（届时
不再需要这个兼容映射）。
"""

from __future__ import annotations

from service.script_report_service import (
    _V4_VERDICT_TO_V3_DECISION_LABEL,
    _derive_v3_decision_from_v4_verdict,
)


# ============================================================
# 1. verdict label 映射表（3 档）
# ============================================================


def test_verdict_label_mapping_is_complete_3_levels():
    """v4 三档 verdict 必须各有 v3 decision label 映射"""
    assert _V4_VERDICT_TO_V3_DECISION_LABEL["qualified"] == "recommend_continue"
    assert _V4_VERDICT_TO_V3_DECISION_LABEL["needs_polish"] == "cautious_continue"
    assert _V4_VERDICT_TO_V3_DECISION_LABEL["not_recommended"] == "not_recommended"
    assert len(_V4_VERDICT_TO_V3_DECISION_LABEL) == 3


# ============================================================
# 2. 正常路径（v4 verdict 存在，合规 OK）
# ============================================================


def test_qualified_maps_to_recommend_continue():
    label, conf, reason = _derive_v3_decision_from_v4_verdict(
        {"label": "qualified", "confidence": "high", "reason": "v4 综合达标"},
        {},
    )
    assert label == "recommend_continue"
    assert conf == "high"
    assert "达标" in reason


def test_needs_polish_maps_to_cautious_continue():
    label, conf, reason = _derive_v3_decision_from_v4_verdict(
        {"label": "needs_polish", "confidence": "medium", "reason": "短板"},
        {},
    )
    assert label == "cautious_continue"
    assert conf == "medium"


def test_not_recommended_maps_to_not_recommended():
    label, conf, _ = _derive_v3_decision_from_v4_verdict(
        {"label": "not_recommended", "confidence": "high", "reason": "X"},
        {},
    )
    assert label == "not_recommended"


# ============================================================
# 3. 合规一票否决（优先于 v4 verdict）
# ============================================================


def test_compliance_high_risk_overrides_qualified_verdict():
    """v4 verdict=qualified 但合规 high_risk → 强制 not_recommended"""
    label, conf, reason = _derive_v3_decision_from_v4_verdict(
        {"label": "qualified", "confidence": "high", "reason": "ok"},
        {"tier": "high_risk"},
    )
    assert label == "not_recommended"
    assert conf == "high"
    assert "合规" in reason


def test_compliance_low_risk_does_not_override():
    label, _, _ = _derive_v3_decision_from_v4_verdict(
        {"label": "qualified", "confidence": "high", "reason": "ok"},
        {"tier": "low_risk"},
    )
    assert label == "recommend_continue"


# ============================================================
# 4. v4 verdict 缺失（v4 评分失败 / 老报告无 verdict）
# ============================================================


def test_none_verdict_falls_back_to_cautious_continue():
    label, conf, reason = _derive_v3_decision_from_v4_verdict(None, {})
    assert label == "cautious_continue"
    assert conf == "low"
    assert "v4" in reason or "复核" in reason


def test_empty_verdict_dict_falls_back():
    label, conf, _ = _derive_v3_decision_from_v4_verdict({}, {})
    assert label == "cautious_continue"
    assert conf == "low"


# ============================================================
# 5. 异常路径（防御性 fallback）
# ============================================================


def test_unknown_verdict_label_falls_back_to_not_recommended():
    """未来如果 v4 加新 verdict label 但忘了更新映射表 → 保守 fallback"""
    label, _, _ = _derive_v3_decision_from_v4_verdict(
        {"label": "future_label_that_doesnt_exist", "confidence": "high"},
        {},
    )
    assert label == "not_recommended"


def test_invalid_confidence_falls_back_to_medium():
    """confidence 不在 high/medium/low 之内 → medium"""
    _, conf, _ = _derive_v3_decision_from_v4_verdict(
        {"label": "qualified", "confidence": "ultra_high"},
        {},
    )
    assert conf == "medium"


def test_missing_reason_uses_default():
    _, _, reason = _derive_v3_decision_from_v4_verdict(
        {"label": "qualified", "confidence": "high"},
        {},
    )
    assert reason  # 非空字符串


def test_compliance_high_risk_with_none_verdict():
    """v4 verdict 缺失但合规 high_risk → 仍然 not_recommended（合规优先）"""
    label, conf, _ = _derive_v3_decision_from_v4_verdict(None, {"tier": "high_risk"})
    assert label == "not_recommended"
    assert conf == "high"
