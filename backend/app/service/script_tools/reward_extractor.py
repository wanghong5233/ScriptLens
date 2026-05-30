"""reward 事件抽取（爽点密度评分的输入）。

链路：
1. 关键词层（risk_terms.REWARD_TERMS）扫全部场景，召回候选
2. 候选场景按集分批（每批 ≤ 8 场，控制 prompt 长度）丢给 mini 模型，
   要求严格 JSON 输出 [{scene_no, type, evidence}]，二级判定剔除假阳性

设计理由：
- 短剧动辄 100 集 / 5000 段，纯 LLM 全量扫不可行（token 爆炸 + 慢 + 贵）
- 关键词层召回率 > 精确率：宁可多召回让 LLM 二筛，也不放过真 reward
- mini 模型（gpt-5-mini）成本只有 primary 的 1/10，二级判定上量合算
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional

from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError, TokenBudget
from service.script_tools.risk_terms import REWARD_TERMS, all_reward_terms
from service.script_tools.scene_repo import (
    LLM_EVIDENCE_MAX_LEN,
    Scene,
    format_scene_for_llm,
    get_all_scenes,
    locate_scenes_by_keyword,
    reconcile_text_quote_selector,
)

logger = logging.getLogger(__name__)


@dataclass
class RewardEvent:
    """单条 reward 事件。

    v3.4 W3C TextQuoteSelector 锚定（取代 v3.3 的 line_range 契约）：

    - `claim`              LLM 自由摘要（≤ 80 字），用于 UI 卡片描述
    - `quote_verbatim`     LLM 给的 verbatim 原文片段；后端 reconcile 不通过则为空
    - `quote_verified`     verbatim 是否在 scene 内被唯一定位（W3C 标准 fail-closed）
    - `evidence_line_range`后端从 quote_verbatim 反算的行号；verified=False 时为 None
    - `evidence`           **下游展示主字段**（向后兼容）：
                           - verified=True  → 取 quote_verbatim（前端可逐字高亮）
                           - verified=False → 取 claim（前端只能跳整场，不再误导性高亮某行）

    业内对照：
    - W3C Web Annotation Data Model §4.2.4 TextQuoteSelector
    - Anthropic Claude Citations API（GA 2026）—— "citations are guaranteed to
      contain valid pointers to the provided documents"
    - AI Alliance Semiont reconcileSelector —— "LLM does NOT supply offsets; it
      supplies `exact` (a verbatim substring); our code computes start/end by
      searching the source"
    """

    scene_id: str
    scene_no: str
    episode_no: Optional[int]
    event_type: str  # face_slap | reversal | revenge | romantic_progress | identity_reveal | humiliate_villain | underdog_rise | scheme_exposed
    claim: str
    quote_verbatim: str
    quote_verified: bool
    evidence_line_range: Optional[tuple[int, int]] = None

    @property
    def evidence(self) -> str:
        """下游 (`script_report_service` 等) 沿用的展示字段。

        verified 时给 verbatim 原文，未 verified 时给 LLM 的 claim 摘要——
        二者都是 ≤80 字，UI tooltip / 卡片描述都能用。
        """
        return self.quote_verbatim if self.quote_verified else self.claim


# LLM 二级判定单批容量
# 8 = TokenBudget.REWARD_EXTRACT (2560) / 单场判定 ~300 token 经验值；
# 详见 docs/08 §6.3 TokenBudget 推导。
_BATCH_SIZE = 8

# 单场喂给 LLM 的文本截断
# 800 字 = 短剧典型单场上限；超出基本是场景大段背景描述，对 reward 判定不增益。
_MAX_TEXT_PER_SCENE = 800


_PROMPT_TEMPLATE = """你是中文短剧爆款分析师。下面是 N 个候选场景（每场原文按行打了 [L{{n}}] 行号标注），
每场可能含 reward 事件（打脸 / 反转 / 复仇 / CP 进展 / 身份揭露 / 反派败落 / 逆袭 / 阴谋败露）。

判定规则：
1. 必须是「事件已经发生」，不是预告或回忆。
2. 同一场景多次命中只算一次，选最强的一类。
3. 单纯说一句脏话、推搡 → 不是 reward。

