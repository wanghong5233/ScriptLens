"""HOOK 第一分钟 inciting incident 判定 prompt。

业内出处：ReelShort writer SOP《minute-1 inciting incident》、
抖音《短剧爆款公式 2024》§1（8 秒决策窗口 + 1 分钟点燃 incident）。

Pydantic schema 的 field_validator(mode='before') 与 beat_chain._BeatActLLM 同款，
处理 LLM 把 bool 输出成 0/1 / "true" 等常见错误。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


PROMPT_ID = "hook_first_minute"


class HookFirstMinutePayload(BaseModel):
    """LLM 输出 schema。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "incident_present": True,
                "incident_strength": 0.8,
                "rationale": "首场 30 秒内即出现穿越/系统觉醒事件，强 inciting incident",
            }
        },
    )

    incident_present: bool = Field(description="第一分钟内是否存在 inciting incident")
    incident_strength: float = Field(
        ge=0.0, le=1.0, description="强度 0=没有 / 0.5=弱 / 1.0=极强"
    )
    rationale: str = Field(max_length=160, description="≤80 中文字符的一句话理由")

    @field_validator("incident_present", mode="before")
    @classmethod
    def _coerce_bool(cls, v: Any) -> Any:
        # LLM 偶发把 bool 输出成 "true" / "false" / 0 / 1
        if isinstance(v, str):
            s = v.strip().lower()
            if s in {"true", "yes", "1"}:
                return True
            if s in {"false", "no", "0"}:
                return False
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return bool(v)
        return v

    @field_validator("incident_strength", mode="before")
    @classmethod
    def _coerce_strength(cls, v: Any) -> Any:
        # 容错策略（最小、不歧义）：
        # - "80%" → 0.8（百分号显式表达）
        # - "0.8" → 0.8（数字字符串）
        # - 数字 > 1 直接交给 le=1.0 校验拒绝，不做"除以 10"猜测（避免 1.5 被静默改成 0.15）
        if isinstance(v, str):
            s = v.strip()
            if s.endswith("%"):
                try:
                    return float(s.rstrip("%")) / 100.0
                except ValueError:
                    return v
            try:
                return float(s)
            except ValueError:
                return v
        return v


_SYSTEM_MESSAGE = """你是短剧爆款选品专家，给 AI 漫剧投资决策评分。
判断"第一分钟 inciting incident"信号——即剧本第一分钟内是否出现强引爆点（穿越 / 重生 /
背叛 / 死亡 / 系统觉醒 / 身份反转 / 强冲突等），决定用户是否会停留观看。

输出严格 JSON，schema 见用户消息。"""


def build_prompt(first_scenes_text: str) -> tuple[str, str]:
    """返回 (system_message, user_prompt)。

    first_scenes_text: 第一集前 1-2 场的文本（已经由调用方裁剪，避免 token 爆炸）
    """
    user = f"""请阅读下面这段剧本（第一集开场），判断第一分钟是否有 inciting incident。

【剧本片段】
{first_scenes_text}

【输出要求】
严格输出 JSON：
{{
  "incident_present": true 或 false,
  "incident_strength": 0.0-1.0,
  "rationale": "≤80 字一句话理由"
}}

【判断要点】
1. 仅看是否在剧本起始位置（约第一分钟内）出现强 inciting incident
2. 强 inciting incident 包括：穿越 / 重生 / 死亡 / 背叛 / 系统觉醒 / 身份反转 /
   强冲突（被退婚 / 当众羞辱 / 复仇启动）等
3. 普通"上班 / 上学 / 日常生活"的开场视为弱 / 无 inciting incident
4. 强度 0.0=没有；0.5=弱（仅暗示）；0.8=明确出现；1.0=多重 incident 叠加
5. 不要输出任何 JSON 以外的内容"""
    return _SYSTEM_MESSAGE, user
