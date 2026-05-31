"""scoring v4 rubric_loader 测试。

覆盖：
- YAML 正常加载
- 缺字段抛 RubricSchemaError（不许 default 兜底）
- 权重和 != 1.0 抛错
- legacy v3 dim_key 抛 RubricLegacyDimensionError
"""

from __future__ import annotations

import copy
import pytest
import yaml
from pydantic import ValidationError

from service.scoring.rubric_loader import (
    RubricConfig,
    RubricLegacyDimensionError,
    RubricSchemaError,
    V4_DIMENSION_KEYS,
    assert_valid_v4_dimension,
    load_keywords,
    load_archetype_library,
    load_rubric,
)


def test_load_rubric_v4_shape() -> None:
    r = load_rubric()
    assert r.version == "v4-cn-2026-05-31"
    assert set(r.dimensions.keys()) == V4_DIMENSION_KEYS
    assert r.status == "active"
    # 5 维 weight 之和 = 1.0
    total = sum(d.weight for d in r.dimensions.values())
    assert abs(total - 1.0) < 1e-6


def test_signal_weights_sum_to_one_per_dim() -> None:
    r = load_rubric()
    for key, dim in r.dimensions.items():
        sig_total = sum(s.weight_in_dim for s in dim.signals)
        assert abs(sig_total - 1.0) < 1e-6, f"{key} signals weight_in_dim 总和 = {sig_total}"


def test_tier_anchor_present_for_all_signals() -> None:
    r = load_rubric()
    for dim in r.dimensions.values():
        for s in dim.signals:
            assert s.tier_anchor is not None
            assert s.tier_scores is not None


def test_dealbreaker_dims_all_valid() -> None:
    r = load_rubric()
    for d in r.aggregation.dealbreaker_dims:
        assert d in r.dimensions
        assert r.dimensions[d].is_dealbreaker is True


def test_verdict_three_labels_present() -> None:
    r = load_rubric()
    assert set(r.aggregation.verdicts.keys()) == {"qualified", "needs_polish", "not_recommended"}


def test_legacy_v3_dim_key_raises() -> None:
    for legacy in ("story", "character", "concept", "emotion", "pacing", "dialogue"):
        with pytest.raises(RubricLegacyDimensionError):
            assert_valid_v4_dimension(legacy)


def test_unknown_dim_key_raises() -> None:
    with pytest.raises(ValueError):
        assert_valid_v4_dimension("nonexistent_dim")


def test_v4_dim_keys_pass() -> None:
    for dim in V4_DIMENSION_KEYS:
        assert_valid_v4_dimension(dim)  # 不应抛异常


# ============================================================
# Pydantic schema 强校验：缺字段 / 错权重应抛 RubricSchemaError
# ============================================================


