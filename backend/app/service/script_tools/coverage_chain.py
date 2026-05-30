"""Coverage Card 抽取：30 秒决策层。

v3.5 产品定位重构：
- 速览 = **全剧高度凝练**的"30 秒决策卡"（业内标杆：Hollywood Studio Coverage /
  Final Draft Bullet Summary / NotebookLM Source Summary / Sudowrite Manuscript Analysis）
- 工业级 coverage 的共识：**不附单场单引用**。证据在 detail 报告（故事 / 看点 /
  风险 tab），速览只回答「值不值得继续看 / 题材定位 / 核心价值 / 综合判断」
- 之前实现是 anti-pattern：max_scenes=18 只看前 14.7% 剧本就拍板，且强制每个
  strength/concern 都给 anchor_scene_id + quote.exact，把速览硬塞进"单场证据"
  范式，跟"全剧凝练"的产品定位矛盾

新 contract：
- 输入：**已经聚合好的全剧结构化数据**（reward 事件 / beat 三幕节拍 / 人物关系图 /
  合规 hit / 题材标签 / 元信息），不再读场原文
- 输出：logline + synopsis（200-300 字全剧故事浓缩，工业 coverage 必有项）+
  recommendation + 题材 + 核心价值 + 综合 strengths / concerns（**纯文本判断**，
  无 anchor / quote / line_range）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError, TokenBudget


@dataclass
class CoveragePoint:
    """速览 strength / concern：纯文本全剧判断，**不挂任何 scene anchor / quote**。

    与故事 / 看点 / 风险 tab 的 evidence_ref 严格区分：那些是"单场单引用证据"，
    这里是"全剧综合判断"，产品语义层级不同。
    """

    title: str
    detail: str

    def to_dict(self) -> dict:
        return {"title": self.title, "detail": self.detail}


@dataclass
class CoverageCard:
    """30 秒决策卡。

    工业 coverage（Hollywood / Final Draft / NotebookLM）必有项：
    - logline:        ≤ 60 字一句话剧本是什么
    - synopsis:       200-300 字全剧故事浓缩
    - recommendation: recommend / consider / pass
    - confidence:     high / medium / low
    - genre:          1-3 个题材标签
    - core_value:     ≤ 30 字核心卖点
    - strengths:      3 条 全剧综合优势（纯文本，无 anchor）
    - concerns:       3 条 全剧综合风险（纯文本，无 anchor）
    """

    logline: str
    recommendation: str
    confidence: str
    synopsis: str = ""
    genre: List[str] = field(default_factory=list)
    core_value: str = ""
    strengths: List[CoveragePoint] = field(default_factory=list)
    concerns: List[CoveragePoint] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "logline": self.logline,
            "synopsis": self.synopsis,
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "genre": self.genre,
            "core_value": self.core_value,
            "strengths": [p.to_dict() for p in self.strengths],
            "concerns": [p.to_dict() for p in self.concerns],
        }


_SYSTEM_PROMPT = """你是中文短剧选品总监，正在为投资决策会撰写 30 秒决策卡。

风格：
- 自然、短、可执行；目标读者是制片人、平台选品、编剧统筹，不是程序员
- 禁止 reward、方差、OOC、schema、embedding 等工程词
- 不要泛泛而谈（如「剧情不错」「节奏可以」），要具体到能指导投资决策

你的判断材料是「全剧已经被分析师拆好的结构化结论」，不是裸场原文。
你的任务是**综合这些信息**写出全剧级的决策卡，无需也不应引用单场细节。
"""


_PROMPT = """下面是一部短剧的全剧分析摘要（已由专业分析师从全 {total_episodes} 集 / {total_scenes} 场抽取出的结构化结论）。

【剧本元信息】
- 标题：{title}
- 总集数：{total_episodes}
- 总场数：{total_scenes}
- 已识别题材标签：{drama_tags}

【三幕节拍（beat_sheet）】
{beat_block}

【主要人物（核心 5 个 + 关键关系）】
{character_block}

【全剧高潮事件（reward events，按集排序）】
{reward_block}

【合规与风险】
{risk_block}

---

请综合以上结构化分析，输出 30 秒决策卡 JSON：

