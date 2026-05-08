"""审核风险扫描（rubric §3.5）。

链路：
1. 三档关键词词表（high / medium / low）扫全场景，命中即记
2. 命中场景批量丢给 mini 模型二级判定（防止"杀人"在比喻里被误判）
3. 聚合 → 4 档分级（high_risk / medium_risk / low_risk / clean）+ 0-10 分

输出对齐 PRD §7：
- scorecard.risk.score（0-10）
- scorecard.risk.level（high_risk / medium_risk / low_risk / clean）
- scorecard.risk.reason
- scorecard.risk.evidence_ref_ids[]
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Optional

from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError, TokenBudget
from service.script_tools.risk_terms import (
    HIGH_RISK_TERMS,
    LOW_RISK_TERMS,
    MEDIUM_RISK_TERMS,
    all_high_risk_terms,
    all_low_risk_terms,
    all_medium_risk_terms,
    categorize_term,
)
from service.script_tools.scene_repo import (
    Scene,
    format_scene_for_llm,
    locate_scenes_by_keyword,
    parse_line_range,
)

logger = logging.getLogger(__name__)


@dataclass
class RiskHit:
    """单条 risk 命中。

    v3.3 line-range anchored：
    - `evidence_line_range` 是 LLM 二筛时同次给出的场内行号区间（1-based 闭区间）
      （low_risk 不过 LLM，行号通过关键词位置反推；high/medium 由 LLM 直接给）
    - `excerpt` 仅作 tooltip 展示文本
    """

    scene_id: str
    scene_no: str
    episode_no: Optional[int]
    level: str  # high_risk | medium_risk | low_risk
    category: str  # underage_sexual / wealth_worship / vulgar_language / ...
    matched_term: str
    excerpt: str  # 命中片段（≤120 字），tooltip-only
    confirmed_by_llm: bool
    evidence_line_range: Optional[tuple[int, int]] = None


@dataclass
class RiskResult:
    score: int  # 0-10
    level: str  # high_risk | medium_risk | low_risk | clean
    reason: str
    evidence_ref_ids: List[str]
    hits: List[RiskHit] = field(default_factory=list)


_BATCH_SIZE = 8
_MAX_TEXT_PER_SCENE = 600


_JUDGE_PROMPT = """下面是中文短剧场景片段（按行打了 [L{{n}}] 行号标注），已被关键词「{term}」（属于 {category} 类）命中。
请判断这是「真实出现的违规内容」，还是「比喻 / 否定 / 引用 / 词义不同」的误命中。

【场景】
[scene_no={scene_no}] [{scene_label}]
{text}

输出 JSON：
{{
  "is_real_violation": <true|false>,
  "rationale": "<≤60 字>",
  "evidence_line_range": [<违规内容起始行号>, <结束行号>]
}}