def _valid_rubric_dict() -> dict:
    """构造一份最小完整 rubric dict。"""
    sig_template = {
        "key": "x",
        "source": "rule",
        "weight_in_dim": 1.0,
        "params": {},
        "tier_anchor": {"high": 1.0, "mid_high": 0.6, "mid_low": 0.3, "low": 0.0},
        "tier_scores": {"high": 9.0, "mid_high": 7.0, "mid_low": 5.0, "low": 2.0},
    }
    dim_template_db = {
        "weight": 0.2,
        "is_dealbreaker": True,
        "label": "X",
        "description": "",
        "signals": [copy.deepcopy(sig_template)],
    }
    dim_template_nondb = copy.deepcopy(dim_template_db)
    dim_template_nondb["is_dealbreaker"] = False
    return {
        "version": "v4-cn-test",
        "status": "active",
        "locale": "cn",
        "description": "test",
        "dimensions": {
            "hook": copy.deepcopy(dim_template_db),
            "archetype": copy.deepcopy(dim_template_db),
            "payoff": copy.deepcopy(dim_template_db),
            "monetization": copy.deepcopy(dim_template_nondb),
            "producibility": copy.deepcopy(dim_template_nondb),
        },
        "aggregation": {
            "type": "gated_multiplicative",
            "dealbreaker_dims": ["hook", "archetype", "payoff"],
            "dealbreaker_threshold": 3.0,
            "dealbreaker_action": "force_not_recommended",
            "verdict_cuts": {
                "qualified_overall_min": 7.0,
                "qualified_floor_min": 5.0,
                "needs_polish_overall_min": 5.5,
            },
            "verdicts": {
                "qualified": {
                    "label": "qualified",
                    "display_cn": "达标",
                    "display_en": "Qualified",
                },
                "needs_polish": {
                    "label": "needs_polish",
                    "display_cn": "待打磨",
                    "display_en": "Needs polish",
                },
                "not_recommended": {
                    "label": "not_recommended",
                    "display_cn": "不立项",
                    "display_en": "Not recommended",
                },
            },
        },
        "compliance": {
            "is_independent_gate": True,
            "veto_tier": "high_risk",
            "high_risk_action": "veto",
        },
        "confidence": {
            "high_min_coverage": 0.8,
            "medium_min_coverage": 0.5,
            "max_llm_judge_failures_for_high": 1,
            "max_llm_judge_failures_for_medium": 3,
        },
        "truncation": {
            "reason_max_chars": 120,
            "evidence_excerpt_max_chars": 80,
            "improvement_rationale_max_chars": 140,
        },
        "improvement_planner": {
            "max_actions": 3,
            "min_signal_score_to_recommend": 6.0,
            "expected_verdict_lift_template_cn": "{from_verdict} → {to_verdict}",
        },
        "dimension_tier_cuts": {"high": 8.0, "mid_high": 6.5, "mid_low": 5.0},
    }


def test_pydantic_validates_full_dict() -> None:
    cfg = RubricConfig.model_validate(_valid_rubric_dict())
    assert cfg.version == "v4-cn-test"


def test_missing_aggregation_raises() -> None:
    d = _valid_rubric_dict()
    del d["aggregation"]
    with pytest.raises(ValidationError):
        RubricConfig.model_validate(d)


def test_signal_weights_not_one_raises() -> None:
    d = _valid_rubric_dict()
    d["dimensions"]["hook"]["signals"][0]["weight_in_dim"] = 0.5  # 总和 != 1
    with pytest.raises(ValidationError):
        RubricConfig.model_validate(d)


def test_dimensions_weights_not_one_raises() -> None:
    d = _valid_rubric_dict()
    d["dimensions"]["hook"]["weight"] = 0.5
    with pytest.raises(ValidationError):
        RubricConfig.model_validate(d)


def test_missing_dimension_raises() -> None:
    d = _valid_rubric_dict()
    del d["dimensions"]["producibility"]
    with pytest.raises(ValidationError):
        RubricConfig.model_validate(d)


def test_dealbreaker_pointing_to_nondb_dim_raises() -> None:
    d = _valid_rubric_dict()
    d["aggregation"]["dealbreaker_dims"] = ["monetization"]  # is_dealbreaker=false
    with pytest.raises(ValidationError):
        RubricConfig.model_validate(d)


def test_verdicts_missing_label_raises() -> None:
    d = _valid_rubric_dict()
    del d["aggregation"]["verdicts"]["needs_polish"]
    with pytest.raises(ValidationError):
        RubricConfig.model_validate(d)


def test_verdict_cuts_monotonic_check() -> None:
    d = _valid_rubric_dict()
    d["aggregation"]["verdict_cuts"]["qualified_overall_min"] = 4.0
    d["aggregation"]["verdict_cuts"]["needs_polish_overall_min"] = 5.0
    with pytest.raises(ValidationError):
        RubricConfig.model_validate(d)


def test_load_keywords_basic_shape() -> None:
    kws = load_keywords()
    assert "hook_keywords" in kws
    assert "cliffhanger_keywords" in kws
    assert "special_scene_keywords" in kws
    assert isinstance(kws["hook_keywords"], list)
    assert len(kws["hook_keywords"]) > 0


def test_load_archetype_library_cn() -> None:
    lib = load_archetype_library("archetypes_cn")
    assert "archetypes" in lib
    assert len(lib["archetypes"]) >= 10


def test_load_character_archetype_library_cn() -> None:
    lib = load_archetype_library("character_archetypes_cn")
    assert "archetypes" in lib
    assert len(lib["archetypes"]) >= 8
