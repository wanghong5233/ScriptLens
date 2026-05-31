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

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError, TokenBudget

logger = logging.getLogger(__name__)


@dataclass
class ComparableTitleEntry:
    """同类爆款条目（v3.7.1）：真实短剧视频标题 + 平台 + 真实跳转链接。

    `to_dict()` 与 schemas.script.ComparableTitleEntry Pydantic 模型 1:1 对齐。
    """

    title: str
    url: Optional[str] = None
    platform: Optional[str] = None
    snippet: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "platform": self.platform,
            "snippet": self.snippet,
        }


# v3.7.1：垂直短剧/漫剧平台白名单（按业内选品流量优先级排序）
# 抖音生态（抖音+西瓜+今日头条+番茄小说短剧）是短剧最大流量池 → 最高优先级
# B 站漫剧（含 AI 漫剧、动画短剧）→ 第二
# 快手 / 微视 / 其他视频平台 → 第三
# 不限定 video host 时也容许搜索结果（最后兜底）
_VIDEO_PLATFORM_PRIORITY: Dict[str, int] = {
    "douyin": 100,
    "v.douyin.com": 100,
    "ixigua.com": 95,
    "haokan.baidu.com": 85,
    "bilibili": 80,
    "kuaishou": 70,
    "v.kuaishou.com": 70,
    "weibo.com": 50,
    "weishi.qq.com": 45,
    "iqiyi.com": 40,
    "youku.com": 40,
    "tencent": 40,
    "qq.com": 35,
}

# Tavily advanced search 优先纳入的视频域（垂直短剧/漫剧池）
_INCLUDE_VIDEO_DOMAINS: List[str] = [
    "douyin.com",
    "v.douyin.com",
    "ixigua.com",
    "bilibili.com",
    "b23.tv",
    "kuaishou.com",
    "v.kuaishou.com",
    "haokan.baidu.com",
    "weibo.com",
    "weishi.qq.com",
]

# 排除明显非视频结果（wiki、新闻聚合、招商页等）
_EXCLUDE_DOMAINS: List[str] = [
    "baike.baidu.com",
    "zhihu.com",
    "wikipedia.org",
]


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
    # 业内对照：抖音红果 / 快手星芒选品判断必看「同类爆款」+ ReelShort comparable titles。
    # v3.7：LLM 给剧名 → 后端用 Tavily 搜索校验 + 取真实链接 → 前端 chip 可点击跳转。
    comparable_titles: List[ComparableTitleEntry] = field(default_factory=list)

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
            "comparable_titles": [c.to_dict() for c in self.comparable_titles],
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
  "core_value": "≤30字，这份剧本最值得投资的卖点。必须是【陈述性卖点】不是 critique；不能含「但/然而/不过/建议/过于/缺少」等转折/建议词；要像短剧推荐位标题（参考：「多重身份反转+复仇打脸爽剧」「穿书救赎双男主CP」「战神归来重生甜宠」），不要像分析师评语",
  "strengths": [
    {{"title": "≤12字综合优势 1", "detail": "≤80字解释为什么是优势（全剧维度，不指向单场）"}},
    {{"title": "≤12字综合优势 2", "detail": "≤80字解释"}},
    {{"title": "≤12字综合优势 3", "detail": "≤80字解释"}}
  ],
  "concerns": [
    {{"title": "≤12字综合风险 1", "detail": "≤80字解释（可指出某段塌陷集数范围，但不引用单场原文）"}},
    {{"title": "≤12字综合风险 2", "detail": "≤80字解释"}},
    {{"title": "≤12字综合风险 3", "detail": "≤80字解释"}}
  ],
  "comparable_titles": ["≤16字 同类爆款 1（剧名 或 剧名+赛道说明）", "≤16字 同类爆款 2", "≤16字 同类爆款 3"]
}}

