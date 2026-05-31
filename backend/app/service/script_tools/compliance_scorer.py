from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from service.script_tools.llm_caller import LlmCaller
from service.script_tools.risk_screener import RiskResult, screen_risks


@dataclass
class ComplianceResult:
    status: str
    level: str
    score: int
    tier: str
    confidence: str
    reason: str
    evidence_ref_ids: list[str]
    hits: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "level": self.level,
            "score": self.score,
            "tier": self.tier,
            "confidence": self.confidence,
            "reason": self.reason,
            "evidence_ref_ids": list(self.evidence_ref_ids),
            "hits": list(self.hits),
        }

    @classmethod
    def empty(cls) -> "ComplianceResult":
        """W1.8 (2026-05-31)：合规扫描失败时的占位结果。

        前端 / scorer 拿到的对象保持同样 shape，但 status="insufficient" + tier
        让 dimension_scorer 知道这是降级数据、不应计入合规分。
        """
        return cls(
            status="insufficient",
            level="insufficient",
            score=0,
            tier="insufficient",
            confidence="low",
            reason="合规扫描 LLM 失败，已降级为空报告",
            evidence_ref_ids=[],
            hits=[],
        )


def _status_from_level(level: str) -> str:
    if level == "high_risk":
        return "blocked"
    if level in {"medium_risk", "low_risk"}:
        return "warn"
    return "pass"


def _tier_from_score(score: int) -> str:
    if score >= 8:
        return "excellent"
    if score >= 6:
        return "good"
    if score >= 4:
        return "weak"
    if score > 0:
        return "poor"
    return "insufficient"


def _confidence_from_level(level: str) -> str:
    if level == "clean":
        return "high"
    if level == "low_risk":
        return "medium"
    return "low"


async def screen_compliance(
    *,
    script_id: str,
    caller: LlmCaller | None = None,
) -> ComplianceResult:
    risk: RiskResult = await screen_risks(script_id=script_id, caller=caller)
    hits_payload = [
        {
            "scene_id": hit.scene_id,
            "scene_no": hit.scene_no,
            "episode_no": hit.episode_no,
            "level": hit.level,
            "category": hit.category,
            "matched_term": hit.matched_term,
            "confirmed_by_llm": hit.confirmed_by_llm,
            "evidence_line_range": list(hit.evidence_line_range) if hit.evidence_line_range else None,
            "excerpt": hit.excerpt,
            "quote_verified": hit.quote_verified,
        }
        for hit in risk.hits
    ]
    return ComplianceResult(
        status=_status_from_level(risk.level),
        level=risk.level,
        score=risk.score,
        tier=_tier_from_score(risk.score),
        confidence=_confidence_from_level(risk.level),
        reason=risk.reason,
        evidence_ref_ids=list(risk.evidence_ref_ids),
        hits=hits_payload,
    )