evidence_line_range 规则：
- 仅在 is_real_violation=true 时填写；为 false 时填 null
- 引用 [L{{n}}] 行号（1-based 闭区间），覆盖违规内容真正出现的那段（典型 1-3 行）"""


async def screen_risks(
    *,
    script_id: str,
    caller: Optional[LlmCaller] = None,
    max_hits_per_level: int = 30,
) -> RiskResult:
    caller = caller or LlmCaller()

    # 三档关键词分别扫
    high_hits = locate_scenes_by_keyword(
        script_id=script_id,
        keywords=all_high_risk_terms(),
        limit=max_hits_per_level,
    )
    medium_hits = locate_scenes_by_keyword(
        script_id=script_id,
        keywords=all_medium_risk_terms(),
        limit=max_hits_per_level * 2,
    )
    low_hits = locate_scenes_by_keyword(
        script_id=script_id,
        keywords=all_low_risk_terms(),
        limit=max_hits_per_level * 2,
    )

    # 关键词 → 类别 → 命中 term 反查
    high_records = _build_records(high_hits, all_high_risk_terms())
    medium_records = _build_records(medium_hits, all_medium_risk_terms())
    low_records = _build_records(low_hits, all_low_risk_terms())

    # high_risk / medium_risk 必须 LLM 二级判定（误判代价大）
    # low_risk 量大且容错率高 → 跳过 LLM，直接采信关键词
    confirmed_high = await _confirm_with_llm(high_records, caller)
    confirmed_medium = await _confirm_with_llm(medium_records, caller)
    confirmed_low = [_to_hit(rec, confirmed=False) for rec in low_records]

    all_hits = confirmed_high + confirmed_medium + confirmed_low
    level, score, reason = _aggregate(confirmed_high, confirmed_medium, confirmed_low)
    evidence_ref_ids = _select_evidence(all_hits, top_k=5)

    return RiskResult(
        score=score,
        level=level,
        reason=reason,
        evidence_ref_ids=evidence_ref_ids,
        hits=all_hits,
    )


# ============================================================
# 内部：候选构造
# ============================================================


@dataclass
class _Record:
    scene: Scene
    matched_term: str
    level: str
    category: str


def _build_records(scenes: List[Scene], term_pool: List[str]) -> List[_Record]:
    """关键词层只给场景，本函数反查每个场景命中的具体 term + category。"""
    out: List[_Record] = []
    for sc in scenes:
        text = sc.text or ""
        # 一个场景可能命中多个 term，挑第一个就行（同 level）
        for term in term_pool:
            if term and term in text:
                cat_info = categorize_term(term)
                if not cat_info:
                    continue
                level, category = cat_info
                out.append(_Record(scene=sc, matched_term=term, level=level, category=category))
                break
    return out


# ============================================================
# 内部：LLM 二级确认
# ============================================================


async def _confirm_with_llm(records: List[_Record], caller: LlmCaller) -> List[RiskHit]:
    if not records:
        return []
    tasks = [_judge_one(rec, caller) for rec in records]
    judged = await asyncio.gather(*tasks, return_exceptions=False)
    return [hit for hit in judged if hit is not None and hit.confirmed_by_llm]


async def _judge_one(rec: _Record, caller: LlmCaller) -> Optional[RiskHit]:
    raw_text = rec.scene.text or ""
    annotated = format_scene_for_llm(scene_text=raw_text, max_chars=_MAX_TEXT_PER_SCENE)
    prompt = _JUDGE_PROMPT.format(
        term=rec.matched_term,
        category=rec.category,
        scene_no=rec.scene.scene_no,
        scene_label=rec.scene.scene_label or "",
        text=annotated,
    )
    try:
        resp = await caller.call_json(
            prompt, tier=ModelTier.MINI, temperature=0,
            max_tokens=TokenBudget.RISK_CONFIRM,
        )
    except ScoreLLMError as e:
        logger.warning("risk judge failed scene_no=%s term=%s: %s", rec.scene.scene_no, rec.matched_term, e)
        return _to_hit(rec, confirmed=False)

    parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
    is_real = bool(parsed.get("is_real_violation", False))
    line_range = None
    if is_real:
        scene_lc = len(raw_text.split("\n")) if raw_text else 0
        line_range = parse_line_range(parsed.get("evidence_line_range"), scene_line_count=scene_lc, max_span=8)
    return _to_hit(
        rec,
        confirmed=is_real,
        rationale=str(parsed.get("rationale") or "")[:120],
        line_range=line_range,
    )


def _to_hit(
    rec: _Record,
    *,
    confirmed: bool,
    rationale: str = "",
    line_range: Optional[tuple[int, int]] = None,
) -> RiskHit:
    excerpt = (rec.scene.text or "")[:120]
    if rationale:
        excerpt = f"{excerpt}｜判定：{rationale}"
    return RiskHit(
        scene_id=rec.scene.id,
        scene_no=rec.scene.scene_no,
        episode_no=rec.scene.episode_no,
        level=rec.level,
        category=rec.category,
        matched_term=rec.matched_term,
        excerpt=excerpt,
        confirmed_by_llm=confirmed,
        evidence_line_range=line_range,
    )


# ============================================================
# 内部：4 档聚合
# ============================================================


def _aggregate(high: List[RiskHit], medium: List[RiskHit], low: List[RiskHit]) -> tuple[str, int, str]:
    high_n = len(high)
    medium_n = len(medium)
    low_n = len(low)

    if high_n >= 1:
        cats = sorted({h.category for h in high})
        return (
            "high_risk",
            1,
            f"命中 {high_n} 处广电红线（{', '.join(cats)}），不建议发布",
        )
    if medium_n >= 3:
        cats = sorted({h.category for h in medium})
        return (
            "medium_risk",
            4,
            f"命中 {medium_n} 处主流题材风险（{', '.join(cats)}），需要修改后过审",
        )
    if medium_n >= 1 or low_n >= 6:
        cats = sorted({h.category for h in medium + low})
        return (
            "low_risk",
            7,
            f"命中 {medium_n + low_n} 处局部风险（{', '.join(cats) or '低俗台词'}），可过但建议优化",
        )
    return ("clean", 9, "全文未命中已知风险关键词，导向正向")


def _select_evidence(hits: List[RiskHit], top_k: int) -> List[str]:
    """证据按 level 优先级取 top_k 个独立 scene_id。"""
    priority = {"high_risk": 0, "medium_risk": 1, "low_risk": 2}
    seen: set[str] = set()
    out: List[str] = []
    for h in sorted(hits, key=lambda x: (priority.get(x.level, 9), x.scene_no)):
        if h.scene_id in seen:
            continue
        if not h.confirmed_by_llm and h.level != "low_risk":
            continue
        seen.add(h.scene_id)
        out.append(h.scene_id)
        if len(out) >= top_k:
            break
    return out