【重要规则】
1. strengths 恰好 3 条；concerns 恰好 3 条。
2. **detail 是全剧综合判断**，不要引用单场或单句台词。如「中段集 40-60 节奏明显塌陷」可以，「第 42 集姜栀枝那句台词显得突兀」不行——那是 detail 应该在的层级。
3. recommendation 不是分数换算，而是「这部剧值不值得继续投入阅读 / 立项 / 推进」。
4. synopsis 不要只是 logline 的扩写，要真的把"开局→中段→结局"讲出来。
5. 不要写空话（「剧情还不错」「节奏可以」）。
6. core_value 必须是**陈述性卖点**，参考短剧推荐位标题写法：「多重身份反转+复仇打脸爽剧」「穿书救赎双男主CP」「战神归来重生甜宠」。**禁止**含「但/然而/不过/如果/建议/过于/偏/缺少/不足」等转折或建议词——这些是 critique 用语，会被前端速读位过滤丢弃。不要写「内容优质」「节奏可以」这类空话。
7. comparable_titles：2-3 部题材接近、规模相当的**已成爆款短剧 / 漫剧**（业内对照：抖音红果 / 快手星芒 / WeTV / ReelShort 头部投放剧）。每条 ≤16 字，可以是「剧名」也可以是「剧名 · 短描述」（如「《无双》逆袭复仇模板」「《哎呀皇后娘娘来打工》穿越爽剧」）。
   - 优先选**同赛道 + 同题材**（如逆袭复仇 / 穿越打脸 / 战神归来 / 重生甜宠）的真实存在剧目；不要编造剧名
   - 不要写"类似某某剧"，直接给出对比锚点；如果实在无可类比，最多回 1 条或留空数组，不要凑数
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

    raw_titles = _string_list(parsed.get("comparable_titles"), limit=5, item_max=16)
    logline_text = _truncate(str(parsed.get("logline") or ""), 60)
    genre_list = _string_list(parsed.get("genre"), limit=3, item_max=8)
    core_value_text = _truncate(str(parsed.get("core_value") or ""), 30)
    comparable_entries = await _resolve_comparable_videos(
        llm_titles=raw_titles,
        logline=logline_text,
        genre=genre_list,
        core_value=core_value_text,
        target_count=3,
    )

    return CoverageCard(
        logline=logline_text,
        synopsis=_truncate(str(parsed.get("synopsis") or ""), 320),
        recommendation=recommendation,
        confidence=confidence,
        genre=genre_list,
        core_value=core_value_text,
        strengths=_points(parsed.get("strengths")),
        concerns=_points(parsed.get("concerns")),
        comparable_titles=comparable_entries,
    )


def _classify_platform(url: str) -> str:
    """从 URL 推断平台标识。

    用于前端按平台上色 + 后端做平台优先级排序。
    """
    if not url:
        return "other"
    u = url.lower()
    if "douyin.com" in u:
        return "douyin"
    if "ixigua.com" in u:
        return "ixigua"
    if "bilibili.com" in u or "b23.tv" in u:
        return "bilibili"
    if "kuaishou.com" in u or "v.kuaishou" in u:
        return "kuaishou"
    if "haokan.baidu.com" in u:
        return "haokan"
    if "weishi.qq.com" in u or "qq.com" in u:
        return "tencent"
    if "weibo.com" in u:
        return "weibo"
    if "iqiyi.com" in u:
        return "iqiyi"
    if "youku.com" in u:
        return "youku"
    return "other"


def _platform_priority(url: str) -> int:
    """URL → 平台权重；用于聚合后按"短剧选品流量"优先级排序。

    抖音生态（抖音/西瓜/番茄）流量池最大 → 最高优先级
    B 站漫剧次之，快手 / 微视 / 其他平台再次。
    """
    if not url:
        return 0
    u = url.lower()
    for host, score in _VIDEO_PLATFORM_PRIORITY.items():
        if host in u:
            return score
    return 10  # 不在白名单中的"其他"也给低分而非 0


