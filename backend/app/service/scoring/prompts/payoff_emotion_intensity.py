"""PAYOFF 爽感强度判读 prompt（LLM-as-judge 主判）。

业内出处：
- 抖音《短剧爆款公式 2024》§4：爽点的"强度 + 节奏"决定完播率，不只是"密度"
- ReelShort writer SOP《Reward design》：reward 不是计数，是用户的"上头瞬间"
- G-Eval (Liu et al, EMNLP 2023)：质量类信号 LLM judge 显著优于规则计数

为什么不再纯靠 reward_extractor 计数：
- reward_extractor 的密度 / 反转密度 / 干涸段是"分布统计"，本身正确
- 但"爽感"还包含：单次爽点的强度 / 反差幅度 / 是否落到主角主线
- 这些是质量维度，无法用计数表达，必须 LLM judge

判读区间：
- 0.0 = 几乎没有爽点 / 都是流水账
- 0.5 = 有爽点但分布散漫、强度普通
- 0.7-0.85 = 爽点强且节奏稳，有打脸 / 反转 / 复仇主线
- 1.0 = 顶级爆款级（强爽 + 紧密节奏 + 主线驱动）

本 signal 与 reward_density_per_episode 互补，不冲突：
- 密度规则：客观计数（reward_extractor 抽取的关键词事件）
- 强度 LLM：主观质量（同样 N 个爽点，强度可能差异 3 倍）
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


PROMPT_ID = "payoff_emotion_intensity"


class PayoffEmotionIntensityPayload(BaseModel):
    """LLM 输出 schema。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "intensity_score": 0.75,
                "has_strong_payoff_arc": True,
                "main_arc_driven": True,
                "rationale": "全剧打脸 / 复仇 / 身份揭露集中在主线，平均每 3 集一次强爽点",
            }
        },
    )

    intensity_score: float = Field(
        ge=0.0, le=1.0,
        description="爽感叙述强度综合分（结合强度+节奏+主线驱动）",
    )
    has_strong_payoff_arc: bool = Field(
        description="是否存在贯穿全剧的强 payoff 主弧（如复仇 / 打脸 / 翻盘）"
    )
    main_arc_driven: bool = Field(
        description="爽点是否落在主角主线上（vs 散落在配角/支线）"
    )
    rationale: str = Field(max_length=200, description="≤100 字理由")

    @field_validator("has_strong_payoff_arc", "main_arc_driven", mode="before")
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

    @field_validator("intensity_score", mode="before")
    @classmethod
    def _coerce_score(cls, v: Any) -> Any:
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
判断"爽感叙述强度"信号——不是计数爽点有多少，而是质量判断：
1. 单次爽点的"强度"够不够（打脸有多狠 / 反转有多大）
2. 爽点节奏是否稳（不会平铺直叙也不会爆点扎堆）
3. 爽点是否驱动主线（vs 配角/支线无关爽点）

输出严格 JSON。"""


def build_prompt(
    *,
    logline: str,
    synopsis: str,
    reward_sample_excerpts: list[str],
    total_episodes: int,
    reward_count: int,
) -> tuple[str, str]:
    """
    reward_sample_excerpts: 已抽取的 3-5 个高密度 reward 周边场景文本（已裁剪）。
    本 prompt 不是要 LLM 阅读全剧（成本爆炸），而是给"概览 + 关键采样"做质量判读。
    """
    samples_text = ""
    if reward_sample_excerpts:
        samples_text = "\n\n".join(
            f"【爽点采样 {i + 1}】\n{ex}" for i, ex in enumerate(reward_sample_excerpts)
        )

    user = f"""请评估这部剧本的"爽感叙述强度"。

【一句话简介】{logline}

【全剧梗概】{synopsis}

【全剧统计】共 {total_episodes} 集，规则抽取爽点事件 {reward_count} 条

{samples_text}

【输出要求】
严格输出 JSON：
{{
  "intensity_score": 0.0-1.0,
  "has_strong_payoff_arc": true 或 false,
  "main_arc_driven": true 或 false,
  "rationale": "≤100 字"
}}

【判断要点】
1. intensity_score 校准：
   - 0.0 = 几乎没爽点 / 都是流水账
   - 0.3 = 偶有爽点但弱、节奏散
   - 0.5 = 有爽点但强度普通
   - 0.7 = 爽点强且节奏较稳，有打脸 / 反转 / 复仇等主流爆款级桥段
   - 0.85 = 爽点强 + 节奏稳 + 主线驱动
   - 1.0 = 顶级爆款级
2. has_strong_payoff_arc：是否有"贯穿全剧的强 payoff 主弧"，如：
   - 复仇 / 打脸 / 翻盘 / 真相揭露 / 系统升级
3. main_arc_driven：爽点是否绑定到主角主线（vs 散落在无关支线）
4. 不依赖 reward 计数，看叙述质量本身
5. 不要输出任何 JSON 以外的内容"""
    return _SYSTEM_MESSAGE, user