{{
  "logline": "≤60字一句话概括这部剧讲什么（主角+核心冲突+核心钩子）",
  "synopsis": "200-300字全剧故事浓缩。要点：开局如何抓人 → 主线冲突如何升级 → 中段关键转折 → 结局如何收。语言要让没读过剧本的人能 1 分钟懂剧情",
  "recommendation": "recommend|consider|pass",
  "confidence": "high|medium|low",
  "genre": ["不超过 3 个题材标签，每个 ≤6 字"],
  "core_value": "≤30字，这份剧本最值得投资的卖点（爽点密度 / 题材稀缺 / 人物魅力 / 节奏感）",
  "strengths": [
    {{"title": "≤12字综合优势 1", "detail": "≤80字解释为什么是优势（全剧维度，不指向单场）"}},
    {{"title": "≤12字综合优势 2", "detail": "≤80字解释"}},
    {{"title": "≤12字综合优势 3", "detail": "≤80字解释"}}
  ],
  "concerns": [
    {{"title": "≤12字综合风险 1", "detail": "≤80字解释（可指出某段塌陷集数范围，但不引用单场原文）"}},
    {{"title": "≤12字综合风险 2", "detail": "≤80字解释"}},
    {{"title": "≤12字综合风险 3", "detail": "≤80字解释"}}
  ]
}}

