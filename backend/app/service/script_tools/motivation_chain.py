"""动机自洽度评分（rubric §3.3）。

链路：
1. 用关键词扫出"关键决策候选场景"（结婚 / 复仇 / 原谅 / 背叛 / 离开 / 牺牲 / 反目）
2. 每个候选场景回扫前 5 场上下文，丢给 primary 模型判定：
   - 该决策有几个铺垫信号？
   - 是否构成 OOC？
3. 聚合所有决策的判定 → motivation 维度分（4 档锚点见 rubric §3.3）

"关键决策"的定义：剧情转折点的角色行为，不是日常对白。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError
from service.script_tools.scene_repo import (
    Scene,
    get_all_scenes,
    get_scenes_around,
    locate_scenes_by_keyword,
)

logger = logging.getLogger(__name__)


_DECISION_KEYWORDS = [
    # 婚恋决策
    "求婚", "结婚", "离婚", "退婚", "悔婚", "私奔", "分手", "和好", "复合",
    # 复仇 / 报复
    "复仇", "报仇", "报复", "我要让", "血债血偿", "原谅你", "放过你", "饶了你",
    # 背叛 / 牺牲
    "背叛", "出卖", "牺牲", "替他", "代替", "顶罪",
    # 离开 / 决裂
    "我走了", "再也不见", "断绝关系", "脱离", "我不认你",
    # 揭露 / 摊牌
    "我知道了", "我都知道", "其实我", "我才是", "我才不是",
]


@dataclass
class DecisionJudgement:
    decision_scene_id: str
    decision_scene_no: str
    decision_excerpt: str
    setup_count: int  # 0-N 个铺垫信号
    is_ooc: bool
    rationale: str


@dataclass
class MotivationResult:
    """rubric §6：score/level 在「证据不足」时为 None；其余正常 0-10/三档。"""

    score: Optional[int]
    level: Optional[str]  # high | medium | low | None
    reason: str
    evidence_ref_ids: List[str]
    judged_decisions: List[DecisionJudgement] = field(default_factory=list)


_PROMPT_TEMPLATE = """你是中文短剧剧本审稿专家。下面给你一个【关键决策场景】和它前面 N 场的【上下文场景】。

任务：判断该决策是否有铺垫、是否构成 OOC。

判定规则：
1. 「铺垫」= 上下文里出现过的、能为该决策提供动机的事件 / 对白 / 角色情绪线索。
2. 「OOC」= 该决策与角色之前已确立的行为 / 价值观 / 设定直接矛盾。
3. 上下文里完全没有相关信号 → setup_count=0；找到 1-2 条 → 1 或 2；≥3 条 → 3。

【关键决策场景 scene_no={decision_scene_no}】
[{decision_scene_label}]
{decision_text}

【上下文（前 {ctx_n} 场，按时间倒序）】
{context_block}