【候选场景】
{scenes_block}

输出 JSON（严格遵循契约 / W3C TextQuoteSelector 范式）：
{{
  "events": [
    {{
      "scene_no": "<必须是上面给出的 scene_no>",
      "event_type": "face_slap|reversal|revenge|romantic_progress|identity_reveal|humiliate_villain|underdog_rise|scheme_exposed",
      "claim": "<对该 reward 的中文摘要 ≤{evidence_max_len} 字（你的判断，不要照抄原文）>",
      "quote": {{
        "exact": "<原文逐字片段：必须是上面 [L{{n}}] 标注里 100% 一字不差出现过的连续文本，10-80 字>",
        "prefix": "<exact 前面紧邻的 5-15 字原文，用于消歧（可选，留空字符串也行）>",
        "suffix": "<exact 后面紧邻的 5-15 字原文，用于消歧（可选，留空字符串也行）>"
      }}
    }}
  ]
}}

quote.exact 是核心字段（用户跳转高亮就用这段原文）：
- **必须**是上面对应 scene 的 [L{{n}}] 标注里**逐字出现**的连续文本，不能改写、概括、合并多行
- **不能**写成"姜栀枝走进房间"这种叙述句——那是你的 claim
- **要**写成"姜栀枝：你闭嘴。"或"△陆斯言把裤子提了上来"这种原文里真有的台词或动作行
- 长度 10-80 字；选 reward 事件最关键的那一句台词或动作行
- 如果原文里同样的话出现了多次（"等下" "好" 之类），用 prefix/suffix 写紧邻上下文消歧

claim vs quote.exact 的分工：
- claim = 你对该 reward 的诠释（"打脸 X，因为 Y" / "反转 X 揭露 Y"）
- exact = 原文里真实出现过的那句台词，证据本身

