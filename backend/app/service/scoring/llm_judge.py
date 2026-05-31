"""scoring v4 LLM-as-judge 统一入口。

设计要点（复用过往踩坑经验）：
- **强制 use_cache=False**：评分不接受脏 cache 污染；每次重算（同 dimension_scorer
  对照，scoring_runs 表本身就是历史快照）
- **强制 validate_with=PydanticSchema**：所有 prompt 必须配套 schema；schema 用
  field_validator(mode="before") coerce dict→list（复用 beat_chain W2.x 经验）
- **trace_id 贯穿**：调用方传入 chain trace_id；不传则本层生成
- **失败显式返回 None**：上层 dimension scorer 根据 None 把 signal 标 FAILED；
  不允许 silent fallback 让 score 看似正常但其实是 0

参考：docs/2026-05-31-llm-schema-harness.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Type

from pydantic import BaseModel, ValidationError

from service.script_tools.llm_caller import (
    LlmCaller,
    ModelTier,
    ScoreLLMError,
    ScoreLLMSchemaError,
    TokenBudget,
)

logger = logging.getLogger(__name__)


@dataclass
class JudgeResult:
    """LLM judge 调用结果。"""

    parsed: Optional[BaseModel]
    raw_text: str
    success: bool
    error: Optional[str] = None
    trace_id: Optional[str] = None


async def judge_with_schema(
    *,
    caller: LlmCaller,
    prompt: str,
    schema: Type[BaseModel],
    system_message: Optional[str] = None,
    max_tokens: int = TokenBudget.DECISION_JUDGE,
    tier: str = ModelTier.MINI,
    chain_name: str = "scoring.llm_judge",
    trace_id: Optional[str] = None,
) -> JudgeResult:
    """调用 LLM judge 并强校验 Pydantic schema。

    - 命中 schema 失败 → LlmCaller 内部走一次 repair retry（W2.1）
    - repair 后仍失败 → 返回 success=False，调用方把对应 signal 标 FAILED
    - 不缓存（scoring 不接受 cache）
    """
    try:
        resp = await caller.call_json(
            prompt,
            tier=tier,
            max_tokens=max_tokens,
            system_message=system_message,
            use_cache=False,
            validate_with=schema,
            trace_id=trace_id,
            chain_name=chain_name,
        )
    except ScoreLLMSchemaError as e:
        logger.warning(
            "scoring.llm_judge schema fail chain=%s schema=%s err=%s",
            chain_name,
            schema.__name__,
            _short(e),
        )
        return JudgeResult(parsed=None, raw_text="", success=False, error=str(e), trace_id=trace_id)
    except ScoreLLMError as e:
        logger.warning(
            "scoring.llm_judge llm fail chain=%s err=%s",
            chain_name,
            _short(e),
        )
        return JudgeResult(parsed=None, raw_text="", success=False, error=str(e), trace_id=trace_id)
    except Exception as e:  # noqa: BLE001
        logger.exception(
            "scoring.llm_judge unexpected fail chain=%s",
            chain_name,
        )
        return JudgeResult(parsed=None, raw_text="", success=False, error=str(e), trace_id=trace_id)

    try:
        parsed = schema.model_validate(resp.parsed)
    except ValidationError as e:
        # 理论上 LlmCaller 内部已 validate；此处兜底
        logger.warning(
            "scoring.llm_judge post-validate fail chain=%s err=%s",
            chain_name,
            _short(e),
        )
        return JudgeResult(
            parsed=None, raw_text=resp.raw, success=False, error=str(e), trace_id=resp.trace_id
        )

    return JudgeResult(
        parsed=parsed,
        raw_text=resp.raw,
        success=True,
        trace_id=resp.trace_id,
    )


def _short(e: BaseException) -> str:
    msg = str(e)
    if len(msg) > 160:
        return msg[:160] + "…"
    return msg


__all__ = ["JudgeResult", "judge_with_schema"]
