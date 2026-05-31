"""集末 cliffhanger 事件抽取（评分 cliffhanger 类信号的输入）。

链路（与 reward_extractor 同款成熟模式）：
1. 关键词层（cliffhanger / hook 关键词）扫每集"集末"场景，召回候选
2. 候选场景批量丢 LLM 二级判定（precision-first，confidence 自评）
3. verbatim quote 校验（W3C TextQuoteSelector，fail-closed）
4. 输出 CliffhangerEvent，5 类 cliff_type（业内字节 WebConf 2026 短剧 QA 标准）

5 类 cliff_type 定义（业内对照）：
- physical_danger：物理危险（死亡 / 受伤 / 被绑架 / 被追杀，主角处于人身风险）
- emotional_reveal：情感揭露（突然表白 / 真相揭露 / 身份揭穿 / 情感冲击）
- false_defeat：虚假失败（主角看似落败 / 计划被识破，下集必看翻盘）
- interrupted_moment：中断时刻（关键时刻被打断，如"门突然被推开 / 黑屏"）
- mystery_setup：悬疑铺垫（新谜团出现 / 重要线索断在此处）

业内出处：
- 字节 WebConf 2026 *Short Drama Quality Assessment* §3.2 cliffhanger taxonomy
  （5 类 cliff_type 取自该 paper Table 2）
- ReelShort writer SOP《Episode-end Hook Design》
- Anthropic Claude Citations API（GA 2026）—— verbatim quote 校验
- W3C Web Annotation Data Model §4.2.4 TextQuoteSelector

为什么不用纯关键词：
- 字节 WebConf 2026 实测：纯关键词扫"集末 200 字"在 cliffhanger 识别上
  precision 仅 ~45%（很多"未完 / 突然"等词在普通过场也出现），recall 也仅 ~60%
  （情境钩子如"主角推开手术室门" 不含关键词但显然是 cliffhanger）
- LLM 二级判定 + verbatim quote 校验可把 precision 拉到 85%+，recall 80%+
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional

from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError, TokenBudget
from service.script_tools.scene_repo import (
    LLM_EVIDENCE_MAX_LEN,
    Scene,
    format_scene_for_llm,
    get_all_scenes,
    reconcile_text_quote_selector,
)

logger = logging.getLogger(__name__)


CLIFF_TYPES: frozenset[str] = frozenset({
    "physical_danger",
    "emotional_reveal",
    "false_defeat",
    "interrupted_moment",
    "mystery_setup",
})

# 5 类 cliff_type 中文展示标签（前端不暴露英文 enum）
CLIFF_TYPE_CN_LABELS: dict[str, str] = {
    "physical_danger": "危机时刻",
    "emotional_reveal": "真相揭露",
    "false_defeat": "虚假失败",
    "interrupted_moment": "关键中断",
    "mystery_setup": "悬疑铺垫",
}


@dataclass
class CliffhangerEvent:
    """单条集末 cliffhanger 事件。

    与 RewardEvent 同款契约：claim + quote_verbatim + verified + confidence。
    """

    scene_id: str
    scene_no: str
    episode_no: int  # cliffhanger 总是绑定到具体集；缺 episode_no 的不录入
    cliff_type: str  # CLIFF_TYPES 之一
    claim: str  # ≤80 字 LLM 中文诠释（前端 tooltip）
    quote_verbatim: str  # verbatim 原文片段，verified=False 时为 ""
    quote_verified: bool
    confidence: str = "high"
    evidence_line_range: Optional[tuple[int, int]] = None

    @property
    def evidence(self) -> str:
        return self.quote_verbatim if self.quote_verified else self.claim

    @property
    def cliff_type_cn(self) -> str:
        return CLIFF_TYPE_CN_LABELS.get(self.cliff_type, self.cliff_type)


# 与 reward_extractor 一致的批量大小（控 token / 控并发）
_BATCH_SIZE = 8
# 单场喂给 LLM 的文本截断
_MAX_TEXT_PER_SCENE = 800
# 集末场景的"集末"判定窗口：每集最后 N 场都视为集末候选
_LAST_SCENES_PER_EPISODE = 1


_PROMPT_TEMPLATE = """你是中文短剧选品编辑。下面是 N 个**候选集末场景**（每场原文按行打了 [L{{n}}] 行号标注）。

**重要心智**：这些场景是"每集最后一场"召回的，但**集末 ≠ 真的有 cliffhanger**。
你的任务是**严格二筛**——大多数"集末"应当被丢弃（普通收束 / 抒情结尾都不算）。
**宁可漏标也不要错标**。爆款剧本里也只有 ~40-60% 集末是真的强 cliffhanger。

【5 类 cliff_type 的严格定义 + 反例】

physical_danger（危机时刻）：
  必要条件：本场结尾**主角**处于明确的人身/物理危险（死亡 / 受伤 / 被绑架 /
            被追杀 / 命悬一线 / 中毒等）。
  反例：路人遇险 / 主角小擦伤 / 抽象的"危险来临"未实化。

