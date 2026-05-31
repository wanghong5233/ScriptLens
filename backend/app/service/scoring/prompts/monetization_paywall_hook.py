"""MONETIZATION 付费拐点钩子强度判读 prompt（LLM-as-judge 主判）。

业内出处：
- 抖音 / 快手 / ReelShort 短剧产品白皮书：付费拐点（典型 15-20 集末）的
  "钩子强度"直接决定首付费率（即 free→paid 转化率），是 ROI 第一性指标。
- 字节 WebConf 2026 *Short Drama Quality Assessment*：纯关键词匹配在付费点
  钩子强度上 FN 率 35%+（很多优秀钩子用情境而非显式"反转/危机"等词）。

为什么不再纯靠 keyword match（`paywall_cliffhanger_strength` rule）：
- 200 字 tail_window 内有"反转 / 阴谋 / 危机"等关键词 → +分 — 但这只是字面命中
- 真正决定付费的是"用户读到这里有没有'必须看下集'的心理状态"，是质量判断
- 例：付费拐点处主角"在医院被推进手术室，外面妻子哭泣"——一个 "反转" 词都没有
  但用户必然付费续看；keyword rule 给 0 分，LLM judge 才能看出是强钩

本 signal 与 paywall_cliffhanger_strength 互补：
- rule：客观关键词匹配（可解释、零成本）
- LLM judge：质量判断（情境钩子的实际转化力）
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


PROMPT_ID = "monetization_paywall_hook"


class PaywallHookQualityPayload(BaseModel):
    """LLM 输出 schema。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "hook_strength": 0.85,
                "creates_curiosity_gap": True,
                "emotional_stakes_clear": True,
                "rationale": "付费拐点处主角被冤入狱，仇人当众羞辱，下集必看真相揭露",
            }
        },
    )

    hook_strength: float = Field(
        ge=0.0, le=1.0,
        description="付费点钩子的'让人必须付费续看'强度",
    )
    creates_curiosity_gap: bool = Field(
        description="是否制造了明确的'信息缺口'让用户必须解开"
    )
    emotional_stakes_clear: bool = Field(
        description="情感利害关系是否清晰（主角处于强情绪/强危险状态）"
    )
    rationale: str = Field(max_length=200, description="≤100 字理由")

    @field_validator("creates_curiosity_gap", "emotional_stakes_clear", mode="before")
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

    @field_validator("hook_strength", mode="before")
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


_SYSTEM_MESSAGE = """你是短剧付费转化专家，给 AI 漫剧投资决策评分。
判断"付费拐点钩子强度"信号——即剧本在免费→付费拐点（典型第 15-20 集末）
处，是否构造了让用户"必须付费续看"的钩子。

注意：不是看关键词匹配，是看叙述本身的"心理诱导力"。
情境钩子（如"主角被推进手术室妻子在外哭泣"）可能没"反转 / 危机"等词，
但用户必然付费续看；反之，写满"惊天反转"但无情境冲击的也不算强钩。

输出严格 JSON。"""


def build_prompt(
    *,
    paywall_episode: int,
    paywall_scene_excerpt: str,
    next_scene_excerpt: str = "",
) -> tuple[str, str]:
    """
    paywall_episode: 付费拐点集号
    paywall_scene_excerpt: 拐点集末场景全文（已裁剪）
    next_scene_excerpt: 付费首集首场预览（可选，让 LLM 看到悬念是否值得付费）
    """
    next_section = ""
    if next_scene_excerpt.strip():
        next_section = (
            f"\n\n【付费首集首场预览】\n{next_scene_excerpt}\n"
            "（仅供判断悬念是否落实，不影响付费转化判定）"
        )
    user = f"""请评估这部剧本付费拐点的"钩子强度"。

【付费拐点位置】第 {paywall_episode} 集末（典型短剧免费→付费切换点）

【拐点集末场景】
{paywall_scene_excerpt}
{next_section}

【输出要求】
严格输出 JSON：
{{
  "hook_strength": 0.0-1.0,
  "creates_curiosity_gap": true 或 false,
  "emotional_stakes_clear": true 或 false,
  "rationale": "≤100 字"
}}

【判断要点】
1. hook_strength 校准（"用户是否愿意付费续看"）：
   - 0.0 = 无钩子 / 平淡收尾
   - 0.3 = 暗示有事但不紧迫
   - 0.5 = 有钩子但弱
   - 0.7 = 强钩子（主角处于危机 / 强冲突 / 真相即将揭露）
   - 0.85 = 极强（多重悬念叠加 / 主角生死边缘）
   - 1.0 = 顶级（必看到下一秒才能解开的死亡 / 反转 / 真相）
2. creates_curiosity_gap：是否制造了明确的"信息缺口"
   （用户必须知道"接下来到底发生了什么"才能心安）
3. emotional_stakes_clear：情感利害是否清晰
   （主角处于强情绪 / 强危险 / 强不公的状态）
4. 不依赖关键词字面匹配；情境钩子（如手术室场景）可能没"反转/危机"等词但极强
5. 不要输出任何 JSON 以外的内容"""
    return _SYSTEM_MESSAGE, user
