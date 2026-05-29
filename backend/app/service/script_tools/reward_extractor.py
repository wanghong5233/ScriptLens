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
    parse_line_range,
)

logger = logging.getLogger(__name__)


@dataclass
class RewardEvent:
    """单条 reward 事件。

    v3.3 line-range anchored：
    - `evidence_line_range` 是 LLM 二筛时同次给出的场内行号区间（1-based 闭区间）
    - `evidence` 仅作 tooltip 展示文本（≤ LLM_EVIDENCE_MAX_LEN 字）
    """

    scene_id: str
    scene_no: str
    episode_no: Optional[int]
    event_type: str  # face_slap | reversal | revenge | romantic_progress | identity_reveal | humiliate_villain | underdog_rise | scheme_exposed
    evidence: str  # 命中片段（≤90 字），tooltip-only
    evidence_line_range: Optional[tuple[int, int]] = None


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

输出 JSON，严格遵循契约：
{{
  "events": [
    {{
      "scene_no": "<必须是上面给出的 scene_no>",
      "event_type": "face_slap|reversal|revenge|romantic_progress|identity_reveal|humiliate_villain|underdog_rise|scheme_exposed",
      "evidence_line_range": [<起始行号>, <结束行号>],
      "evidence": "<line_range 那段原文摘要，≤{evidence_max_len} 字>"
    }}
  ]
}}

evidence_line_range 规则：
- 引用该场内的 [L{{n}}] 行号（1-based 闭区间），例如 [4, 9] 表示 L4 到 L9
- 区间应**整段覆盖** reward 事件发生的那段戏（典型 4-10 行），不要给单行碎片
- 不要给整场 [1, 99]，要切到事件真正发生的那段

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
        evidence = str(ev.get("evidence") or "").strip()[: LLM_EVIDENCE_MAX_LEN + 10]
        if not evidence:
            continue
        scene_lc = len((scene.text or "").split("\n"))
        line_range = parse_line_range(ev.get("evidence_line_range"), scene_line_count=scene_lc)
        out.append(
            RewardEvent(
                scene_id=scene.id,
                scene_no=scene.scene_no,
                episode_no=scene.episode_no,
                event_type=etype,
                evidence=evidence,
                evidence_line_range=line_range,
            )
        )
    return out
