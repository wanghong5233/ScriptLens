"""scoring v4 rubric / library 加载器。

设计要点（与 v3 score_registry 不同）：
- **零 default 兜底**：缺字段直接抛 RubricSchemaError，避免 silent degradation
- **Pydantic 强校验**：所有阈值字段、权重总和、tier_anchor 单调性、prompt_id 引用合法性
  都在加载期校验
- **lru_cache 在 version 维度**：测试可以加载多个 candidate rubric 做 AB
- **legacy dim_key 显式报错**：旧 6 维（story/character/concept/emotion/pacing/dialogue）
  调用方收到 RubricLegacyDimensionError，明确提示迁移路径
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

logger = logging.getLogger(__name__)


# ============================================================
# 异常
# ============================================================


class RubricSchemaError(RuntimeError):
    """rubric YAML 加载或 schema 校验失败。"""


class RubricLegacyDimensionError(ValueError):
    """调用方传入 v3 旧维度名（story / character / concept / emotion / pacing / dialogue）。

    显式抛错而不是 silent map，避免 v4 上线后 rewrite_chain 仍按旧名调用导致结果错乱。
    """


# ============================================================
# 路径
# ============================================================


_ROOT = Path(__file__).resolve().parent
_RUBRIC_DIR = _ROOT / "rubrics"
_LIBRARY_DIR = _ROOT / "libraries"
_KEYWORDS_DIR = _ROOT / "signals"

V3_LEGACY_DIMENSIONS: frozenset[str] = frozenset(
    {"story", "character", "concept", "emotion", "pacing", "dialogue"}
)

V4_DIMENSION_KEYS: frozenset[str] = frozenset(
    {"hook", "archetype", "payoff", "monetization", "producibility"}
)


# ============================================================
# Pydantic schema —— 与 YAML 字段 1:1 对齐
# ============================================================


class TierAnchorConfig(BaseModel):
    """signal raw_value 落档锚点。"""

    model_config = ConfigDict(extra="forbid")
    high: float = Field(ge=0.0)
    mid_high: float = Field(ge=0.0)
    mid_low: float = Field(ge=0.0)
    low: float = Field(ge=0.0)


class TierScoreConfig(BaseModel):
    """各档对应分数（0-10）。"""

    model_config = ConfigDict(extra="forbid")
    high: float = Field(ge=0.0, le=10.0)
    mid_high: float = Field(ge=0.0, le=10.0)
    mid_low: float = Field(ge=0.0, le=10.0)
    low: float = Field(ge=0.0, le=10.0)


class SignalConfig(BaseModel):
    """单个 signal 的配置。"""

    model_config = ConfigDict(extra="forbid")

    key: str
    source: str = Field(pattern=r"^(rule|llm_judge|hybrid)$")
    weight_in_dim: float = Field(gt=0.0, le=1.0)
    tier_anchor: TierAnchorConfig
    tier_scores: TierScoreConfig
    params: dict[str, Any] = Field(default_factory=dict)
    prompt_id: str | None = None
    reference: str = ""

    @model_validator(mode="after")
    def _check_llm_prompt(self) -> "SignalConfig":
        if self.source == "llm_judge" and not self.prompt_id:
            raise ValueError(f"signal {self.key!r} 声明 source=llm_judge 但缺少 prompt_id")
        return self


class DimensionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = ""  # 由父级填充
    weight: float = Field(gt=0.0, le=1.0)
    is_dealbreaker: bool
    label: str
    description: str = ""
    signals: list[SignalConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_signal_weights(self) -> "DimensionConfig":
        total = sum(s.weight_in_dim for s in self.signals)
        if not _close(total, 1.0):
            raise ValueError(
                f"dimension {self.key!r} signals.weight_in_dim 之和必须 = 1.0，当前 = {total:.4f}"
            )
        keys = [s.key for s in self.signals]
        if len(set(keys)) != len(keys):
            raise ValueError(f"dimension {self.key!r} 存在重复 signal.key: {keys}")
        return self


class VerdictDisplayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    display_cn: str
    display_en: str


class VerdictCutsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    qualified_overall_min: float = Field(ge=0.0, le=10.0)
    qualified_floor_min: float = Field(ge=0.0, le=10.0)
    needs_polish_overall_min: float = Field(ge=0.0, le=10.0)

    @model_validator(mode="after")
    def _check_monotonic(self) -> "VerdictCutsConfig":
        if self.qualified_overall_min < self.needs_polish_overall_min:
            raise ValueError(
                "verdict_cuts.qualified_overall_min 必须 >= needs_polish_overall_min"
            )
        return self


class AggregationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(pattern=r"^gated_multiplicative$")
    dealbreaker_dims: list[str] = Field(min_length=1)
    dealbreaker_threshold: float = Field(ge=0.0, le=10.0)
    dealbreaker_action: str = Field(pattern=r"^force_not_recommended$")
    verdict_cuts: VerdictCutsConfig
    verdicts: dict[str, VerdictDisplayConfig]

    @field_validator("verdicts")
    @classmethod
    def _check_verdict_labels(
        cls, v: dict[str, VerdictDisplayConfig]
    ) -> dict[str, VerdictDisplayConfig]:
        required = {"qualified", "needs_polish", "not_recommended"}
        if set(v.keys()) != required:
            raise ValueError(
                f"aggregation.verdicts 三档必须为 {sorted(required)}，当前 = {sorted(v.keys())}"
            )
        return v


class ComplianceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_independent_gate: bool
    veto_tier: str
    high_risk_action: str = Field(pattern=r"^veto$")


class ConfidenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    high_min_coverage: float = Field(gt=0.0, le=1.0)
    medium_min_coverage: float = Field(gt=0.0, le=1.0)
    max_llm_judge_failures_for_high: int = Field(ge=0)
    max_llm_judge_failures_for_medium: int = Field(ge=0)

    @model_validator(mode="after")
    def _check(self) -> "ConfidenceConfig":
        if self.high_min_coverage <= self.medium_min_coverage:
            raise ValueError(
                "confidence.high_min_coverage 必须 > medium_min_coverage"
            )
        return self


class TruncationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason_max_chars: int = Field(gt=0)
    evidence_excerpt_max_chars: int = Field(gt=0)
    improvement_rationale_max_chars: int = Field(gt=0)


class ImprovementPlannerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_actions: int = Field(gt=0, le=10)
    min_signal_score_to_recommend: float = Field(ge=0.0, le=10.0)
    expected_verdict_lift_template_cn: str
    # v4.1 多样性约束：每维度最多挑几条 improvement。0 / 负值 = 不约束（兼容旧 YAML）。
    # 默认 1 = 强制每条 improvement 来自不同维度，防止单一维度（典型：producibility
    # 因为多个低分 signal）霸占全部 top N 槽位。
    per_dimension_cap: int = Field(default=1, ge=0, le=10)


class DimensionTierCutsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    high: float = Field(ge=0.0, le=10.0)
    mid_high: float = Field(ge=0.0, le=10.0)
    mid_low: float = Field(ge=0.0, le=10.0)

    @model_validator(mode="after")
    def _check_monotonic(self) -> "DimensionTierCutsConfig":
        if not (self.high > self.mid_high > self.mid_low):
            raise ValueError(
                f"dimension_tier_cuts 必须严格单调递减：high={self.high} > "
                f"mid_high={self.mid_high} > mid_low={self.mid_low}"
            )
        return self


class RubricConfig(BaseModel):
    """完整 rubric 配置（YAML 1:1 映射）。"""

    model_config = ConfigDict(extra="forbid")

    version: str
    status: str = Field(pattern=r"^(active|deprecated|candidate)$")
    locale: str
    description: str = ""
    dimensions: dict[str, DimensionConfig]
    aggregation: AggregationConfig
    compliance: ComplianceConfig
    confidence: ConfidenceConfig
    truncation: TruncationConfig
    improvement_planner: ImprovementPlannerConfig
    dimension_tier_cuts: DimensionTierCutsConfig

    @model_validator(mode="after")
    def _wire_and_check(self) -> "RubricConfig":
        # 1) dimension key 必须严格匹配 V4 5 维（不许多不许少）
        dim_keys = set(self.dimensions.keys())
        if dim_keys != V4_DIMENSION_KEYS:
            raise ValueError(
                f"rubric dimensions 必须严格为 {sorted(V4_DIMENSION_KEYS)}，"
                f"当前 = {sorted(dim_keys)}"
            )

        # 2) 把 key 注入每个 DimensionConfig
        for key, dim in self.dimensions.items():
            dim.key = key

        # 3) dimensions 权重之和 = 1.0
        total_weight = sum(d.weight for d in self.dimensions.values())
        if not _close(total_weight, 1.0):
            raise ValueError(
                f"rubric dimensions weight 之和必须 = 1.0，当前 = {total_weight:.4f}"
            )

        # 4) dealbreaker_dims 必须都是合法 dim key
        for d in self.aggregation.dealbreaker_dims:
            if d not in self.dimensions:
                raise ValueError(
                    f"aggregation.dealbreaker_dims 包含未知 dim key={d!r}"
                )
            if not self.dimensions[d].is_dealbreaker:
                raise ValueError(
                    f"aggregation.dealbreaker_dims 包含 {d!r}，但 dimension.is_dealbreaker=false"
                )

        return self


# ============================================================
# 工具
# ============================================================


def _close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


# ============================================================
# Loader 入口
# ============================================================


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RubricSchemaError(f"rubric YAML 文件不存在：{path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise RubricSchemaError(f"rubric YAML 解析失败 path={path} err={e}") from e
    if not isinstance(data, dict):
        raise RubricSchemaError(f"rubric YAML 顶层必须是 mapping，path={path}")
    return data


@lru_cache(maxsize=8)
def load_rubric(version: str = "v4-cn-2026-05-31") -> RubricConfig:
    """加载并强校验 rubric。

    - 缺字段 / 类型错 / 权重和 != 1.0 → RubricSchemaError
    - 旧 v3 6 维 key 不在合法 V4 dim set 内 → 校验失败
    """
    # 当前 v4 只有一个 active rubric 文件；后续 overseas 加一个文件名映射即可
    if not version.startswith("v4-cn-"):
        raise RubricSchemaError(
            f"未知 rubric version={version!r}（v4 当前仅支持 cn locale）"
        )

    path = _RUBRIC_DIR / "cn_short_drama.yaml"
    data = _read_yaml(path)

    if data.get("version") != version:
        raise RubricSchemaError(
            f"rubric YAML 文件 version={data.get('version')!r} 不等于请求 version={version!r}"
        )

    try:
        cfg = RubricConfig.model_validate(data)
    except ValidationError as e:
        raise RubricSchemaError(f"rubric YAML schema 校验失败 path={path}: {e}") from e

    logger.info(
        "scoring.rubric_loader loaded version=%s dimensions=%s",
        cfg.version,
        list(cfg.dimensions.keys()),
    )
    return cfg


def assert_valid_v4_dimension(dim_key: str) -> None:
    """供 rewrite_chain / 报告链使用：禁止传入 v3 legacy 维度名。"""
    if dim_key in V3_LEGACY_DIMENSIONS:
        raise RubricLegacyDimensionError(
            f"维度 {dim_key!r} 是 v3 历史维度，v4 已废弃。"
            f"迁移指引：story → hook+payoff；character → archetype；"
            f"concept → archetype；emotion → payoff；pacing → hook+monetization；"
            f"dialogue → archetype/producibility 子信号。"
        )
    if dim_key not in V4_DIMENSION_KEYS:
        raise ValueError(
            f"未知维度 key={dim_key!r}，合法 v4 维度：{sorted(V4_DIMENSION_KEYS)}"
        )


# ============================================================
# 关键词 / archetype library 加载
# ============================================================


@lru_cache(maxsize=4)
def load_keywords() -> dict[str, Any]:
    """加载业务关键词集中库（signals/_keywords.yaml）。"""
    data = _read_yaml(_KEYWORDS_DIR / "_keywords.yaml")
    return data


@lru_cache(maxsize=8)
def load_archetype_library(library_name: str) -> dict[str, Any]:
    """加载题材 / 角色原型库。

    library_name 来自 rubric YAML signal.params.archetype_library 字段。
    """
    safe_name = library_name.replace("/", "").replace("..", "")
    path = _LIBRARY_DIR / f"{safe_name}.yaml"
    return _read_yaml(path)
