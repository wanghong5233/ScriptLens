"""配置文件校验逻辑。"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field, field_validator

ALLOWED_INTENTS = {"qa", "suggest", "edit", "citation", "file_op", "unknown"}


class IntentRuleModel(BaseModel):
    intent: str = Field(..., description="意图类型")
    description: str | None = None
    keywords: List[str] = Field(default_factory=list)
    patterns: List[str] = Field(default_factory=list)
    examples: List[str] = Field(default_factory=list)

    @field_validator("intent")
    @classmethod
    def validate_intent(cls, value: str) -> str:
        if value not in ALLOWED_INTENTS:
            raise ValueError(f"Unsupported intent type: {value}")
        return value


class IntentRulesConfig(BaseModel):
    version: str = "1.0"
    rules: List[IntentRuleModel]
    fallback: dict | None = None

    @field_validator("rules")
    @classmethod
    def validate_rules(cls, value: List[IntentRuleModel]) -> List[IntentRuleModel]:
        if not value:
            raise ValueError("At least one intent rule is required")
        return value


def validate_intent_rules(payload: dict) -> IntentRulesConfig:
    """校验意图识别配置。"""
    return IntentRulesConfig.model_validate(payload)


class ToolStepModel(BaseModel):
    tool: str
    optional: bool = False
    condition: str | None = None


class StrategyModel(BaseModel):
    description: str | None = None
    tool_sequence: list[ToolStepModel] = Field(default_factory=list)
    notes: str | None = None
    max_iterations: int | None = Field(default=None, ge=1)


class PlanStrategyConfig(BaseModel):
    version: str = "1.0"
    strategies: dict[str, StrategyModel]


def validate_plan_strategy(payload: dict) -> PlanStrategyConfig:
    """校验计划策略配置。"""
    return PlanStrategyConfig.model_validate(payload)
