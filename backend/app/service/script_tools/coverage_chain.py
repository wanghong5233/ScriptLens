"""Coverage Card 抽取：30 秒决策层。

借鉴 studio coverage：logline + recommendation + strengths/concerns。
这层回答 task.md 的「值不值得继续看 / 核心价值 / 问题风险」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.engine import Engine

from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError, TokenBudget
from service.script_tools.scene_repo import (
    LLM_EVIDENCE_MAX_LEN,
    format_scene_for_llm,
    get_all_scenes,
    reconcile_text_quote_selector,
)
from utils.database import engine as default_engine


@dataclass
class CoveragePoint:
    """30 秒判断卡的单条 strength / concern（v3.4 W3C TextQuoteSelector 锚定）。

    跳转锚点：(anchor_scene_id, evidence_line_range)
    - evidence_line_range 由后端在 scene_text 里 reconcile quote.exact 反算
    - evidence_quote 是 verified verbatim 原文（未 verified 时为 None）
    - LLM 不再写 offset；W3C / Anthropic Citations / Semiont 标准 pattern
    """

    title: str
    detail: str
    anchor_scene_id: Optional[str] = None
    evidence_line_range: Optional[tuple[int, int]] = None
    evidence_quote: Optional[str] = None
    quote_verified: bool = False

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "detail": self.detail,
            "anchor_scene_id": self.anchor_scene_id,
            "evidence_line_range": list(self.evidence_line_range) if self.evidence_line_range else None,
            "evidence_quote": self.evidence_quote,
            "quote_verified": self.quote_verified,
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

【场景】（每场原文都按行打了 [L1] [L2] ... 行号标注，便于你在下方引用）
{scenes_block}

【输出 JSON】（严格遵循 W3C TextQuoteSelector 范式）
{{
  "logline": "≤60字一句话概括这部剧讲什么",
  "recommendation": "recommend|consider|pass",
  "confidence": "high|medium|low",
  "genre": ["类型标签1", "类型标签2"],
  "core_value": "≤30字，这份剧本最值得关注的价值",
  "strengths": [
    {{
      "title": "≤12字",
      "detail": "≤80字（你的诠释，不要照抄原文）",
      "anchor_scene_id": "<上面给出的 scene_id 之一，无法定位则 null>",
      "quote": {{
        "exact": "<原文逐字片段：必须是上方 anchor scene 的 [L{{n}}] 标注里 100% 一字不差出现过的连续文本，10-{evidence_max_len} 字>",
        "prefix": "<exact 前面紧邻的 5-15 字原文，用于消歧（可选）>",
        "suffix": "<exact 后面紧邻的 5-15 字原文，用于消歧（可选）>"
      }}
    }}
  ],
  "concerns": [...同上结构...]
}}

规则：
1. strengths 恰好 3 条；concerns 恰好 3 条。
2. anchor_scene_id 必须来自上方场景；无法定位则填 null。
3. **quote.exact 是核心字段**——用户跳转高亮就用这段原文：
   - **必须**是 anchor scene 的 [L{{n}}] 标注里**逐字出现**的连续文本，不能改写、合并多行、概括
   - 选最能证明 detail 的那一句台词或动作行；动作行（△ 开头）和对白行都可以
   - 长度 10-{evidence_max_len} 字；不足 10 字时把紧邻上下文带上凑够
   - 如 anchor_scene_id 为 null，quote.exact 留空字符串
   - **不要**写成"姜栀枝走进房间"这种叙述句——那是你的 detail；exact 写真原文台词或动作行
4. recommendation 不是分数换算，而是「是否值得继续投入阅读/讨论/推进」。
5. 不要写泛泛而谈的空话，例如「剧情不错」「节奏可以」。
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
    scene_text_by_id: dict[str, str] = {s.id: (s.text or "") for s in scenes}
    blocks = []
    for scene in scenes:
        scene_text = scene.text or ""
        annotated = format_scene_for_llm(scene_text=scene_text, max_chars=900)
        blocks.append(
            f"[scene_id={scene.id}] [第{scene.episode_no or '?'}集] "
            f"[{scene.scene_no}] [{scene.scene_label}]\n{annotated}"
        )

    resp = await caller.call_json(
        prompt=_PROMPT.format(
            scenes_block="\n\n---\n\n".join(blocks),
            evidence_max_len=LLM_EVIDENCE_MAX_LEN,
        ),
        tier=ModelTier.PRIMARY,
        system_message=_SYSTEM_PROMPT,
        temperature=0.2,
        max_tokens=TokenBudget.COVERAGE_CARD,
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
        strengths=_points(parsed.get("strengths"), allowed_scene_ids, scene_text_by_id),
        concerns=_points(parsed.get("concerns"), allowed_scene_ids, scene_text_by_id),
    )


def _points(
    raw: object,
    allowed_scene_ids: set[str],
    scene_text_by_id: dict[str, str],
) -> List[CoveragePoint]:
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

        line_range: Optional[tuple[int, int]] = None
        evidence_quote: Optional[str] = None
        quote_verified = False

        if anchor_id:
            quote_raw = item.get("quote")
            exact = ""
            prefix = ""
            suffix = ""
            if isinstance(quote_raw, dict):
                exact = str(quote_raw.get("exact") or "").strip()
                prefix = str(quote_raw.get("prefix") or "").strip()
                suffix = str(quote_raw.get("suffix") or "").strip()
            if exact:
                scene_text = scene_text_by_id.get(anchor_id, "")
                line_range = reconcile_text_quote_selector(
                    scene_text=scene_text,
                    exact=exact,
                    prefix=prefix or None,
                    suffix=suffix or None,
                )
                if line_range is not None:
                    quote_verified = True
                    evidence_quote = _truncate(exact, LLM_EVIDENCE_MAX_LEN) or None
                else:
                    # verbatim 没匹配上：line_range 留 None，前端会做整场跳转
                    # evidence_quote 留 None 以避免 UI 展示与原文不符的"伪原文"
                    evidence_quote = None

        out.append(CoveragePoint(
            title=title,
            detail=detail,
            anchor_scene_id=anchor_id,
            evidence_line_range=line_range,
            evidence_quote=evidence_quote,
            quote_verified=quote_verified,
        ))
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
