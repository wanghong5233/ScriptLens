"""Coverage Card 抽取：30 秒决策层。

借鉴 studio coverage：logline + recommendation + strengths/concerns。
这层回答 task.md 的「值不值得继续看 / 核心价值 / 问题风险」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.engine import Engine

from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError
from service.script_tools.scene_repo import get_all_scenes
from utils.database import engine as default_engine


@dataclass
class CoveragePoint:
    title: str
    detail: str
    anchor_scene_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "detail": self.detail,
            "anchor_scene_id": self.anchor_scene_id,
        }


@dataclass
class CoverageCard:
    logline: str
    recommendation: str
    confidence: str
    genre: List[str] = field(default_factory=list)
    core_value: str = ""
    strengths: List[CoveragePoint] = field(default_factory=list)
    concerns: List[CoveragePoint] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "logline": self.logline,
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "genre": self.genre,
            "core_value": self.core_value,
            "strengths": [p.to_dict() for p in self.strengths],
            "concerns": [p.to_dict() for p in self.concerns],
        }


_SYSTEM_PROMPT = """你是中文短剧选品编辑，正在写 studio coverage 的 30 秒决策卡。

你只基于给定剧本文本判断，不要编造市场数据。
目标用户是选品、编剧统筹、平台审核，不是程序员。
语言要求：自然、短、可执行；禁止 reward、方差、OOC、schema、embedding 等工程词。
"""

_PROMPT = """下面是剧本前部场景与若干全剧线索。请输出 30 秒决策卡。

【场景】
{scenes_block}

【输出 JSON】
{{
  "logline": "≤60字一句话概括这部剧讲什么",
  "recommendation": "recommend|consider|pass",
  "confidence": "high|medium|low",
  "genre": ["类型标签1", "类型标签2"],
  "core_value": "≤30字，这份剧本最值得关注的价值",
  "strengths": [
    {{"title": "≤12字", "detail": "≤80字", "anchor_scene_id": "<scene_id或空>"}}
  ],
  "concerns": [
    {{"title": "≤12字", "detail": "≤80字", "anchor_scene_id": "<scene_id或空>"}}
  ]
}}

规则：
1. strengths 恰好 3 条；concerns 恰好 3 条。
2. anchor_scene_id 必须来自上方场景；无法定位则填 null。
3. recommendation 不是分数换算，而是「是否值得继续投入阅读/讨论/推进」。
4. 不要写泛泛而谈的空话，例如「剧情不错」「节奏可以」。
"""


async def extract_coverage_card(
    *,
    script_id: str,
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
    max_scenes: int = 18,
) -> CoverageCard:
    caller = caller or LlmCaller()
    scenes = get_all_scenes(script_id=script_id, limit=max_scenes, engine=engine)
    if not scenes:
        raise ValueError(f"script_id={script_id} 没有可分析的场景")

    allowed_scene_ids = {s.id for s in scenes}
    blocks = []
    for scene in scenes:
        text = scene.text or ""
        if len(text) > 900:
            text = text[:900] + "..."
        blocks.append(
            f"[scene_id={scene.id}] [第{scene.episode_no or '?'}集] "
            f"[{scene.scene_no}] [{scene.scene_label}]\n{text}"
        )

    resp = await caller.call_json(
        prompt=_PROMPT.format(scenes_block="\n\n---\n\n".join(blocks)),
        tier=ModelTier.PRIMARY,
        system_message=_SYSTEM_PROMPT,
        temperature=0.2,
        max_tokens=1200,
    )
    parsed = resp.parsed if isinstance(resp.parsed, dict) else None
    if parsed is None:
        raise ScoreLLMError("coverage_chain: LLM 返回非 JSON object")

    recommendation = str(parsed.get("recommendation") or "").strip()
    if recommendation not in {"recommend", "consider", "pass"}:
        raise ScoreLLMError(f"coverage_chain: invalid recommendation={recommendation!r}")

    confidence = str(parsed.get("confidence") or "medium").strip()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium"

    return CoverageCard(
        logline=_truncate(str(parsed.get("logline") or ""), 60),
        recommendation=recommendation,
        confidence=confidence,
        genre=_string_list(parsed.get("genre"), limit=3),
        core_value=_truncate(str(parsed.get("core_value") or ""), 30),
        strengths=_points(parsed.get("strengths"), allowed_scene_ids),
        concerns=_points(parsed.get("concerns"), allowed_scene_ids),
    )


def _points(raw: object, allowed_scene_ids: set[str]) -> List[CoveragePoint]:
    if not isinstance(raw, list):
        return []
    out: List[CoveragePoint] = []
    for item in raw[:3]:
        if not isinstance(item, dict):
            continue
        anchor = item.get("anchor_scene_id")
        anchor_id = str(anchor).strip() if anchor else None
        if anchor_id not in allowed_scene_ids:
            anchor_id = None
        title = _truncate(str(item.get("title") or ""), 12)
        detail = _truncate(str(item.get("detail") or ""), 80)
        if not title or not detail:
            continue
        out.append(CoveragePoint(title=title, detail=detail, anchor_scene_id=anchor_id))
    return out


def _string_list(raw: object, *, limit: int) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        text = _truncate(str(item or "").strip(), 12)
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _truncate(text: str, max_len: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"