输出 JSON：
{{
  "setup_count": <整数 0-5>,
  "is_ooc": <true|false>,
  "rationale": "<≤80 字，必须引用具体场号或角色名>"
}}"""


async def score_motivation(
    *,
    script_id: str,
    caller: Optional[LlmCaller] = None,
    max_decisions: int = 12,
    context_window: int = 5,
) -> MotivationResult:
    """主入口。返回 motivation 维度的最终分 + 各决策判定。

    评分策略（rubric §3.3）：
      - all decisions setup_count ≥ 2 且 ooc=0 → 9-10
      - 多数 setup_count ≥ 1 且 ooc ≤ 2 → 6-8
      - 至少 1 个核心决策 setup=0 / ooc 3-5 → 3-5
      - ooc > 5 或 ≥ 2 个核心决策 setup=0 → 0-2
    """
    caller = caller or LlmCaller()

    candidates = locate_scenes_by_keyword(
        script_id=script_id,
        keywords=_DECISION_KEYWORDS,
        limit=max_decisions * 3,  # 关键词宽召回
    )

    # 决策候选为空（短剧少见，但场景切分异常 / 极简剧本时可能发生）→ rubric §6 证据不足
    if not candidates:
        return MotivationResult(
            score=None,
            level=None,
            reason="未识别到关键决策场景（剧本可能未含转折性对白或场景切分异常）",
            evidence_ref_ids=[],
        )

    # LLM 一级筛：从关键词召回里挑「真的是关键决策」的（避免角色嘴上说"我不认你"但只是日常吵架）
    real_decisions = await _filter_real_decisions(candidates[: max_decisions * 2], caller)
    if not real_decisions:
        # 关键词召回但 LLM 二筛无真实决策 → 证据不足（不伪造 6/medium）
        return MotivationResult(
            score=None,
            level=None,
            reason=f"关键词召回 {len(candidates)} 场，LLM 二筛后均不构成关键决策，动机维度证据不足",
            evidence_ref_ids=[],
        )

    real_decisions = real_decisions[:max_decisions]

    # 并行回扫每个决策的前 N 场上下文（单决策失败容忍，全部失败 fail aloud）
    tasks = [
        _judge_one(decision, script_id, context_window, caller)
        for decision in real_decisions
    ]
    judgements: List[Optional[DecisionJudgement]] = await asyncio.gather(
        *tasks, return_exceptions=False
    )
    judged: List[DecisionJudgement] = [j for j in judgements if j is not None]
    if not judged:
        # 所有决策判定都失败 → 真故障 fail aloud（不再返回伪 5/medium）
        raise ScoreLLMError(
            f"motivation: {len(real_decisions)} 个决策场景的 LLM 回扫全部失败，"
            f"无法给出动机维度评分"
        )

    # 聚合
    no_setup_count = sum(1 for j in judged if j.setup_count == 0)
    ooc_count = sum(1 for j in judged if j.is_ooc)
    setup2_count = sum(1 for j in judged if j.setup_count >= 2)
    n = len(judged)

    if ooc_count > 5 or no_setup_count >= 2:
        score, level = 2, "low"
    elif no_setup_count >= 1 or 3 <= ooc_count <= 5:
        score, level = 4, "low"
    elif setup2_count == n and ooc_count == 0:
        score, level = 9, "high"
    else:
        score, level = 7, "medium"

    reason = (
        f"评估 {n} 个关键决策："
        f"{setup2_count} 个铺垫充足、{no_setup_count} 个无铺垫、{ooc_count} 个 OOC"
    )
    evidence_ref_ids = [j.decision_scene_id for j in judged[:5]]
    return MotivationResult(
        score=score,
        level=level,
        reason=reason,
        evidence_ref_ids=evidence_ref_ids,
        judged_decisions=judged,
    )


async def _filter_real_decisions(
    candidates: List[Scene],
    caller: LlmCaller,
) -> List[Scene]:
    """LLM 一级筛：把关键词召回里的真"关键决策"挑出来（一次 batch 完成）。"""
    if not candidates:
        return []
    blocks = []
    for sc in candidates:
        excerpt = (sc.text or "")[:300]
        blocks.append(f"[scene_no={sc.scene_no}] {excerpt}")
    prompt = (
        "以下是中文短剧的候选场景。挑出真正属于「角色关键决策点」的场景"
        "（结婚 / 复仇 / 原谅 / 背叛 / 离开 / 牺牲 / 摊牌等转折性行为），"
        "排除日常吵架、玩笑话、回忆叙述。\n\n"
        + "\n---\n".join(blocks)
        + '\n\n输出 JSON：{"scene_nos": ["1-3", "5-2", ...]}'
    )
    try:
        resp = await caller.call_json(prompt, tier=ModelTier.MINI, temperature=0.1, max_tokens=512)
    except ScoreLLMError as e:
        logger.warning("decision filter failed, fall back to keyword-only: %s", e)
        return candidates  # 失败时全保留
    nos = resp.parsed.get("scene_nos", []) if isinstance(resp.parsed, dict) else []
    nos_set = {str(n) for n in nos}
    return [sc for sc in candidates if sc.scene_no in nos_set]


async def _judge_one(
    decision: Scene,
    script_id: str,
    ctx_window: int,
    caller: LlmCaller,
) -> Optional[DecisionJudgement]:
    """对一个决策场景跑回扫判定。"""
    around = get_scenes_around(
        script_id=script_id,
        target_scene_id=decision.id,
        before=ctx_window,
        after=0,
    )
    # around 末尾就是 decision 本身；上下文是前 N 场（不含 decision）
    context_scenes = [s for s in around if s.id != decision.id][-ctx_window:]
    if not context_scenes:
        # 第一场就是决策（罕见），无上下文可判
        return DecisionJudgement(
            decision_scene_id=decision.id,
            decision_scene_no=decision.scene_no,
            decision_excerpt=(decision.text or "")[:120],
            setup_count=0,
            is_ooc=False,
            rationale="该决策出现在剧本第一场，无前序上下文",
        )

    # 上下文按时间倒序（最近的在前），让 LLM 更关注近期信号
    ctx_blocks = []
    for sc in reversed(context_scenes):
        excerpt = (sc.text or "")[:400]
        ctx_blocks.append(f"[scene_no={sc.scene_no}] [{sc.scene_label}]\n{excerpt}")

    prompt = _PROMPT_TEMPLATE.format(
        decision_scene_no=decision.scene_no,
        decision_scene_label=decision.scene_label or "",
        decision_text=(decision.text or "")[:600],
        ctx_n=len(context_scenes),
        context_block="\n---\n".join(ctx_blocks),
    )
    try:
        resp = await caller.call_json(
            prompt, tier=ModelTier.PRIMARY, temperature=0.1, max_tokens=512
        )
    except ScoreLLMError as e:
        logger.warning(
            "motivation judge failed scene_no=%s: %s",
            decision.scene_no,
            e,
        )
        return None

    parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
    setup_count = int(parsed.get("setup_count", 0) or 0)
    is_ooc = bool(parsed.get("is_ooc", False))
    rationale = str(parsed.get("rationale") or "")[:200]
    return DecisionJudgement(
        decision_scene_id=decision.id,
        decision_scene_no=decision.scene_no,
        decision_excerpt=(decision.text or "")[:120],
        setup_count=max(0, min(5, setup_count)),
        is_ooc=is_ooc,
        rationale=rationale,
    )