def _build_search_queries(
    *,
    llm_titles: List[str],
    logline: str,
    genre: List[str],
    core_value: str,
) -> List[str]:
    """v3.7.1：从 coverage 自身信号构造多条搜索 query。

    业内对照（Perplexity Discover / Tavily best practice）：多 query 并发能显著
    提升相关性命中率，避免单一 query 偏差导致 0 命中。

    Query 优先级：
      1. LLM 给的剧名候选（如果有）—— 已成爆款的对照锚点
      2. 题材组合 —— 行业最稳定的"找同类"信号
      3. logline 关键词 —— 卖点驱动相似性
      4. core_value —— 兜底
    """
    queries: List[str] = []
    seen: set[str] = set()

    def push(q: str) -> None:
        q = (q or "").strip()
        if not q or q in seen:
            return
        seen.add(q)
        queries.append(q)

    # 1. LLM 推荐的剧名 → 拼上"短剧"加强短剧场景命中
    for title in llm_titles[:5]:
        # 剥离书名号 + 副标题，保留核心剧名
        cleaned = title.strip().lstrip("《").rstrip("》").split("》")[0]
        cleaned = cleaned.split("（")[0].split("(")[0].strip()
        if cleaned:
            push(f"{cleaned} 短剧")

    # 2. 题材组合（短剧选品最稳定的"找同类"信号）
    if genre:
        push(" ".join(genre[:2]) + " 短剧 爆款")
        if len(genre) >= 1:
            push(f"{genre[0]} 短剧 高播放")

    # 3. logline 卖点关键词
    if logline:
        # 取 logline 前 18 字作为搜索词（avoid 完整长句拉低 BM25）
        push(f"{logline[:18]} 短剧")

    # 4. core_value 兜底
    if core_value and len(queries) < 3:
        push(f"{core_value[:16]} 短剧 爆款")

    return queries[:5]  # 上限 5 条避免搜索成本爆炸


def _aggregate_search_results(
    responses: List[Any],
    target_count: int,
) -> List[ComparableTitleEntry]:
    """聚合多个搜索响应：按平台优先级排序 + 去重 + 截断 Top N。

    去重规则：URL host + path 前 40 字符相同视为同一视频。
    """
    all_hits: List[Dict[str, Any]] = []
    for resp in responses:
        if isinstance(resp, Exception) or not isinstance(resp, dict):
            continue
        results = resp.get("results") or []
        for item in results:
            if not isinstance(item, dict):
                continue
            url = (item.get("url") or "").strip()
            title = (item.get("title") or "").strip()
            if not url or not title:
                continue
            all_hits.append(
                {
                    "url": url,
                    "title": title,
                    "snippet": (item.get("snippet") or "").strip(),
                    "score": item.get("score") or 0.0,
                    "platform_priority": _platform_priority(url),
                    "platform": _classify_platform(url),
                }
            )

    if not all_hits:
        return []

    # 排序：平台优先级 desc → Tavily score desc → title 字典序
    all_hits.sort(
        key=lambda x: (-x["platform_priority"], -float(x.get("score") or 0.0), x["title"]),
    )

    # 去重：同 URL 前缀只留第一个（已经按优先级排好）
    seen_keys: set[str] = set()
    deduped: List[Dict[str, Any]] = []
    for hit in all_hits:
        url = hit["url"]
        # 用 host + path 前 40 字符做去重 key
        from urllib.parse import urlparse

        parsed = urlparse(url)
        dedupe_key = f"{parsed.netloc}{parsed.path[:40]}"
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        deduped.append(hit)

    # 截断到目标数（实际取 max(target, 3) 以满足"至少 3 条"）
    limit = max(target_count, 3)
    return [
        ComparableTitleEntry(
            title=_truncate(hit["title"], 40),
            url=hit["url"],
            platform=hit["platform"],
            snippet=_truncate(hit["snippet"], 80) or None,
        )
        for hit in deduped[:limit]
    ]


