"""scoring v4 框架级数据结构。

设计原则：
- 框架不引入任何阈值 / 关键词 / archetype 字面常量，所有这些都在 YAML rubric / library 里
- SignalResult / DimensionScore / ScoreVerdict / ScoringReport 是被 aggregator / dimension /
  provenance 共享的纯数据类型，零业务逻辑
- 仅引用上游 chain 数据类型（BeatSheet / RewardEvent / CharacterGraph / CoverageCard /
  ComplianceResult / Scene），不与上游链路逻辑耦合

参考：docs/2026-05-31-投资决策评分框架-v4.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from service.script_tools.beat_chain import BeatSheet
    from service.script_tools.character_graph_chain import CharacterGraph
    from service.script_tools.compliance_scorer import ComplianceResult
    from service.script_tools.coverage_chain import CoverageCard
    from service.script_tools.llm_caller import LlmCaller
    from service.script_tools.motivation_chain import MotivationResult
    from service.script_tools.reward_extractor import RewardEvent
    from service.script_tools.scene_repo import Scene


# ============================================================
# Signal / Dimension / Verdict 枚举
# ============================================================


class SignalStatus(str, Enum):
    """单个 signal 的计算状态。

    computed:        正常算出
    degraded:        算出但用了规则兜底（如 LLM 失败回落到规则）
    failed:          完全没算出，跳过该 signal（影响 coverage_ratio → confidence）
    not_applicable:  数据缺失但不可补（如剧本过短，不算作失败）
    """

    COMPUTED = "computed"
    DEGRADED = "degraded"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class SignalSource(str, Enum):
    """signal 的数据来源。与 rubric YAML 中 `source` 字段值一一对应。"""

    RULE = "rule"
    LLM_JUDGE = "llm_judge"
    HYBRID = "hybrid"


class TierLabel(str, Enum):
    """signal 分档锚点的 4 档（与 rubric tier_anchor 中 key 一一对应）。"""

    HIGH = "high"
    MID_HIGH = "mid_high"
    MID_LOW = "mid_low"
    LOW = "low"


class VerdictLabel(str, Enum):
    """Verdict 三档（与 rubric `verdicts` 字段 key 一一对应）。

    禁止使用 "invest" / "refine" 等历史 v3 表述。
    """

    QUALIFIED = "qualified"
    NEEDS_POLISH = "needs_polish"
    NOT_RECOMMENDED = "not_recommended"


class ConfidenceLabel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ============================================================
# 数据结构
# ============================================================


@dataclass
class SignalResult:
    """单个 signal 的计算结果。

    - score:           0.0-10.0，本 signal 在维度内的分数贡献（已经按 tier_anchor 映射）
    - raw_value:       原始度量值（如"前 30 字命中关键词率 0.67"）。仅用于可观测
    - status:          见 SignalStatus
    - source:          见 SignalSource
    - evidence_ref_ids:支持该 signal 判断的 scene_id / episode_no（前端用来高亮 / 跳转）
    - fallback_reason: 当 status != COMPUTED 时给出降级原因；前端不暴露给用户，仅写日志
                       与 `?debug=1` 视图
    """

    key: str
    source: SignalSource
    status: SignalStatus
    score: float
    raw_value: Optional[float] = None
    evidence_ref_ids: list[str] = field(default_factory=list)
    fallback_reason: Optional[str] = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source": self.source.value,
            "status": self.status.value,
            "score": round(self.score, 2),
            "raw_value": self.raw_value if self.raw_value is None else round(self.raw_value, 3),
            "evidence_ref_ids": list(self.evidence_ref_ids),
            "fallback_reason": self.fallback_reason,
            "detail": self.detail,
        }


@dataclass
class DimensionScore:
    """单维度评分结果。"""

    key: str
    score: float                                # 0.0-10.0，本维度聚合分（signals weighted sum）
    tier: TierLabel                             # 由 score 落档（aggregator 决定，不在这里硬编码）
    reason: str                                 # 给用户看的一句话
    signals: list[SignalResult] = field(default_factory=list)
    is_dealbreaker_triggered: bool = False
    evidence_ref_ids: list[str] = field(default_factory=list)
    top_improvement_hint: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "score": round(self.score, 2),
            "tier": self.tier.value,
            "reason": self.reason,
            "is_dealbreaker_triggered": self.is_dealbreaker_triggered,
            "evidence_ref_ids": list(self.evidence_ref_ids),
            "top_improvement_hint": self.top_improvement_hint,
            "signals": [s.to_dict() for s in self.signals],
        }


@dataclass
class ImprovementAction:
    """改写建议。给前端 top_improvements 用。"""

    title: str
    rationale: str
    expected_verdict_lift: Optional[str] = None          # 如 "needs_polish -> qualified"
    dimension_key: Optional[str] = None
    signal_key: Optional[str] = None
    evidence_ref_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "rationale": self.rationale,
            "expected_verdict_lift": self.expected_verdict_lift,
            "dimension_key": self.dimension_key,
            "signal_key": self.signal_key,
            "evidence_ref_ids": list(self.evidence_ref_ids),
        }


@dataclass
class ScoreVerdict:
    """聚合后的 Verdict（v4 用户最终决策结果）。"""

    label: VerdictLabel
    reason: str
    overall_score: Optional[float] = None             # 0.0-10.0；compliance 一票否决时可为 None
    confidence: ConfidenceLabel = ConfidenceLabel.MEDIUM
    compliance_tier: Optional[str] = None
    compliance_veto_triggered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label.value,
            "reason": self.reason,
            "overall_score": None if self.overall_score is None else round(self.overall_score, 2),
            "confidence": self.confidence.value,
            "compliance_tier": self.compliance_tier,
            "compliance_veto_triggered": self.compliance_veto_triggered,
        }


@dataclass
class ScoringReport:
    """scoring v4 主链最终输出。

    每个 generate_report 调用产生一个 ScoringReport，落入 scoring_runs 表
    （含 rubric_version）。
    """

    verdict: ScoreVerdict
    dimensions: list[DimensionScore]
    top_improvements: list[ImprovementAction]
    rubric_version: str
    coverage_ratio: float
    chain_status_records: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.to_dict(),
            "dimensions": [d.to_dict() for d in self.dimensions],
            "top_improvements": [a.to_dict() for a in self.top_improvements],
            "rubric_version": self.rubric_version,
            "coverage_ratio": round(self.coverage_ratio, 3),
            "chain_status_records": list(self.chain_status_records),
        }


# ============================================================
# ScoringContext —— 上游 chain 输出聚合传入 scoring
# ============================================================


@dataclass
class ScoringContext:
    """scoring 模块的输入聚合体。

    上游 chain 输出 + 元数据 + LLM caller 一次性传入，避免每个 dimension scorer
    各自读 DB / 各自实例化 LlmCaller。
    """

    script_id: str
    scenes: list["Scene"]
    total_episodes: int

    beat_sheet: Optional["BeatSheet"] = None
    reward_events: list["RewardEvent"] = field(default_factory=list)
    character_graph: Optional["CharacterGraph"] = None
    coverage_card: Optional["CoverageCard"] = None
    motivation_result: Optional["MotivationResult"] = None
    compliance: Optional["ComplianceResult"] = None

    llm_caller: Optional["LlmCaller"] = None

    def has_scenes(self) -> bool:
        return bool(self.scenes)