【重要规则】
1. strengths 恰好 3 条；concerns 恰好 3 条。
2. **detail 是全剧综合判断**，不要引用单场或单句台词。如「中段集 40-60 节奏明显塌陷」可以，「第 42 集姜栀枝那句台词显得突兀」不行——那是 detail 应该在的层级。
3. recommendation 不是分数换算，而是「这部剧值不值得继续投入阅读 / 立项 / 推进」。
4. synopsis 不要只是 logline 的扩写，要真的把"开局→中段→结局"讲出来。
5. 不要写空话（「剧情还不错」「节奏可以」）。
6. core_value 要具体到品类卖点（"穿书救赎 + 双男主 CP"），不要写「内容优质」。
"""


def _format_beat_block(beat_sheet: Optional[Any]) -> str:
    if beat_sheet is None or not getattr(beat_sheet, "acts", None):
        return "（无可用 beat_sheet 数据）"
    lines: list[str] = []
    for act in beat_sheet.acts:
        lines.append(f"第 {act.act} 幕「{act.title}」（共 {len(act.beats)} 个节拍）：")
        for beat in act.beats:
            lines.append(f"  - [{beat.type}] {beat.summary}")
    return "\n".join(lines)


def _format_character_block(
    characters: List[Dict[str, Any]],
    relationships: List[Dict[str, Any]],
) -> str:
    if not characters:
        return "（无可用人物数据）"

    # 取曝光度 top 5
    top_characters = sorted(
        characters,
        key=lambda c: int(c.get("appearance_count") or 0),
        reverse=True,
    )[:5]

    lines: list[str] = []
    for c in top_characters:
        name = str(c.get("name") or "").strip()
        role = str(c.get("archetype") or c.get("role_in_arc") or "").strip()
        agency = str(c.get("agency_level") or "").strip()
        bio = f"  - {name}"
        if role:
            bio += f"（{role}）"
        if agency and agency != "medium":
            bio += f" agency={agency}"
        lines.append(bio)

    # 关键关系：top 5（按 polarity 强度排）
    if relationships:
        lines.append("\n核心关系：")
        polarity_priority = {"negative": 0, "positive": 1, "neutral": 2}
        sorted_rels = sorted(
            relationships[:10],
            key=lambda r: polarity_priority.get(str(r.get("polarity") or "neutral"), 9),
        )[:5]
        name_by_id = {str(c.get("id") or ""): str(c.get("name") or "") for c in characters}
        for r in sorted_rels:
            a = name_by_id.get(str(r.get("a_id") or ""), "?")
            b = name_by_id.get(str(r.get("b_id") or ""), "?")
            t = str(r.get("type") or "")
            polarity = str(r.get("polarity") or "")
            lines.append(f"  - {a} ↔ {b} : {t} ({polarity})")

    return "\n".join(lines)


def _format_reward_block(reward_events: List[Any]) -> str:
    if not reward_events:
        return "（全剧未抽到 high-confidence reward 事件——可能是爽点密度过低）"

    # 按集 sort
    sorted_events = sorted(
        reward_events,
        key=lambda e: (e.episode_no or 9999, e.scene_no or ""),
    )

    lines: list[str] = [f"全剧抽出 {len(sorted_events)} 个 high-confidence 高潮事件："]
    for ev in sorted_events[:20]:  # 限 20 条避免 prompt 爆炸
        ep = ev.episode_no or "?"
        lines.append(f"  - 第{ep}集 [{ev.event_type}] {ev.claim}")
    if len(sorted_events) > 20:
        lines.append(f"  ...（还有 {len(sorted_events) - 20} 个未列出）")
    return "\n".join(lines)


def _format_risk_block(compliance_payload: Dict[str, Any]) -> str:
    status = str(compliance_payload.get("status") or "")
    level = str(compliance_payload.get("level") or "")
    reason = str(compliance_payload.get("reason") or "")
    hits = compliance_payload.get("hits") or []

    if status == "pass":
        return "（全剧未触发广电红线 / 主流题材风险）"

    lines: list[str] = [f"合规状态：{status} / {level}"]
    if reason:
        lines.append(f"原因：{reason}")
    # 按 category 聚合 hit
    if hits:
        from collections import Counter
        cat_count = Counter(str(h.get("category") or "") for h in hits)
        cat_summary = "、".join(f"{cat}×{n}" for cat, n in cat_count.most_common(5))
        lines.append(f"hit 分布：{cat_summary}")
    return "\n".join(lines)


def _format_drama_tags(drama_tags: List[Dict[str, Any]]) -> str:
    if not drama_tags:
        return "（无）"
    return "、".join(
        str(t.get("value") or "").strip()
        for t in drama_tags[:5]
        if t.get("value")
    )


async def extract_coverage_card(
    *,
    title: str,
    total_episodes: int,
    total_scenes: int,
    reward_events: List[Any],
    beat_sheet: Optional[Any] = None,
    characters: Optional[List[Dict[str, Any]]] = None,
    relationships: Optional[List[Dict[str, Any]]] = None,
    compliance_payload: Optional[Dict[str, Any]] = None,
    drama_tags: Optional[List[Dict[str, Any]]] = None,
    caller: Optional[LlmCaller] = None,
) -> CoverageCard:
    """从全剧已聚合的结构化分析中提炼 30 秒决策卡。

    v3.5 起：不再读场原文，输入全部是 chain / scorer 已经抽出的结构化结论。
    与故事 / 看点 / 风险 tab 严格分层——速览只回答全剧综合判断。
    """
    caller = caller or LlmCaller()
    prompt = _PROMPT.format(
        title=title or "（无标题）",
        total_episodes=total_episodes or 0,
        total_scenes=total_scenes or 0,
        drama_tags=_format_drama_tags(drama_tags or []),
        beat_block=_format_beat_block(beat_sheet),
        character_block=_format_character_block(characters or [], relationships or []),
        reward_block=_format_reward_block(reward_events or []),
        risk_block=_format_risk_block(compliance_payload or {}),
    )

    resp = await caller.call_json(
        prompt=prompt,
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
        synopsis=_truncate(str(parsed.get("synopsis") or ""), 320),
        recommendation=recommendation,
        confidence=confidence,
        genre=_string_list(parsed.get("genre"), limit=3, item_max=8),
        core_value=_truncate(str(parsed.get("core_value") or ""), 30),
        strengths=_points(parsed.get("strengths")),
        concerns=_points(parsed.get("concerns")),
    )


def _points(raw: object) -> List[CoveragePoint]:
    if not isinstance(raw, list):
        return []
    out: List[CoveragePoint] = []
    for item in raw[:3]:
        if not isinstance(item, dict):
            continue
        title = _truncate(str(item.get("title") or "").strip(), 12)
        detail = _truncate(str(item.get("detail") or "").strip(), 80)
        if not title or not detail:
            continue
        out.append(CoveragePoint(title=title, detail=detail))
    return out


def _string_list(raw: object, *, limit: int, item_max: int = 12) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        text = _truncate(str(item or "").strip(), item_max)
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
