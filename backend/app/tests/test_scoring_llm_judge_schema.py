"""scoring v4 LLM judge prompt schema 测试。

验证：
- field_validator(mode='before') 容错 coercion 工作（dict→bool / "80%"→0.8）
- minimal example 在 model_config.json_schema_extra 中存在（给 LlmCaller repair prompt 用）
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from service.scoring.prompts.archetype_differentiation import (
    ArchetypeDifferentiationPayload,
)
from service.scoring.prompts.hook_first_minute import HookFirstMinutePayload


# ============================================================
# HookFirstMinutePayload
# ============================================================


def test_hook_first_minute_basic() -> None:
    p = HookFirstMinutePayload(
        incident_present=True, incident_strength=0.8, rationale="测试"
    )
    assert p.incident_present is True
    assert abs(p.incident_strength - 0.8) < 1e-6


def test_hook_first_minute_coerce_bool_from_string() -> None:
    p = HookFirstMinutePayload.model_validate(
        {"incident_present": "true", "incident_strength": 0.5, "rationale": "x"}
    )
    assert p.incident_present is True
    p = HookFirstMinutePayload.model_validate(
        {"incident_present": "no", "incident_strength": 0.5, "rationale": "x"}
    )
    assert p.incident_present is False


def test_hook_first_minute_coerce_bool_from_int() -> None:
    p = HookFirstMinutePayload.model_validate(
        {"incident_present": 1, "incident_strength": 0.5, "rationale": "x"}
    )
    assert p.incident_present is True


def test_hook_first_minute_coerce_strength_from_percentage() -> None:
    p = HookFirstMinutePayload.model_validate(
        {"incident_present": True, "incident_strength": "80%", "rationale": "x"}
    )
    assert abs(p.incident_strength - 0.8) < 1e-6


def test_hook_first_minute_oversized_int_rejected() -> None:
    """LLM 输出 8 (out-of-10 scale)：不做 silent coerce，直接交给 le=1.0 拒绝。
    依赖 LlmCaller 内部的 repair retry 让 LLM 重新输出。
    """
    with pytest.raises(ValidationError):
        HookFirstMinutePayload.model_validate(
            {"incident_present": True, "incident_strength": 8, "rationale": "x"}
        )


def test_hook_first_minute_invalid_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        HookFirstMinutePayload.model_validate(
            {
                "incident_present": True,
                "incident_strength": 0.5,
                "rationale": "x",
                "extra_field": "evil",
            }
        )


def test_hook_first_minute_minimal_example_present() -> None:
    """LlmCaller 的 repair prompt 使用 model_json_schema().examples 给 LLM 看正确输出。"""
    schema = HookFirstMinutePayload.model_json_schema()
    # Pydantic 2.x 把 json_schema_extra 的 example 放在 schema 顶层
    assert "example" in schema or any(
        "example" in v for v in schema.get("properties", {}).values()
    )


# ============================================================
# ArchetypeDifferentiationPayload
# ============================================================


def test_archetype_differentiation_basic() -> None:
    p = ArchetypeDifferentiationPayload(
        archetype_recognizable=True,
        originality_within_template=0.6,
        differentiation_quality=0.7,
        rationale="OK",
    )
    assert p.archetype_recognizable is True


def test_archetype_differentiation_coerce_percent_string() -> None:
    p = ArchetypeDifferentiationPayload.model_validate(
        {
            "archetype_recognizable": True,
            "originality_within_template": "70%",
            "differentiation_quality": "75%",
            "rationale": "x",
        }
    )
    assert abs(p.originality_within_template - 0.7) < 1e-6
    assert abs(p.differentiation_quality - 0.75) < 1e-6


def test_archetype_differentiation_oversized_int_rejected() -> None:
    """同 hook：silent /10 coerce 风险更大；交给 le=1.0 + repair。"""
    with pytest.raises(ValidationError):
        ArchetypeDifferentiationPayload.model_validate(
            {
                "archetype_recognizable": True,
                "originality_within_template": 7,
                "differentiation_quality": 8,
                "rationale": "x",
            }
        )


def test_archetype_differentiation_oversize_str_decimal_no_percent() -> None:
    p = ArchetypeDifferentiationPayload.model_validate(
        {
            "archetype_recognizable": True,
            "originality_within_template": "0.65",
            "differentiation_quality": "0.85",
            "rationale": "x",
        }
    )
    assert abs(p.originality_within_template - 0.65) < 1e-6
    assert abs(p.differentiation_quality - 0.85) < 1e-6


def test_archetype_differentiation_out_of_range_rejected() -> None:
    # ge=0, le=1.0 → 1.5 不允许
    with pytest.raises(ValidationError):
        ArchetypeDifferentiationPayload(
            archetype_recognizable=True,
            originality_within_template=1.5,
            differentiation_quality=0.5,
            rationale="x",
        )
