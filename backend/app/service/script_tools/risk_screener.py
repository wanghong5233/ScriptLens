"""审核风险扫描（rubric §3.5）。

链路：
1. 三档关键词词表（high / medium / low）扫全场景，命中即记
2. 命中场景批量丢给 mini 模型二级判定（防止"杀人"在比喻里被误判）
3. 聚合 → 4 档分级（high_risk / medium_risk / low_risk / clean）+ 0-10 分

输出对齐 PRD §7：
- scorecard.risk.score（0-10）
- scorecard.risk.tier/level（high_risk / medium_risk / low_risk / clean）
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
    reconcile_text_quote_selector,
)

logger = logging.getLogger(__name__)


@dataclass
class RiskHit:
    """单条 risk 命中。

    v3.4 W3C TextQuoteSelector 锚定（取代 v3.3 line_range 契约）：
    - `quote_verbatim`     在 scene 内被唯一 verbatim 定位的原文片段；未通过则空串
    - `quote_verified`     verbatim 在 scene 内唯一定位成功 → 跳转可精确到行
    - `evidence_line_range`后端从 quote_verbatim 反算的行号；verified=False 时为 None
    - `excerpt`            UI 展示（向后兼容）：verified 时 = verbatim quote，否则 = rationale
                           或关键词所在行（low_risk 路径）
    - `rationale`          LLM 给的判定理由（high/medium）；低风险无 LLM 时为空

    设计原则同 reward_extractor：LLM 不写 offset，offset 由后端从 scene_text
    反算（W3C / Anthropic Citations / Semiont reconcileSelector pattern）。
    """

    scene_id: str
    scene_no: str
    episode_no: Optional[int]
    level: str  # high_risk | medium_risk | low_risk
    category: str  # underage_sexual / wealth_worship / vulgar_language / ...
    matched_term: str
    confirmed_by_llm: bool
    quote_verbatim: str = ""
    quote_verified: bool = False
    rationale: str = ""
    evidence_line_range: Optional[tuple[int, int]] = None

    @property
    def excerpt(self) -> str:
        """下游展示主字段（保留向后兼容）。

        verified 时给 verbatim 原文（用户跳转点击 → 高亮该行），未 verified
        时给 LLM rationale（或关键词所在行片段），都 ≤120 字。
        """
        if self.quote_verified and self.quote_verbatim:
            tail = f"｜判定：{self.rationale}" if self.rationale else ""
            return (self.quote_verbatim + tail)[:120]
        return (self.rationale or "")[:120]


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

输出 JSON（严格遵循 W3C TextQuoteSelector 范式）：
{{
  "is_real_violation": <true|false>,
  "rationale": "<≤60 字判定理由>",
  "quote": {{
    "exact": "<原文逐字片段：必须是上面 [L{{n}}] 标注里 100% 一字不差出现过的连续文本，10-80 字；is_real_violation=false 时填空字符串>",
    "prefix": "<exact 前面紧邻的 5-15 字原文，用于消歧（可选）>",
    "suffix": "<exact 后面紧邻的 5-15 字原文，用于消歧（可选）>"
  }}
}}

quote.exact 规则（核心）：
- **必须**是 [L{{n}}] 标注里逐字出现过的连续文本，不能改写、合并、概括
- 选包含违规关键词「{term}」的那一句台词或动作行；如关键词出现多次，选最强证据那一处
- is_real_violation=false 时 exact 留空字符串
- 长度 10-80 字；如原文该处不足 10 字，把紧邻上下文带上凑够"""


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
    # low_risk 不过 LLM，但仍用关键词位置反推 verbatim 行——前端跳转能精确到行而非整场 dump
    confirmed_low = []
    for rec in low_records:
        kw_line = _locate_keyword_line(rec.scene.text or "", rec.matched_term)
        line_range = kw_line
        verbatim = _line_text(rec.scene.text or "", kw_line[0])[:120] if kw_line else ""
        confirmed_low.append(_to_hit(
            rec,
            confirmed=False,
            quote_verbatim=verbatim,
            quote_verified=False,
            line_range=line_range,
        ))

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
    rationale = str(parsed.get("rationale") or "")[:120]

    quote_verbatim = ""
    quote_verified = False
    line_range: Optional[tuple[int, int]] = None

    if is_real:
        quote_raw = parsed.get("quote")
        exact = ""
        prefix = ""
        suffix = ""
        if isinstance(quote_raw, dict):
            exact = str(quote_raw.get("exact") or "").strip()
            prefix = str(quote_raw.get("prefix") or "").strip()
            suffix = str(quote_raw.get("suffix") or "").strip()
        if exact:
            line_range = reconcile_text_quote_selector(
                scene_text=raw_text,
                exact=exact,
                prefix=prefix or None,
                suffix=suffix or None,
            )
            if line_range is not None:
                quote_verified = True
                quote_verbatim = exact[:120]
            else:
                logger.warning(
                    "risk_screener.quote_unverified scene_no=%s term=%s category=%s "
                    "exact_head=%r (verbatim 在 scene 内搜不到或多义无法消歧 → 整场跳转)",
                    rec.scene.scene_no, rec.matched_term, rec.category, exact[:40],
                )
        # verbatim 兜底：LLM 既然判 is_real_violation=true，至少把关键词所在行扯出来
        if not quote_verified:
            kw_line = _locate_keyword_line(raw_text, rec.matched_term)
            if kw_line is not None:
                line_range = kw_line
                quote_verbatim = _line_text(raw_text, kw_line[0])[:120]
                # 不标 verified —— 这是关键词反推不是 LLM verbatim，UI 应可视化区分

    return _to_hit(
        rec,
        confirmed=is_real,
        quote_verbatim=quote_verbatim,
        quote_verified=quote_verified,
        rationale=rationale,
        line_range=line_range,
    )


def _locate_keyword_line(scene_text: str, term: str) -> Optional[tuple[int, int]]:
    """关键词所在行（1-based 闭区间，单行）。term 命中多行时取首行。"""
    if not scene_text or not term:
        return None
    for idx, line in enumerate(scene_text.split("\n"), start=1):
        if term in line:
            return (idx, idx)
    return None


def _line_text(scene_text: str, line_no_1based: int) -> str:
    """取 1-based 行号对应的整行文本（用于 low_risk 关键词反推 verbatim）。"""
    if not scene_text or line_no_1based < 1:
        return ""
    lines = scene_text.split("\n")
    idx = line_no_1based - 1
    if idx >= len(lines):
        return ""
    return lines[idx]


def _to_hit(
    rec: _Record,
    *,
    confirmed: bool,
    quote_verbatim: str = "",
    quote_verified: bool = False,
    rationale: str = "",
    line_range: Optional[tuple[int, int]] = None,
) -> RiskHit:
    return RiskHit(
        scene_id=rec.scene.id,
        scene_no=rec.scene.scene_no,
        episode_no=rec.scene.episode_no,
        level=rec.level,
        category=rec.category,
        matched_term=rec.matched_term,
        confirmed_by_llm=confirmed,
        quote_verbatim=quote_verbatim,
        quote_verified=quote_verified,
        rationale=rationale,
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