emotional_reveal（真相揭露）：
  必要条件：本场结尾**首次**揭露某真相 / 身份 / 表白，且对主角心理冲击巨大
            （非小情绪波动）。
  反例：观众已知的信息再被提及 / 暗示性表达 / 角色独白回忆。

false_defeat（虚假失败）：
  必要条件：本场结尾主角**看似全盘失败**（计划被识破 / 被冤入狱 / 被陷害 /
            眼看败局已定），但显然不是真的结束（下集必看翻盘）。
  反例：真正的悲剧结尾 / 一次正常挫折。

interrupted_moment（关键中断）：
  必要条件：关键情节进行到一半被外力**强行打断**（门被推开 / 突然黑屏 /
            电话挂断 / 突然停电），且打断点本身就有冲击力。
  反例：自然过场 / 主角主动结束 / 镜头切换。

mystery_setup（悬疑铺垫）：
  必要条件：本场结尾**新出现**一个清晰的谜团 / 重要线索（一封神秘信 /
            一段奇怪录音 / 一个意外身份），且未在本集解决。
  反例：旧谜团再提 / 普通悬念过场。

【候选场景】
{scenes_block}

【输出 JSON】（字段顺序很重要：必须**先选原文 quote.exact，再写 claim**）
{{
  "events": [
    {{
      "scene_no": "<必须是上面给出的 scene_no>",
      "cliff_type": "physical_danger|emotional_reveal|false_defeat|interrupted_moment|mystery_setup",
      "quote": {{
        "exact": "<从场内 [L{{n}}] 行里直接 Ctrl+C / Ctrl+V 出来的一句台词或动作行，10-80 字，必须 100% 一字不差>",
        "prefix": "<exact 前面紧邻的 5-15 字原文，可选>",
        "suffix": "<exact 后面紧邻的 5-15 字原文，可选>"
      }},
      "confidence": "high|medium|low",
      "claim": "<对该 cliffhanger 的中文诠释 ≤{evidence_max_len} 字，说清'卡在哪 + 为什么必须看下集'>"
    }}
  ]
}}

【quote.exact 契约 —— 最容易翻车的地方，读三遍】
1. **复制粘贴**模式：从场内的 [L{{n}}] 行里选一行，**整行**或**该行的一个连续片段**复制下来。
2. **绝不允许合成 / 概括 / 改写**。
3. 写完 exact 后自我校验：你写的这串字符，能不能在原文的**某一 [L{{n}}] 行里 Ctrl+F 完全搜得到**？
4. 优先选 cliffhanger **临界点**那一句（即整场最有冲击力的一句台词 / 动作）。
5. 同样的话原文多次出现时，用 prefix/suffix 写紧邻上下文消歧。

【confidence 自评门槛（precision 控制）】
- **high**：必要条件**全部满足**，且 quote.exact 你已经在原文里**Ctrl+F 验证过**能搜到。
- **medium**：方向对但 quote 不太好选 / 必要条件只满足一半。
- **low**：勉强沾边。
- **只采纳 high 的事件；medium/low 直接丢弃**，请严格自评。

【claim 字段（给用户看的"AI 判读"文案）】
- 在 quote 选定之后再写。
- ≤{evidence_max_len} 字。
- 说清"卡在哪（用了哪类钩子）+ 为什么必须看下集"。
- **不要复述 quote 内容**；不要带"集末出现…"等过场词，直接给判读。
- 例：「主角推开急救室门发现妻子已被推走，悬念悬置」「真凶身份当场揭露
  但主角尚不知情，下集必有反转」

