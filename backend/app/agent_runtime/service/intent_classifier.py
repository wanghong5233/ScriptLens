"""意图识别模块."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from ..config_loader import config_loader

logger = logging.getLogger(__name__)


class IntentType(str, Enum):
    QA = "qa"  # 纯问答
    SUGGEST = "suggest"  # 给出建议/检查
    EDIT = "edit"  # 修改/重写
    CITATION = "citation"  # 引用处理
    FILE_OP = "file_op"  # 文件操作
    UNKNOWN = "unknown"


@dataclass
class IntentClassificationResult:
    intent: IntentType
    confidence: float
    matched_keywords: List[str] = field(default_factory=list)
    matched_patterns: List[str] = field(default_factory=list)
    reason: Optional[str] = None


class RobustIntentClassifier:
    """多维度打分 + 否定检测的意图分类器。"""

    NEGATION_WORDS = [
        "不要",
        "不用",
        "无需",
        "别",
        "不需要",
        "不要去",
        "do not",
        "don't",
        "no ",
        "not ",
    ]

    def classify(
        self,
        user_intent: str,
        context: Optional[Dict[str, any]] = None,
    ) -> IntentClassificationResult:
        config = config_loader.load_intent_rules() or {}
        rules = config.get("rules", [])
        fallback_config = config.get("fallback") or {}
        fallback_intent = fallback_config.get("intent", IntentType.EDIT.value)
        fallback_type = self._safe_intent(fallback_intent)

        text = user_intent.strip()
        normalized = text.lower()
        has_selection = bool(context and context.get("selection", {}).get("text"))
        has_file_mentions = bool(context and context.get("file_mentions"))

        best_result = IntentClassificationResult(intent=fallback_type, confidence=0.0, reason=fallback_config.get("reason"))

        for rule in rules:
            intent = self._safe_intent(rule.get("intent"))
            score, matched_keywords, matched_patterns = self._score_rule(
                intent,
                normalized,
                text,
                rule,
                has_selection,
                has_file_mentions,
            )
            if score > best_result.confidence:
                best_result = IntentClassificationResult(
                    intent=intent,
                    confidence=score,
                    matched_keywords=matched_keywords,
                    matched_patterns=matched_patterns,
                    reason=rule.get("description"),
                )

        return best_result

    def _score_rule(
        self,
        intent: IntentType,
        normalized_text: str,
        raw_text: str,
        rule: Dict[str, any],
        has_selection: bool,
        has_file_mentions: bool,
    ) -> tuple[float, List[str], List[str]]:
        keywords = [kw.lower() for kw in rule.get("keywords", []) if kw]
        patterns = rule.get("patterns", [])

        matched_keywords: List[str] = []
        keyword_score = 0.0
        for keyword in keywords:
            if keyword in normalized_text and not self._has_negation(normalized_text, keyword):
                matched_keywords.append(keyword)
        if matched_keywords:
            keyword_score = min(len(matched_keywords) * 0.25, 0.6)

        matched_patterns: List[str] = []
        pattern_score = 0.0
        for pattern in patterns:
            try:
                if re.search(pattern, raw_text):
                    matched_patterns.append(pattern)
            except re.error as error:
                logger.warning("Invalid regex pattern '%s': %s", pattern, error)
        if matched_patterns:
            pattern_score = min(len(matched_patterns) * 0.25, 0.4)

        context_score = 0.0
        if intent == IntentType.EDIT and has_selection:
            context_score += 0.25
        if intent == IntentType.EDIT and has_file_mentions:
            # @file 引用代表明确的文件编辑上下文，提升编辑意图置信度。
            context_score += 0.22
        if intent == IntentType.QA and raw_text.rstrip().endswith(("?", "？")):
            context_score += 0.2
        if intent == IntentType.SUGGEST and not has_selection and "检查" in normalized_text:
            context_score += 0.15

        confidence = min(keyword_score + pattern_score + context_score, 1.0)
        return confidence, matched_keywords, matched_patterns

    def _safe_intent(self, value: Optional[str]) -> IntentType:
        if not value:
            return IntentType.UNKNOWN
        try:
            return IntentType(value)
        except ValueError:
            logger.warning("Unknown intent string in config: %s", value)
            return IntentType.UNKNOWN

    def _has_negation(self, text: str, keyword: str) -> bool:
        index = text.find(keyword)
        if index == -1:
            return False
        window_start = max(0, index - 6)
        window_text = text[window_start:index]
        return any(neg in window_text for neg in self.NEGATION_WORDS)


_classifier = RobustIntentClassifier()


def classify_intent(
    user_intent: str,
    context: Optional[Dict[str, any]] = None,
) -> IntentClassificationResult:
    """对外暴露的意图识别方法。"""
    return _classifier.classify(user_intent, context)