未命中的场景不要列出。如果没有任何场景命中，返回 {{"events": []}}。"""


async def extract_reward_events(
    *,
    script_id: str,
    caller: Optional[LlmCaller] = None,
    max_scenes: int = 200,
) -> List[RewardEvent]:
    """全剧 reward 事件清单（用于 reward_density 维度评分）。

    返回顺序：按 episode_no / scene_no。同一场景同一 type 只保留一条。
    """
    caller = caller or LlmCaller()
    keyword_hits = locate_scenes_by_keyword(
        script_id=script_id,
        keywords=all_reward_terms(),
        limit=max_scenes,
    )
    if not keyword_hits:
        logger.info("reward_extractor: no keyword hits for script_id=%s", script_id)
        return []

    logger.info(
        "reward_extractor.candidates script_id=%s n=%s",
        script_id,
        len(keyword_hits),
    )

    # 分批跑 LLM 二级判定（单批失败容忍，全部失败 → fail aloud）
    batches = [keyword_hits[i : i + _BATCH_SIZE] for i in range(0, len(keyword_hits), _BATCH_SIZE)]
    tasks = [_judge_batch(batch, caller) for batch in batches]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    failed = [r for r in raw_results if isinstance(r, BaseException)]
    succeeded = [r for r in raw_results if not isinstance(r, BaseException)]
    if not succeeded and failed:
        # 全部批次都失败 → 真故障，向上抛
        raise ScoreLLMError(
            f"reward_extractor: 全部 {len(failed)} 个批次判定都失败（首例：{failed[0]}）"
        )
    if failed:
        logger.warning(
            "reward_extractor: %d/%d 个批次失败（已容忍），可能漏 reward",
            len(failed),
            len(batches),
        )
    results: List[List[RewardEvent]] = succeeded

    # 按场景去重（同 scene 多 type 时选最强：复仇 > 反转 > 身份揭露 > CP > 打脸 > 阴谋 > 反派败落 > 逆袭）
    type_priority = {
        "revenge": 8,
        "reversal": 7,
        "identity_reveal": 6,
        "romantic_progress": 5,
        "humiliate_villain": 4,
        "scheme_exposed": 3,
        "face_slap": 2,
        "underdog_rise": 1,
    }
    by_scene: dict[str, RewardEvent] = {}
    for batch_events in results:
        for ev in batch_events:
            cur = by_scene.get(ev.scene_id)
            if cur is None or type_priority.get(ev.event_type, 0) > type_priority.get(cur.event_type, 0):
                by_scene[ev.scene_id] = ev

    out = sorted(
        by_scene.values(),
        key=lambda e: (e.episode_no if e.episode_no is not None else 9999, e.scene_no),
    )
    logger.info(
        "reward_extractor.done script_id=%s reward_count=%s",
        script_id,
        len(out),
    )
    return out


async def _judge_batch(batch: List[Scene], caller: LlmCaller) -> List[RewardEvent]:
    """LLM 二级判定单批。

    失败策略（fail aloud）：
    - 单批 LLM 失败 → 把 ScoreLLMError 抛出去，由上层 `extract_reward_events`
      根据「全失败 / 部分失败」分别走 raise / 容忍逻辑。
    - 不在本函数静默返回 []，避免「全失败但流水线以为只是 reward 少」的幽灵故障。
    """
    blocks = []
    for sc in batch:
        annotated = format_scene_for_llm(scene_text=sc.text or "", max_chars=_MAX_TEXT_PER_SCENE)
        ep_label = f"第{sc.episode_no}集" if sc.episode_no else "未编集"
        blocks.append(
            f"[scene_no={sc.scene_no}] [{ep_label}] [{sc.scene_label}]\n{annotated}"
        )
    prompt = _PROMPT_TEMPLATE.format(
        scenes_block="\n\n---\n\n".join(blocks),
        evidence_max_len=LLM_EVIDENCE_MAX_LEN,
    )

    resp = await caller.call_json(
        prompt,
        tier=ModelTier.MINI,
        temperature=0.1,
        max_tokens=TokenBudget.REWARD_EXTRACT,
    )

    parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
    events_raw = parsed.get("events", []) if isinstance(parsed.get("events"), list) else []
    out: List[RewardEvent] = []
    by_no = {sc.scene_no: sc for sc in batch}
    verified_count = 0
    rejected_count = 0
    for ev in events_raw:
        if not isinstance(ev, dict):
            continue
        sno = str(ev.get("scene_no") or "").strip()
        scene = by_no.get(sno)
        if scene is None:
            continue
        etype = str(ev.get("event_type") or "").strip()
        if etype not in REWARD_TERMS:
            continue

        claim = str(ev.get("claim") or ev.get("evidence") or "").strip()[: LLM_EVIDENCE_MAX_LEN + 10]
        if not claim:
            continue

        quote_raw = ev.get("quote")
        exact = ""
        prefix = ""
        suffix = ""
        if isinstance(quote_raw, dict):
            exact = str(quote_raw.get("exact") or "").strip()
            prefix = str(quote_raw.get("prefix") or "").strip()
            suffix = str(quote_raw.get("suffix") or "").strip()

        line_range: Optional[tuple[int, int]] = None
        verified = False
        if exact:
            line_range = reconcile_text_quote_selector(
                scene_text=scene.text or "",
                exact=exact,
                prefix=prefix or None,
                suffix=suffix or None,
            )
            if line_range is not None:
                verified = True
                verified_count += 1
            else:
                rejected_count += 1
                logger.warning(
                    "reward_extractor.quote_unverified scene_no=%s type=%s exact_head=%r "
                    "(LLM 给的 verbatim 在 scene 里搜不到或多义无法消歧 → 降级为整场跳转)",
                    sno,
                    etype,
                    exact[:40],
                )

        out.append(
            RewardEvent(
                scene_id=scene.id,
                scene_no=scene.scene_no,
                episode_no=scene.episode_no,
                event_type=etype,
                claim=claim,
                quote_verbatim=exact[: LLM_EVIDENCE_MAX_LEN + 10] if verified else "",
                quote_verified=verified,
                evidence_line_range=line_range,
            )
        )
    if out:
        logger.info(
            "reward_extractor.batch_verified n=%s verified=%s rejected=%s (verbatim 命中率=%s)",
            len(out),
            verified_count,
            rejected_count,
            f"{verified_count * 100 / len(out):.0f}%",
        )
    return out
