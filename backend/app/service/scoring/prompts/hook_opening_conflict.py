"""HOOK 开场冲突判读 prompt（hybrid 信号的 LLM 兜底）。

业内出处：
- 抖音《短剧爆款公式 2024》§1：8 秒决策窗
- 字节 WebConf 2026 *Short Drama Quality Assessment*：keyword-only rule 在
  开场冲突判定上 FN（False Negative）率 25-40%，必须叠加 LLM judge 才能稳。
- G-Eval (Liu et al, EMNLP 2023)：CoT-style LLM judge 在二分类 + 强度评分
  任务上与人类一致率 80%+，显著高于规则关键词匹配。

用法：
本 prompt 用于 hybrid 信号 opening_30char_conflict 的 **LLM 兜底**——
当规则关键词在首场首 N 字未命中时，调用 LLM 判读"首场前 1 分钟实际剧情
是否存在强冲突 / 强情绪 / 反常事件"。

不允许直接作为主判（首场已命中关键词时不调 LLM，省 token）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


PROMPT_ID = "hook_opening_conflict"


class HookOpeningConflictPayload(BaseModel):
    """LLM 输出 schema。

    conflict_present:    首场实际剧情是否有"足以让用户停下"的强冲突/反常
    conflict_strength:   0.0-1.0；0=没有，0.5=有但弱，0.8=强，1.0=极强
    conflict_type:       冲突类型简短标签（"羞辱" / "背叛" / "穿越" / "意外死亡" 等）
    rationale:           ≤80 中文字符理由
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "conflict_present": True,
                "conflict_strength": 0.8,
                "conflict_type": "系统觉醒+身份反转",
                "rationale": "首场即触发系统觉醒，主角直接被系统告知身份反转为反派",
            }
        },
    )

    conflict_present: bool = Field(description="首场是否存在足以让用户停下的强冲突/反常")
    conflict_strength: float = Field(
        ge=0.0, le=1.0, description="0=没有 / 0.5=弱 / 0.8=明确 / 1.0=极强"
    )
    conflict_type: str = Field(max_length=40, description="冲突类型简短标签")
    rationale: str = Field(max_length=160, description="≤80 中文字符理由")

    @field_validator("conflict_present", mode="before")
    @classmethod
    def _coerce_bool(cls, v: Any) -> Any:
        if isinstance(v, str):
            s = v.strip().lower()
            if s in {"true", "yes", "1"}:
                return True
            if s in {"false", "no", "0"}:
                return False
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return bool(v)
        return v

    @field_validator("conflict_strength", mode="before")
    @classmethod
    def _coerce_strength(cls, v: Any) -> Any:
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
判断"开场冲突"信号——即剧本首场（约 30-60 秒）是否存在"足以让用户在 8 秒内
停下不划走"的强冲突 / 强情绪 / 反常事件。这是抖音 / 快手 / ReelShort 短剧
SOP 的第一性指标。

输出严格 JSON，schema 见用户消息。"""


def build_prompt(first_scene_text: str) -> tuple[str, str]:
    """first_scene_text: 第一集首场场景的全文（已由调用方裁剪到合理长度）。"""
    user = f"""请阅读下面这段剧本（第一集首场），判断开场是否有强冲突。

【剧本片段】
{first_scene_text}

【输出要求】
严格输出 JSON：
{{
  "conflict_present": true 或 false,
  "conflict_strength": 0.0-1.0,
  "conflict_type": "≤20 字简短标签",
  "rationale": "≤80 字理由"
}}

【判断要点】
1. "强冲突 / 强情绪 / 反常事件" 包括但不限于：
   - 强情绪激发：羞辱 / 背叛 / 当众否定 / 弑亲 / 复仇启动
   - 反常事件：穿越 / 重生 / 系统觉醒 / 身份反转 / 角色死而复生
   - 命运转折：被退婚 / 被夺权 / 突遭家变 / 突生异能
2. 普通"日常上班 / 上学 / 起床 / 早餐"等开场为弱 / 无强冲突。
3. conflict_strength 校准：
   - 0.0 = 完全无冲突（纯日常 / 介绍）
   - 0.5 = 暗示有冲突但未爆发（如旁白预告 / 角色焦虑独白）
   - 0.8 = 明确出现单一强冲突
   - 1.0 = 多重强冲突叠加（如"穿越 + 身份反转 + 被追杀"）
4. 不要输出任何 JSON 以外的内容。
5. 即使首场没有命中你预期的关键词（重生 / 穿越等），只要剧情本身有强情绪/反常事件，
   也算 conflict_present=true——本判断不依赖关键词字面匹配。"""
    return _SYSTEM_MESSAGE, user
