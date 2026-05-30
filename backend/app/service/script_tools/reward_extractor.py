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

    v3.4 W3C TextQuoteSelector 锚定 + v3.5 LLM 自评 confidence 过滤：

    - `claim`              LLM 自由摘要（≤ 80 字），用于 UI 卡片描述
    - `quote_verbatim`     LLM 给的 verbatim 原文片段；后端 reconcile 不通过则为空
    - `quote_verified`     verbatim 是否在 scene 内被唯一定位（W3C 标准 fail-closed）
    - `evidence_line_range`后端从 quote_verbatim 反算的行号；verified=False 时为 None
    - `confidence`         LLM 自评 (high|medium|low)；仅保留 high 写入报告（precision-first）
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
    confidence: str = "high"
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


_PROMPT_TEMPLATE = """你是中文短剧选品编辑。下面是 N 个候选场景（每场原文按行打了 [L{{n}}] 行号标注）。

**重要心智**：这些场景是关键词初筛召回的，关键词命中 ≠ 真的有 reward；
你的任务是**严格二筛**——大多数候选**应当被丢弃**。爆款剧本里 reward 密度典型也就 5-10%，
**宁可漏标也不要错标**。

【8 种 event_type 的严格定义 + 反例】

face_slap（打脸）：
  必要条件：①场前 A 曾看不起/嘲讽/误判 B；②本场 B 用事实让 A 哑口无言或承认错误。
  反例：单纯吵架 / 物理掌掴但无身份反差 / 仅一方占嘴上便宜。

reversal（反转）：
  必要条件：场内**同时出现**两段原文证据，分别表达「原本以为 X」+「事实却是 ¬X」，
            X 可以是身份、因果、立场、计划走向。**单段台词不构成 reversal。**
  反例（这些都不是 reversal）：
    - "好，那你去死好了" → 是冲突升级，不是反转
    - "递给他：喝" → 单独动作，没有"原计划"被推翻的对照
    - "原主实在是太恶毒了" → 内心反思，不是反转
    - "裴鹤年喜欢乔颜" → 是 identity_reveal 不是 reversal
  **正例特征**：场内必须既能找到"以为/打算 X"的原文，又能找到"实际是 ¬X"的原文。

revenge（复仇成功）：
  必要条件：本场反派承认/下跪/受实质损失（破产/入狱/认错），且为主角主动行动的结果。
  反例：吵架占上风 / 反派只输一回合无实质后果。

romantic_progress（CP 进展）：
  必要条件：本场两人关系发生**实质性升级**（首次吻 / 表白 / 求婚 / 在一起）。
  反例：暧昧眼神 / 单方面好感 / 牵手等微小动作。

identity_reveal（身份揭露）：
  必要条件：本场某角色的真实身份**首次在剧内**被揭示（"原来他是 XX 总裁/继承人"）。
  反例：观众已知的身份再被提及 / 仅暗示未直接揭穿。

humiliate_villain（反派败落）：本场反派被实质性"按住"（入狱 / 公开身败名裂）。
scheme_exposed（阴谋败露）：本场反派精心策划的计谋被当场拆穿，且对手知晓。
underdog_rise（逆袭）：主角凭具体行为击败长期压制者，需有**实质行为**而非仅一句口号。

【候选场景】
{scenes_block}

【输出 JSON】
{{
  "events": [
    {{
      "scene_no": "<必须是上面给出的 scene_no>",
      "event_type": "face_slap|reversal|revenge|romantic_progress|identity_reveal|humiliate_villain|underdog_rise|scheme_exposed",
      "confidence": "high|medium|low",
      "claim": "<对该 reward 的中文诠释 ≤{evidence_max_len} 字（你的判断，不要照抄原文）>",
      "quote": {{
        "exact": "<原文逐字片段：[L{{n}}] 标注里 100% 一字不差的连续文本，10-80 字>",
        "prefix": "<exact 前面紧邻的 5-15 字原文，可选>",
        "suffix": "<exact 后面紧邻的 5-15 字原文，可选>"
      }}
    }}
  ]
}}

【confidence 自评门槛（核心 precision 控制）】
- **high**：场内原文证据完整满足上面"必要条件"的全部要点；reversal 必须能在原文里同时点出"以为 X"和"实际 ¬X"两段。
- **medium**：方向看起来对但有疑点（如只有 claim 没有显式原文锚定 / 单段台词难以构成完整对照）。
- **low**：勉强沾边。
- **我们只采纳 confidence=high 的事件；medium/low 都会被直接丢弃**，所以请严格自评。

【quote.exact 契约】
- **必须**是该 scene [L{{n}}] 标注里**逐字出现**的连续文本，不能改写、概括、合并多行。
- **不能**写成"姜栀枝走进房间"这种叙述句——那是 claim。
- **要**写成"姜栀枝：你闭嘴。"或"△陆斯言把裤子提了上来"这种原文里真有的台词或动作行。
- 选 reward 事件**最关键的那一句**台词或动作行；reversal 优先选"实际 ¬X"那段。
- 同样的话原文多次出现时，用 prefix/suffix 写紧邻上下文消歧。

【最终输出原则】
- 大多数场景应当**不出现在结果里**。返回空数组 `{{"events": []}}` 是非常正常的。
- 一场内若有多个候选 type，只输出**最强的一个**。
- 严格自评 confidence，宁缺毋滥。"""


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

    # primary 模型：reversal 需要跨句跨段的语义对照（"以为 X" vs "实际 ¬X"），
    # mini 模型在 audit 上观察到误判率 80%（5 条 reversal 4 条错）。
    # 短剧总场景量 100-200 场，primary 调用一次 ~$0.02，总成本可接受。
    resp = await caller.call_json(
        prompt,
        tier=ModelTier.PRIMARY,
        temperature=0.1,
        max_tokens=TokenBudget.REWARD_EXTRACT,
    )

    parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
    events_raw = parsed.get("events", []) if isinstance(parsed.get("events"), list) else []
    out: List[RewardEvent] = []
    by_no = {sc.scene_no: sc for sc in batch}
    verified_count = 0
    rejected_count = 0
    confidence_filtered = 0
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

        # precision-first：只保留 confidence=high 的事件
        confidence = str(ev.get("confidence") or "").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"  # LLM 没自评 → 视为 low
        if confidence != "high":
            confidence_filtered += 1
            logger.info(
                "reward_extractor.confidence_filtered scene_no=%s type=%s conf=%s "
                "(只采纳 high 置信事件 → 丢弃)",
                sno, etype, confidence,
            )
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
                confidence=confidence,
                evidence_line_range=line_range,
            )
        )
    logger.info(
        "reward_extractor.batch_done n_kept=%s n_filtered_by_confidence=%s "
        "verbatim_verified=%s rejected=%s",
        len(out),
        confidence_filtered,
        verified_count,
        rejected_count,
    )
    return out
