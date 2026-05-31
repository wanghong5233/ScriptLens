"""scoring v4 — 单维 LLM-as-judge 统一入口（独立评分模块）。

设计要点（2026-05-31 LLM-first scoring 翻盘）：
- **第一性原理**：评分 = LLM 按 framework v4 doc 注入的 spec 给每维及每个子项独立打分，
  rule 不再参与小分计算。framework 5 维 + 子项的目的是给 LLM 提供更客观的思考框架。
- **复用既有 harness**：直接走 LlmCaller.call_json(validate_with=Schema)
  → 自动 schema 校验 + repair retry + provider/model fallback + trace/metric。
- **单一通用入口**：5 维共享同一段 prompt template + 同一个输出 schema；
  各维只是注入不同的 spec block（dimension_judge_spec.DIMENSION_JUDGE_SPECS）。
- **失败显式**：LLM 失败 → 整维 score=0、status=FAILED、reason 给用户友好文案；
  aggregator 自然走 dealbreaker 降档，不会假装给分。
- **零 rule 兜底**：本模块完全不算 raw / 不调 rule fallback；rule 时代的
  dimensions/{hook,archetype,...}.py 5 个文件本 commit 不删（避免一次改动过大），
  下个 commit 清理。
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from service.scoring.framework import (
    DimensionScore,
    ScoringContext,
    SignalResult,
    SignalSource,
    SignalStatus,
    TierLabel,
)
from service.scoring.prompts.dimension_judge_spec import (
    DIMENSION_JUDGE_SPECS,
    DIMENSION_LABELS_CN,
    GLOBAL_SCORING_DISCIPLINE,
)
from service.scoring.rubric_loader import (
    DimensionConfig,
    DimensionTierCutsConfig,
    SignalConfig,
)
from service.script_tools.llm_caller import (
    LlmCaller,
    ModelTier,
    ScoreLLMError,
    ScoreLLMSchemaError,
)

logger = logging.getLogger(__name__)


# ============================================================
# LLM 输出 schema（Pydantic）
# ============================================================


_VALID_TIERS = {"high", "mid_high", "mid_low", "low"}


class _LlmSignalOut(BaseModel):
    """单个 sub-signal 的 LLM 评分输出。"""

    model_config = ConfigDict(extra="ignore")

    key: str
    score: float = Field(ge=0.0, le=10.0)
    tier: str
    rationale: str
    evidence_excerpt: Optional[str] = None
    evidence_episode_no: Optional[int] = None
    evidence_scene_id: Optional[str] = None

    @field_validator("tier")
    @classmethod
    def _check_tier(cls, v: str) -> str:
        if v not in _VALID_TIERS:
            raise ValueError(
                f"tier 必须是 {sorted(_VALID_TIERS)} 之一，得到 {v!r}"
            )
        return v

    @field_validator("rationale")
    @classmethod
    def _check_rationale_nonempty(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("rationale 不能为空，必须给用户人话理由")
        return v


class _LlmDimensionVerdict(BaseModel):
    """单个 dimension 的 LLM 评分输出（聚合后的维度总分 + 子项明细）。"""

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "dimension_key": "hook",
                "dimension_score": 7.4,
                "dimension_tier": "mid_high",
                "dimension_reason": "首集首场冲突清晰、前 3 场钩子链稳定；集末留钩覆盖率中等，"
                                    "下一步可在 10-20 集集末加强 false_defeat 类反转",
                "signals": [
                    {
                        "key": "opening_30char_conflict",
                        "score": 8.5,
                        "tier": "high",
                        "rationale": "首集首场开场即为主角被未婚夫当众宣布悔婚的羞辱场，冲突在前 20 字内引爆，符合 8 秒决策窗口要求",
                        "evidence_excerpt": "「林婉清，今日宴上我宣布退婚」",
                        "evidence_episode_no": 1,
                        "evidence_scene_id": None,
                    },
                ],
            }
        },
    )

    dimension_key: str
    dimension_score: float = Field(ge=0.0, le=10.0)
    dimension_tier: str
    dimension_reason: str
    signals: list[_LlmSignalOut] = Field(min_length=1)

    @field_validator("dimension_tier")
    @classmethod
    def _check_tier(cls, v: str) -> str:
        if v not in _VALID_TIERS:
            raise ValueError(
                f"dimension_tier 必须是 {sorted(_VALID_TIERS)} 之一，得到 {v!r}"
            )
        return v

    @field_validator("dimension_reason")
    @classmethod
    def _check_reason(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("dimension_reason 不能为空")
        return v


# ============================================================
# Prompt 组装
# ============================================================


def _build_system_message(dim_key: str) -> str:
    spec = DIMENSION_JUDGE_SPECS.get(dim_key, "")
    if not spec:
        raise ValueError(f"dim_key={dim_key!r} 没有对应的 judge spec")
    return f"{GLOBAL_SCORING_DISCIPLINE}\n\n{spec}"


def _format_signal_config_table(dim_cfg: DimensionConfig) -> str:
    """把 yaml 里 dim 的子项配置（weight / tier_anchor / tier_scores）拼成
    一段表格文本，注入 prompt 让 LLM 知道每个子项打几分对应 high/mid_high。
    """
    lines = [
        "## 本维度子项配置（你必须为下表每个 key 打 1 个分数）",
    ]
    for sig in dim_cfg.signals:
        ts = sig.tier_scores
        lines.append(
            f"- key={sig.key} | weight_in_dim={sig.weight_in_dim:.2f} | "
            f"tier_scores: high={ts.high} mid_high={ts.mid_high} "
            f"mid_low={ts.mid_low} low={ts.low}"
        )
        if sig.reference:
            lines.append(f"    参考依据: {sig.reference}")
    return "\n".join(lines)


def _build_user_prompt(
    *,
    dim_key: str,
    dim_cfg: DimensionConfig,
    tier_cuts: DimensionTierCutsConfig,
    script_summary: str,
) -> str:
    label = DIMENSION_LABELS_CN.get(dim_key, dim_key)
    signal_keys = [s.key for s in dim_cfg.signals]
    return (
        f"# 任务：给当前剧本的 {label} 维度评分\n\n"
        f"{_format_signal_config_table(dim_cfg)}\n\n"
        f"## 维度落档表（dimension_score 落到哪一档）\n"
        f"- dimension_score >= {tier_cuts.high} → high\n"
        f"- dimension_score >= {tier_cuts.mid_high} → mid_high\n"
        f"- dimension_score >= {tier_cuts.mid_low} → mid_low\n"
        f"- 否则 → low\n\n"
        f"## 剧本素材（请基于此评分，不要凭空臆测）\n\n"
        f"{script_summary}\n\n"
        f"## 输出 JSON schema\n"
        f"严格输出 JSON 对象，字段如下，**不要 markdown 围栏 / 解释 / 前缀**：\n\n"
        "```\n"
        "{\n"
        f'  "dimension_key": "{dim_key}",\n'
        '  "dimension_score": 0-10 数字（按本维度子项 weighted sum 给出）,\n'
        '  "dimension_tier": "high|mid_high|mid_low|low"（按上表落档）,\n'
        '  "dimension_reason": "≤ 80 字一句话总结（结论 + 关键改进点）",\n'
        '  "signals": [\n'
        '    {"key": "<上表中的 key>", "score": 0-10, "tier": "high|mid_high|mid_low|low",\n'
        '     "rationale": "80-150 字人话，必须引用剧本桥段",\n'
        '     "evidence_excerpt": "≤ 80 字原文片段（可选）",\n'
        '     "evidence_episode_no": 集号（可选）,\n'
        '     "evidence_scene_id": "场景 id（可选）"}\n'
        "  ]\n"
        "}\n"
        "```\n\n"
        f"**必须为以下 key 全部各打 1 项**: {signal_keys}\n"
        "如果某个子项素材确实不足以判断，rationale 老实写"
        "「剧本素材不足以判断 XXX，建议补充 XXX」并给 low 档分数。"
    )


# ============================================================
# 主入口
# ============================================================


# Token 预算：本 judge 单次输出 ≈
#   dimension_reason ~80 字 + 6 signals × (rationale 150 + evidence 80 + meta 60) ≈ 1800 字
#   ≈ 1.5 × 1800 = 2700 token；safety 1.8 → 4096
_OUTPUT_TOKEN_BUDGET = 4096


async def score_dimension_via_llm(
    *,
    dim_key: str,
    ctx: ScoringContext,
    dim_cfg: DimensionConfig,
    tier_cuts: DimensionTierCutsConfig,
    script_summary: str,
) -> DimensionScore:
    """对单维度调 LLM judge 评分，输出 DimensionScore（含 signals 明细）。

    失败处理：
    - LLM 调用或 schema 校验失败 → 整维标 FAILED，score=0，reason 给用户友好文案。
    - LLM 返回的 signals 与 dim_cfg.signals 的 key 集合不一致 → 缺失的 key 补
      FAILED signal，多余的 key 直接忽略。
    """
    if ctx.llm_caller is None:
        return _fail_dimension(
            dim_key, dim_cfg, reason_for_user="评分服务未注入 LLM 调用器", fallback="ctx.llm_caller=None"
        )

    system_message = _build_system_message(dim_key)
    user_prompt = _build_user_prompt(
        dim_key=dim_key,
        dim_cfg=dim_cfg,
        tier_cuts=tier_cuts,
        script_summary=script_summary,
    )

    try:
        resp = await ctx.llm_caller.call_json(
            user_prompt,
            tier=ModelTier.PRIMARY,
            max_tokens=_OUTPUT_TOKEN_BUDGET,
            system_message=system_message,
            use_cache=False,
            validate_with=_LlmDimensionVerdict,
            chain_name=f"scoring.llm_dim_judge.{dim_key}",
        )
    except ScoreLLMSchemaError as e:
        logger.warning(
            "llm_dim_judge schema fail dim=%s err=%s",
            dim_key, _short_exc(e),
        )
        return _fail_dimension(
            dim_key, dim_cfg,
            reason_for_user="评分模型输出格式异常，本维度暂无法判断",
            fallback=f"ScoreLLMSchemaError: {_short_exc(e)}",
        )
    except ScoreLLMError as e:
        logger.warning(
            "llm_dim_judge llm fail dim=%s err=%s",
            dim_key, _short_exc(e),
        )
        return _fail_dimension(
            dim_key, dim_cfg,
            reason_for_user="评分模型调用失败，本维度暂无法判断",
            fallback=f"ScoreLLMError: {_short_exc(e)}",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("llm_dim_judge unexpected fail dim=%s", dim_key)
        return _fail_dimension(
            dim_key, dim_cfg,
            reason_for_user="评分链路出现未知异常，本维度暂无法判断",
            fallback=f"{type(e).__name__}: {_short_exc(e)}",
        )

    try:
        verdict = _LlmDimensionVerdict.model_validate(resp.parsed)
    except ValidationError as e:
        logger.warning(
            "llm_dim_judge post-validate fail dim=%s err=%s",
            dim_key, _short_exc(e),
        )
        return _fail_dimension(
            dim_key, dim_cfg,
            reason_for_user="评分模型输出未通过校验，本维度暂无法判断",
            fallback=f"ValidationError: {_short_exc(e)}",
        )

    return _verdict_to_dimension_score(verdict, dim_cfg)


# ============================================================
# 转换 / 失败构造
# ============================================================


def _verdict_to_dimension_score(
    verdict: _LlmDimensionVerdict,
    dim_cfg: DimensionConfig,
) -> DimensionScore:
    expected_keys = {s.key for s in dim_cfg.signals}
    sig_by_key: dict[str, _LlmSignalOut] = {}
    for sig in verdict.signals:
        if sig.key in expected_keys and sig.key not in sig_by_key:
            sig_by_key[sig.key] = sig

    signals: list[SignalResult] = []
    evidence_ref_ids: list[str] = []
    for cfg_sig in dim_cfg.signals:
        s = sig_by_key.get(cfg_sig.key)
        if s is None:
            signals.append(
                SignalResult(
                    key=cfg_sig.key,
                    source=SignalSource.LLM_JUDGE,
                    status=SignalStatus.FAILED,
                    score=0.0,
                    raw_value=None,
                    evidence_ref_ids=[],
                    fallback_reason="LLM 输出中未包含此子项",
                    detail="该子项剧本素材不足以判断",
                )
            )
            continue

        scene_evidences = [s.evidence_scene_id] if s.evidence_scene_id else []
        if scene_evidences:
            evidence_ref_ids.extend(scene_evidences)
        signals.append(
            SignalResult(
                key=cfg_sig.key,
                source=SignalSource.LLM_JUDGE,
                status=SignalStatus.COMPUTED,
                score=float(s.score),
                raw_value=None,
                evidence_ref_ids=scene_evidences,
                fallback_reason=None,
                detail=s.rationale.strip(),
            )
        )

    return DimensionScore(
        key=dim_cfg.key,
        score=float(verdict.dimension_score),
        tier=_tier_label(verdict.dimension_tier),
        reason=verdict.dimension_reason.strip(),
        signals=signals,
        is_dealbreaker_triggered=False,  # 由 main_chain 根据 aggregator threshold 再判
        evidence_ref_ids=evidence_ref_ids,
        top_improvement_hint=None,
    )


def _fail_dimension(
    dim_key: str,
    dim_cfg: DimensionConfig,
    *,
    reason_for_user: str,
    fallback: str,
) -> DimensionScore:
    """评分链路硬失败时构造的 DimensionScore：每个子项标 FAILED，整维 score=0。"""
    signals: list[SignalResult] = []
    for cfg_sig in dim_cfg.signals:
        signals.append(
            SignalResult(
                key=cfg_sig.key,
                source=SignalSource.LLM_JUDGE,
                status=SignalStatus.FAILED,
                score=0.0,
                raw_value=None,
                evidence_ref_ids=[],
                fallback_reason=fallback,
                detail="评分链路异常，未生成该子项判断",
            )
        )
    return DimensionScore(
        key=dim_key,
        score=0.0,
        tier=TierLabel.LOW,
        reason=reason_for_user,
        signals=signals,
        is_dealbreaker_triggered=False,
        evidence_ref_ids=[],
        top_improvement_hint=None,
    )


def _tier_label(s: str) -> TierLabel:
    try:
        return TierLabel(s)
    except ValueError:
        return TierLabel.LOW


def _short_exc(e: BaseException) -> str:
    msg = f"{type(e).__name__}: {e}"
    return msg if len(msg) <= 200 else msg[:200] + "…"


__all__ = ["score_dimension_via_llm"]