async def _resolve_comparable_videos(
    *,
    llm_titles: List[str],
    logline: str,
    genre: List[str],
    core_value: str,
    target_count: int = 3,
) -> List[ComparableTitleEntry]:
    """v3.7.1：基于剧本题材 + 卖点 + LLM 候选剧名，搜垂直短剧/漫剧平台拿至少 N 条爆款视频链接。

    业内对照（成熟方案，非我编造）：
      - **Tavily best practice**: `search_depth=advanced` + `include_domains` + `max_results=10`
      - **Perplexity Discover**: 多 query 并发 + 聚合去重 + 重排
      - **Reelytics 选品工具**: 基于题材搜视频平台，不依赖 LLM 编造剧名

    完整流程：
      1. 从 coverage.genre + logline + core_value + LLM 候选剧名构造 3-5 个 query
      2. 并发跑 Tavily advanced search，`include_domains` 限定垂直短剧/漫剧平台
         （抖音/西瓜/B站/快手/好看 等业内白名单）
      3. 聚合所有结果，按平台优先级 + Tavily score 排序
      4. URL host+path 去重
      5. 取 Top N（保底 ≥ 3 条）
      6. 兜底：如果限定平台命中 < 3 条，再跑一次"不限平台"的兜底搜索补齐

    全异常吞掉，url=None 也不阻断 coverage 主流程。
    """
    # 加载搜索配置
    try:
        from agent_runtime.core.config import settings as agent_settings
        from agent_runtime.service.web_search_client import WebSearchClient
    except Exception as exc:
        logger.warning(
            "coverage_chain: web_search 模块不可用，comparable_titles 退化为纯 LLM 文本: %s",
            exc,
        )
        return [
            ComparableTitleEntry(title=t, url=None, platform="fallback")
            for t in llm_titles[:3]
        ]

    api_key = getattr(agent_settings, "WEB_SEARCH_API_KEY", None)
    if not api_key:
        logger.info(
            "coverage_chain: WEB_SEARCH_API_KEY 未配置，comparable_titles 退化为纯 LLM 文本",
        )
        return [
            ComparableTitleEntry(title=t, url=None, platform="fallback")
            for t in llm_titles[:3]
        ]

    queries = _build_search_queries(
        llm_titles=llm_titles,
        logline=logline,
        genre=genre,
        core_value=core_value,
    )
    if not queries:
        logger.info("coverage_chain: 无可构造的搜索 query，跳过 comparable_videos")
        return []

    client = WebSearchClient(
        provider=getattr(agent_settings, "WEB_SEARCH_PROVIDER", "tavily"),
        api_key=api_key,
        base_url=getattr(agent_settings, "WEB_SEARCH_BASE_URL", None),
        timeout=getattr(agent_settings, "WEB_SEARCH_TIMEOUT", 20),
    )
    try:
        # 第一轮：垂直短剧/漫剧平台限定 + advanced search
        tasks = [
            client.search(
                query=q,
                max_results=8,
                search_depth="advanced",
                include_domains=_INCLUDE_VIDEO_DOMAINS,
                exclude_domains=_EXCLUDE_DOMAINS,
            )
            for q in queries
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        entries = _aggregate_search_results(responses, target_count)

        # 兜底：限定平台 < 3 条 → 再跑一次不限平台的，补齐到至少 3
        if len(entries) < target_count and queries:
            logger.info(
                "coverage_chain: 限定平台仅命中 %d 条 (target=%d)，跑兜底无限制搜索",
                len(entries),
                target_count,
            )
            fallback_tasks = [
                client.search(
                    query=q,
                    max_results=5,
                    search_depth="advanced",
                    exclude_domains=_EXCLUDE_DOMAINS,
                )
                for q in queries[:2]  # 兜底只跑前 2 个最相关 query，控制成本
            ]
            fallback_responses = await asyncio.gather(
                *fallback_tasks, return_exceptions=True
            )
            fallback_entries = _aggregate_search_results(
                fallback_responses, target_count
            )
            # 合并：原 entries 优先，fallback 补齐
            existing_urls = {e.url for e in entries if e.url}
            for fe in fallback_entries:
                if fe.url and fe.url not in existing_urls:
                    entries.append(fe)
                    existing_urls.add(fe.url)
                if len(entries) >= max(target_count, 5):
                    break

        return entries[: max(target_count, 5)]
    except Exception as exc:
        logger.warning("coverage_chain: comparable_videos 搜索整体失败: %s", str(exc)[:200])
        return [
            ComparableTitleEntry(title=t, url=None, platform="fallback")
            for t in llm_titles[:3]
        ]
    finally:
        await client.close()


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
