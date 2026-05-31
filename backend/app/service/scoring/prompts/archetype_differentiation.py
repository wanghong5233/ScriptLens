"""ARCHETYPE 差异化 prompt。

业内出处：ReelShort comparable archetype 选品 SOP（"模板内的微差异" 优于 "完全创新"）。

注意：短剧"差异化"不是"越大越好"——必须是模板内的微差异。
LLM 输出 differentiation_quality 是连续值，取自"合理范围内的微创新"：
- 0.0 = 完全无差异化（直接抄爆款）
- 0.6-0.8 = 模板识别清晰 + 有微创新（理想区间）
- 1.0 = 模板识别清晰 + 强烈记忆点（更佳）
- 但若 archetype_recognizable=false（完全脱离模板），即使 originality 高也判低分。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


PROMPT_ID = "archetype_differentiation"


class ArchetypeDifferentiationPayload(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "archetype_recognizable": True,
                "originality_within_template": 0.7,
                "differentiation_quality": 0.75,
                "rationale": "穿越系统流模板清晰，但'多男主修罗场+系统任务'组合是同类内少见的微创新",
            }
        },
    )

    archetype_recognizable: bool = Field(description="模板是否清晰可辨识（1 秒识别）")
    originality_within_template: float = Field(
        ge=0.0, le=1.0, description="在模板内的微创新强度"
    )
    differentiation_quality: float = Field(
        ge=0.0, le=1.0, description="差异化综合分（结合 recognizable+originality 的判断）"
    )
    rationale: str = Field(max_length=200, description="≤100 字理由")

    @field_validator("archetype_recognizable", mode="before")
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

    @field_validator("originality_within_template", "differentiation_quality", mode="before")
    @classmethod
    def _coerce_float(cls, v: Any) -> Any:
        # 容错策略：仅处理"百分号"和"数字字符串"，不做"除以 10"猜测
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
判断"模板内的微差异"信号——短剧爆款的成功公式是"清晰模板 + 模板内微创新"，
完全脱离模板会让算法无法分发，完全抄袭则没有记忆点。

输出严格 JSON。"""


def build_prompt(
    archetype_hint: str | None,
    logline: str,
    synopsis: str,
    top_archetype_matches: list[str],
) -> tuple[str, str]:
    """archetype_hint: coverage_card 的 genre 第一项；
    top_archetype_matches: archetype_matcher 给出的 top 3 命中原型名（如"穿越系统流, 修罗场"）。
    """
    hint_section = ""
    if archetype_hint:
        hint_section = f"【题材标签】{archetype_hint}\n"
    matches_section = ""
    if top_archetype_matches:
        matches_section = "【已命中原型】" + " / ".join(top_archetype_matches[:3]) + "\n"

    user = f"""请评估这部剧本在短剧领域的"差异化"质量。

{hint_section}{matches_section}【一句话简介】{logline}

【全剧梗概】{synopsis}

【输出要求】
严格输出 JSON：
{{
  "archetype_recognizable": true 或 false,
  "originality_within_template": 0.0-1.0,
  "differentiation_quality": 0.0-1.0,
  "rationale": "≤100 字"
}}

【判断要点】
1. archetype_recognizable：用户 / 算法能否 1 秒识别这是哪类短剧（如"穿越系统"
   "豪门弃妇"）。能识别为 true。
2. originality_within_template：在该模板内是否有微创新或记忆点（如"穿越系统 + 多男主
   修罗场"是穿越系统类内少见组合）。完全抄袭为 0.0，强烈微创新为 1.0。
3. differentiation_quality：综合分。模板识别 + 适度微创新最佳（≈0.7-0.85）；
   纯抄爆款也能勉强成爆但易被淹没（≈0.4-0.5）；完全脱离模板（recognizable=false）
   即使 originality 高，也判 ≤ 0.3（算法分发不到目标人群）。
4. 不要输出任何 JSON 以外的内容"""
    return _SYSTEM_MESSAGE, user