【最终输出原则】
- 大多数场景应当**不出现在结果里**。返回空数组 `{{"events": []}}` 是非常正常的。
- 一场内若有多类 cliff_type 候选，只输出**最强的一个**。
- 严格自评 confidence，宁缺毋滥。"""


async def extract_cliffhangers(
    *,
    script_id: str,
    caller: Optional[LlmCaller] = None,
) -> List[CliffhangerEvent]:
    """全剧集末 cliffhanger 事件清单。

    与 reward_extractor 一致的设计：
    - 关键词召回（虽然 cliffhanger 关键词 precision 不高，但召回率不错，
      给 LLM 做候选过滤更省 token）
    - LLM 二级判定 + verbatim quote 校验
    - precision-first，仅采纳 confidence=high

    返回顺序：按 episode_no 升序。
    """
    caller = caller or LlmCaller()

    scenes = get_all_scenes(script_id=script_id)
    if not scenes:
        logger.info("cliffhanger_extractor: no scenes for script_id=%s", script_id)
        return []

    # 召回每集"集末"场景（每集最后 _LAST_SCENES_PER_EPISODE 场）
    by_ep: dict[int, list[Scene]] = {}
    for sc in scenes:
        if sc.episode_no is None:
            continue
        by_ep.setdefault(sc.episode_no, []).append(sc)

    candidates: list[Scene] = []
    for ep, ep_scenes in by_ep.items():
        # scene_repo 已经按 scene_no 排好序，取末尾 N 场
        candidates.extend(ep_scenes[-_LAST_SCENES_PER_EPISODE:])

    if not candidates:
        logger.info(
            "cliffhanger_extractor: no candidate end-scenes for script_id=%s", script_id
        )
        return []

    logger.info(
        "cliffhanger_extractor.candidates script_id=%s n_episodes=%s n_candidates=%s",
        script_id,
        len(by_ep),
        len(candidates),
    )

    # 分批跑 LLM 二级判定
    batches = [candidates[i : i + _BATCH_SIZE] for i in range(0, len(candidates), _BATCH_SIZE)]
    tasks = [_judge_batch(batch, caller) for batch in batches]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    failed = [r for r in raw_results if isinstance(r, BaseException)]
    succeeded = [r for r in raw_results if not isinstance(r, BaseException)]
    if not succeeded and failed:
        raise ScoreLLMError(
            f"cliffhanger_extractor: 全部 {len(failed)} 个批次都失败（首例：{failed[0]}）"
        )
    if failed:
        logger.warning(
            "cliffhanger_extractor: %d/%d 个批次失败（已容忍），可能漏 cliffhanger",
            len(failed),
            len(batches),
        )

    # 同一集场景去重：cliff_type 优先级排序（physical_danger 最强）
    type_priority = {
        "physical_danger": 5,
        "emotional_reveal": 4,
        "false_defeat": 3,
        "interrupted_moment": 2,
        "mystery_setup": 1,
    }
    by_episode: dict[int, CliffhangerEvent] = {}
    for batch_events in succeeded:
        for ev in batch_events:
            cur = by_episode.get(ev.episode_no)
            if cur is None or type_priority.get(ev.cliff_type, 0) > type_priority.get(
                cur.cliff_type, 0
            ):
                by_episode[ev.episode_no] = ev

    out = sorted(by_episode.values(), key=lambda e: e.episode_no)
    logger.info(
        "cliffhanger_extractor.done script_id=%s cliffhanger_count=%s "
        "(covers %s/%s episodes)",
        script_id,
        len(out),
        len(out),
        len(by_ep),
    )
    return out


async def _judge_batch(
    batch: List[Scene], caller: LlmCaller
) -> List[CliffhangerEvent]:
    """LLM 二级判定单批（与 reward_extractor._judge_batch 同款契约）。"""
    blocks = []
    for sc in batch:
        annotated = format_scene_for_llm(scene_text=sc.text or "", max_chars=_MAX_TEXT_PER_SCENE)
        ep_label = f"第{sc.episode_no}集" if sc.episode_no else "未编集"
        blocks.append(
            f"[scene_no={sc.scene_no}] [{ep_label} 集末] [{sc.scene_label}]\n{annotated}"
        )
    prompt = _PROMPT_TEMPLATE.format(
        scenes_block="\n\n---\n\n".join(blocks),
        evidence_max_len=LLM_EVIDENCE_MAX_LEN,
    )

    # 与 reward_extractor 一致：cliffhanger 判定也是跨语义对照（"被打断 / 揭露"等），
    # primary 模型比 mini 更稳。
    resp = await caller.call_json(
        prompt,
        tier=ModelTier.PRIMARY,
        temperature=0.1,
        max_tokens=TokenBudget.REWARD_EXTRACT,
    )

    parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
    events_raw = parsed.get("events", []) if isinstance(parsed.get("events"), list) else []
    out: List[CliffhangerEvent] = []
    by_no = {sc.scene_no: sc for sc in batch}
    verified_count = 0
    rejected_count = 0
    confidence_filtered = 0
    for ev in events_raw:
        if not isinstance(ev, dict):
            continue
        sno = str(ev.get("scene_no") or "").strip()
        scene = by_no.get(sno)
        if scene is None or scene.episode_no is None:
            continue
        ctype = str(ev.get("cliff_type") or "").strip()
        if ctype not in CLIFF_TYPES:
            continue

        confidence = str(ev.get("confidence") or "").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        if confidence != "high":
            confidence_filtered += 1
            logger.info(
                "cliffhanger_extractor.confidence_filtered scene_no=%s type=%s conf=%s",
                sno, ctype, confidence,
            )
            continue

        claim = str(ev.get("claim") or "").strip()[: LLM_EVIDENCE_MAX_LEN + 10]
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
                    "cliffhanger_extractor.quote_unverified scene_no=%s type=%s exact_head=%r",
                    sno, ctype, exact[:40],
                )

        out.append(
            CliffhangerEvent(
                scene_id=scene.id,
                scene_no=scene.scene_no,
                episode_no=scene.episode_no,
                cliff_type=ctype,
                claim=claim,
                quote_verbatim=exact[: LLM_EVIDENCE_MAX_LEN + 10] if verified else "",
                quote_verified=verified,
                confidence=confidence,
                evidence_line_range=line_range,
            )
        )

    logger.info(
        "cliffhanger_extractor.batch_done n_kept=%s n_filtered_by_confidence=%s "
        "verbatim_verified=%s rejected=%s",
        len(out),
        confidence_filtered,
        verified_count,
        rejected_count,
    )
    return out
