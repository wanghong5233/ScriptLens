"""任务计划构建模块."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..config_loader import config_loader
from .intent_classifier import IntentType


@dataclass
class TaskPlan:
    intent: IntentType
    steps: List[str]
    notes: List[str] = field(default_factory=list)
    max_iterations: Optional[int] = None

    @property
    def is_empty(self) -> bool:
        return not self.steps


class DynamicPlanBuilder:
    """根据配置和上下文动态生成任务计划。"""

    def __init__(self) -> None:
        self._config = config_loader.load_plan_strategy() or {}

    def build(
        self,
        intent: IntentType,
        context_info: Optional[Dict[str, object]] = None,
    ) -> TaskPlan:
        context = context_info or {}
        strategies = self._config.get("strategies") or {}
        strategy = strategies.get(intent.value)
        notes: List[str] = []

        if not strategy:
            notes.append("未找到匹配的策略，进入自由决策模式")
            return TaskPlan(intent=intent, steps=[], notes=notes)

        if strategy.get("notes"):
            notes.append(strategy["notes"])

        selected_steps: List[str] = []

        for step in strategy.get("tool_sequence", []):
            tool_name = step.get("tool")
            if not tool_name:
                continue

            condition = step.get("condition")
            condition_passed = self._eval_condition(condition, context)

            if condition is not None and not condition_passed:
                notes.append(f"跳过 {tool_name}：条件 {condition} 不满足")
                continue

            if tool_name in selected_steps:
                notes.append(f"跳过重复工具 {tool_name}：已在计划中")
                continue
            selected_steps.append(tool_name)

        max_iterations = strategy.get("max_iterations")

        return TaskPlan(
            intent=intent,
            steps=selected_steps,
            notes=notes,
            max_iterations=max_iterations,
        )

    def _eval_condition(self, condition: Optional[str], context: Dict[str, object]) -> bool:
        if not condition:
            return True

        condition = condition.strip()
        if not condition:
            return True

        # 支持简单布尔表达式：
        # - A || B
        # - A && B
        # - !A
        # 不支持括号，按 || -> && -> 原子条件递归求值。
        if "||" in condition:
            return any(
                self._eval_condition(part.strip(), context)
                for part in condition.split("||")
                if part.strip()
            )
        if "&&" in condition:
            return all(
                self._eval_condition(part.strip(), context)
                for part in condition.split("&&")
                if part.strip()
            )

        if condition.startswith("!"):
            return not self._eval_condition(condition[1:], context)

        value_map = {
            "has_selection": bool(context.get("has_selection")),
            "has_file_mentions": bool(context.get("has_file_mentions")),
            "has_kb": bool(context.get("has_kb")),
            "wants_directory_create": bool(context.get("wants_directory_create")),
            "wants_file_create": bool(context.get("wants_file_create")),
            "wants_move_rename": bool(context.get("wants_move_rename")),
            "wants_delete_path": bool(context.get("wants_delete_path")),
            "selection_length": int(context.get("selection_length") or 0),
            "workspace_file_count": int(context.get("workspace_file_count") or 0),
            "intent_confidence": float(context.get("intent_confidence") or 0.0),
        }

        if condition in (
            "has_selection",
            "has_file_mentions",
            "has_kb",
            "wants_directory_create",
            "wants_file_create",
            "wants_move_rename",
            "wants_delete_path",
        ):
            return value_map[condition]

        match = re.match(r"(selection_length|workspace_file_count|intent_confidence)\s*([<>]=?|==)\s*([\d\.]+)", condition)
        if match:
            key, operator, threshold = match.groups()
            value = value_map.get(key)
            if value is None:
                return False
            try:
                threshold_value = float(threshold) if key == "intent_confidence" else int(float(threshold))
            except ValueError:
                return False
            return self._compare(value, operator, threshold_value)

        return False

    def _compare(self, value: float, operator: str, threshold: float) -> bool:
        if operator == ">":
            return value > threshold
        if operator == ">=":
            return value >= threshold
        if operator == "<":
            return value < threshold
        if operator == "<=":
            return value <= threshold
        if operator == "==":
            return value == threshold
        return False


def build_plan(
    intent: IntentType,
    *,
    context_info: Optional[Dict[str, object]] = None,
) -> TaskPlan:
    """快捷函数，构建任务计划。"""
    builder = DynamicPlanBuilder()
    return builder.build(intent, context_info=context_info)

