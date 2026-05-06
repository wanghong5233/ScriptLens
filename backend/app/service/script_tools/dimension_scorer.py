"""3 维 LLM 评分入口：opening_hook / reward_density / pacing。

motivation / risk 各自走专用 scorer（motivation_chain / risk_screener），
不放本模块；本模块只放「档位 prompt + LLM」型评分。

为什么不把所有维度装一个万能 score_dimension：
- opening_hook 输入是「前 3 集场景文本」
- reward_density 输入是「reward 事件列表 + 全剧集数」
- pacing 输入是「分集事件数序列（纯数字）+ 方差」
三者的 prompt 形态完全不同，强行统一 prompt 会让档位判据失焦。

不变式（rubric §6 + core-principles fail aloud）：
- 任何 LLM 调用失败：先重试一次（提温度提多样性）；二次仍失败 → 抛 ScoreLLMError
- LLM 返回 evidence_scene_nos 为空：用强化 prompt 重试一次；二次仍空 →
  返回 score=None / level=None / reason="证据不足"（**不允许伪造默认 5/medium**）
- LLM 返回的 evidence_scene_nos 必须出自输入场号集；非法 scene_no 直接丢弃
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import List, Optional

from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError
from service.script_tools.reward_extractor import RewardEvent
from service.script_tools.scene_repo import (
    Scene,
    get_all_scenes,
    get_first_episode_scenes,
)

logger = logging.getLogger(__name__)


# rubric §4.1 通用骨架硬约束（评分 prompt 必须包含）
_HARD_CONSTRAINTS = """【硬约束】
1. evidence_scene_nos 必须 ≥1 条且属于上面给出的场号集合，否则你的输出无效。
2. reason 不准出现「秒/镜头/画面/特效/分镜/拍摄」等成片词汇（剧本是文本）。
3. score 与 level 必须落到同一档（9-10→high；6-8→high；3-5→medium；0-2→low）。"""

_RETRY_HINT = """\n\n注意：你刚才的输出未给 evidence_scene_nos（或非法）。请重新打分，"""\
"""**必须**给出 ≥1 条 evidence_scene_nos，并且只能从下方【场景】里给出的 scene_no 里挑。"""


@dataclass
class ScoreOutput:
    """rubric §6：score/level 在「证据不足」时为 None；其余正常 0-10/三档。"""

    score: Optional[int]
    level: Optional[str]  # high | medium | low
    reason: str
    evidence_ref_ids: List[str] = field(default_factory=list)


_INSUFFICIENT = ScoreOutput(score=None, level=None, reason="证据不足", evidence_ref_ids=[])


# ============================================================
# opening_hook（rubric §3.1）
# ============================================================


_OPENING_PROMPT = (
    """你是中文短剧爆款分析师。下面是某剧的【前 3 集前若干场】原文。

任务：判定 opening_hook（开场钩子强度），按以下 4 档锚点选最匹配的一档：

| 档 | 信号 |
|---|---|
| 9-10（high） | 首场 20 段内出现死亡 / 绝症 / 离婚 / 重生 / 穿越 / 阴谋揭露 / 当众羞辱 任一；首集结尾留明确钩子 |
| 6-8（high） | 首场内有冲突或反差但非极强；首集前 3 场至少 2 次冲突事件 |
| 3-5（medium） | 首集前 3 场只有 1 次冲突 / 多数在交代背景或日常 |
| 0-2（low） | 首集前 3 场只有人物介绍和环境描写，无冲突 / 首场超过 30 段没有事件 |

【场景】
{scenes_block}

输出 JSON（严格契约）：
{{
  "score": <0-10 整数>,
  "level": "high|medium|low",
  "reason": "<≤80 字，必须引用具体场号>",
  "evidence_scene_nos": ["1-1", "1-2"]
}}

"""
    + _HARD_CONSTRAINTS
)


async def score_opening_hook(
    *,
    script_id: str,
    caller: Optional[LlmCaller] = None,
    n_episodes: int = 3,
    max_scenes: int = 9,
    max_text_per_scene: int = 800,
) -> ScoreOutput:
    """rubric §3.1。fail aloud：LLM 二次失败抛错；evidence 二次为空 → score=None。"""
    caller = caller or LlmCaller()
    scenes = get_first_episode_scenes(script_id=script_id, n_episodes=n_episodes)
    scenes = scenes[:max_scenes]
    if not scenes:
        # 信息缺失（rubric §6）：剧本切分异常或集号缺失
        return ScoreOutput(
            score=None,
            level=None,
            reason="无开场场景可读（剧本切分可能异常）",
            evidence_ref_ids=[],
        )

    blocks = []
    for sc in scenes:
        text = (sc.text or "")[:max_text_per_scene]
        ep = f"第{sc.episode_no}集" if sc.episode_no else "未编集"
        blocks.append(f"[scene_no={sc.scene_no}] [{ep}] [{sc.scene_label}]\n{text}")
    base_prompt = _OPENING_PROMPT.format(scenes_block="\n\n---\n\n".join(blocks))

    return await _call_with_evidence_retry(
        caller=caller,
        base_prompt=base_prompt,
        scenes=scenes,
        log_tag="score_opening_hook",
    )


# ============================================================
# reward_density（rubric §3.2）—— 比值驱动 + LLM 翻译
# ============================================================


_REWARD_PROMPT = (
    """你是中文短剧爆款分析师。下面是某剧的【reward 事件统计 + 抽样事件清单】。

任务：基于统计指标按 4 档锚点判定 reward_density（爽点密度）：

| 档 | 信号 |
|---|---|
| 9-10（high） | reward / 集数比值 ≥ 3.0 ；连续 ≥3 集无 reward 段 ≤ 1 处 |
| 6-8（high） | 比值 1.5-3.0 ；连续 ≥3 集无 reward 段 ≤ 3 处 |
| 3-5（medium） | 比值 0.5-1.5 ；存在连续 5+ 集无 reward 段 |
| 0-2（low） | 比值 < 0.5 ；中后段连续 8+ 集无 reward |

【统计】
总集数：{n_episodes}
reward 事件总数：{n_rewards}
比值（events / episodes）：{ratio:.2f}
最长连续无 reward 集数：{max_dry_streak}

【抽样 reward 事件（前 6 个）】
{events_block}

输出 JSON：
{{
  "score": <0-10 整数>,
  "level": "high|medium|low",
  "reason": "<≤80 字，必须引用比值或具体场号>",
  "evidence_scene_nos": ["..."]
}}

"""
    + _HARD_CONSTRAINTS
)


async def score_reward_density(
    *,
    script_id: str,
    reward_events: List[RewardEvent],
    total_episodes: int,
    caller: Optional[LlmCaller] = None,
) -> ScoreOutput:
    """rubric §3.2。reward 事件为 0 时直接 score=0/level=low（不需 LLM）。"""
    caller = caller or LlmCaller()

    if total_episodes <= 0:
        # 集号缺失（fallback 切分）→ 用场景数 / 30 估算等效集数
        total_episodes = max(1, len(reward_events) // 2)

    n_rewards = len(reward_events)
    ratio = n_rewards / total_episodes

    # 0 reward → 不需要 LLM，按 rubric §3.2 直接 0-2 档（low）
    if n_rewards == 0:
        return ScoreOutput(
            score=1,
            level="low",
            reason=f"全剧未识别到 reward 事件（{total_episodes} 集），爽点密度极低",
            evidence_ref_ids=[],
        )

    max_dry = _max_dry_streak(reward_events, total_episodes)

    sample_events = reward_events[:6]
    events_block = "\n".join(
        f"- [scene_no={ev.scene_no}] [{ev.event_type}] {ev.evidence}"
        for ev in sample_events
    )

    base_prompt = _REWARD_PROMPT.format(
        n_episodes=total_episodes,
        n_rewards=n_rewards,
        ratio=ratio,
        max_dry_streak=max_dry,
        events_block=events_block,
    )

    out = await _call_with_evidence_retry(
        caller=caller,
        base_prompt=base_prompt,
        scenes=_scenes_from_events(reward_events),
        log_tag="score_reward_density",
    )
    if out.score is not None and not out.reason:
        out.reason = f"reward/集数比 {ratio:.2f}，最长连续无 reward {max_dry} 集"
    return out


def _max_dry_streak(events: List[RewardEvent], total_eps: int) -> int:
    """计算最长「连续无 reward 集数」。简化算法：按 episode_no 分布 → diff 最大值。"""
    if not events or total_eps <= 0:
        return total_eps
    eps_with_reward = sorted({ev.episode_no for ev in events if ev.episode_no is not None})
    if not eps_with_reward:
        return total_eps
    prev = 0
    max_gap = 0
    for ep in eps_with_reward:
        gap = ep - prev - 1
        if gap > max_gap:
            max_gap = gap
        prev = ep
    tail = total_eps - prev
    if tail > max_gap:
        max_gap = tail
    return max_gap


def _scenes_from_events(events: List[RewardEvent]) -> List[Scene]:
    """伪 Scene 对象列表：让 _build_score_output 能反查 scene_no -> scene_id。"""
    return [
        Scene(
            id=ev.scene_id,
            script_id="",
            episode_no=ev.episode_no,
            scene_no=ev.scene_no,
            scene_label="",
            characters=[],
            start_line=None,
            end_line=None,
            text=ev.evidence,
        )
        for ev in events
    ]


# ============================================================
# pacing（rubric §3.4）—— 纯量化 + LLM 翻译
# ============================================================


_PACING_PROMPT = (
    """你是中文短剧节奏审稿专家。下面是某剧分集事件数（场景数 + reward 事件数）的统计。

判定 pacing（节奏控制）档位：

| 档 | 信号 |
|---|---|
| 9-10（high） | 方差小；无连续 3+ 集低密度段 |
| 6-8（high） | 方差中等；连续 3+ 集低密度段 ≤ 2 处 |
| 3-5（medium） | 中段塌陷（中段平均 < 全剧均值 70%）|
| 0-2（low） | 中后段连续 5+ 集低密度 / 总集 ≥ 60 但 reward ≤ 30 |

【统计】
总集数：{n_episodes}
全剧均值（事件/集）：{mean:.2f}
方差：{variance:.2f}
最长连续低密度段（事件数 < 均值 50%）集数：{max_dry_streak}
中段（中间 1/3 集）平均：{mid_mean:.2f}（占全剧均值 {mid_ratio:.0%}）

【分集事件序列】
{series}

输出 JSON：
{{
  "score": <0-10 整数>,
  "level": "high|medium|low",
  "reason": "<≤80 字，引用方差/塌陷集数等具体数字>",
  "evidence_scene_nos": []
}}

【pacing 维度特别说明】evidence_scene_nos 可为空（pacing 是分布维度，不强制定位单场景）。
其它硬约束：
1. reason 不准出现「秒/镜头/画面/特效/分镜/拍摄」等成片词汇。
2. score 与 level 必须落到同一档。"""
)


async def score_pacing(
    *,
    script_id: str,
    reward_events: List[RewardEvent],
    caller: Optional[LlmCaller] = None,
) -> ScoreOutput:
    """rubric §3.4。pacing 是分布量化维度，**不强制 evidence**（与 §6 重试规则不同）。"""
    caller = caller or LlmCaller()
    scenes = get_all_scenes(script_id=script_id)
    if not scenes:
        return ScoreOutput(
            score=None,
            level=None,
            reason="无场景数据可评（剧本切分可能异常）",
            evidence_ref_ids=[],
        )

    by_ep_scene = _count_by_episode([s.episode_no for s in scenes])
    by_ep_reward = _count_by_episode([ev.episode_no for ev in reward_events])

    if not by_ep_scene:
        return ScoreOutput(
            score=None,
            level=None,
            reason="剧本无集号信息（fallback 切分），pacing 维度不可评",
            evidence_ref_ids=[],
        )

    if len(by_ep_scene) < 3:
        # rubric §6：集数 < 5 时 pacing 不输出（样本量不够）；放宽到 < 3 才彻底拒
        return ScoreOutput(
            score=None,
            level=None,
            reason=f"剧本仅 {len(by_ep_scene)} 集，集数过少无法评 pacing 维度",
            evidence_ref_ids=[],
        )

    eps_sorted = sorted(by_ep_scene.keys())
    series = [by_ep_scene[e] + by_ep_reward.get(e, 0) for e in eps_sorted]
    n_eps = len(series)
    mean = statistics.fmean(series) if series else 0.0
    variance = statistics.pvariance(series) if len(series) > 1 else 0.0

    mid_lo = n_eps // 3
    mid_hi = max(mid_lo + 1, 2 * n_eps // 3)
    mid_series = series[mid_lo:mid_hi]
    mid_mean = statistics.fmean(mid_series) if mid_series else mean
    mid_ratio = mid_mean / mean if mean > 0 else 1.0

    threshold = mean * 0.5
    max_dry = 0
    cur = 0
    for v in series:
        if v < threshold:
            cur += 1
            if cur > max_dry:
                max_dry = cur
        else:
            cur = 0

    series_str = ", ".join(f"E{e}={by_ep_scene[e] + by_ep_reward.get(e, 0)}" for e in eps_sorted[:30])
    if len(eps_sorted) > 30:
        series_str += f" ...（共 {len(eps_sorted)} 集，仅展示前 30）"

    prompt = _PACING_PROMPT.format(
        n_episodes=n_eps,
        mean=mean,
        variance=variance,
        max_dry_streak=max_dry,
        mid_mean=mid_mean,
        mid_ratio=mid_ratio,
        series=series_str,
    )
    # pacing 不要求 evidence；LLM 失败抛 fail aloud
    try:
        resp = await caller.call_json(prompt, tier=ModelTier.PRIMARY, temperature=0.1, max_tokens=512)
    except ScoreLLMError as e:
        logger.warning("score_pacing first attempt failed, retrying: %s", e)
        # 二次重试：换 temperature
        resp = await caller.call_json(prompt, tier=ModelTier.PRIMARY, temperature=0.4, max_tokens=512)

    out = _build_score_output_strict(resp.parsed, [], require_evidence=False)
    if out.score is not None and not out.reason:
        out.reason = f"方差 {variance:.1f}，中段占均值 {mid_ratio:.0%}"
    return out


def _count_by_episode(episodes: List[Optional[int]]) -> dict[int, int]:
    out: dict[int, int] = {}
    for ep in episodes:
        if ep is None:
            continue
        out[ep] = out.get(ep, 0) + 1
    return out


# ============================================================
# 共用：调 LLM + evidence 重试（rubric §6）
# ============================================================


async def _call_with_evidence_retry(
    *,
    caller: LlmCaller,
    base_prompt: str,
    scenes: List[Scene],
    log_tag: str,
) -> ScoreOutput:
    """rubric §6 重试链：
    1. LLM 一次（temperature=0.1）
    2. 若 LLM 抛错 → 二次重试（temperature=0.4），仍失败 → raise ScoreLLMError
    3. 若返回 evidence_scene_nos 为空或全部非法 → 用强化 prompt 重试一次
    4. 二次仍空 → score=None / level=None / reason="证据不足"
    """
    # 第 1 次
    try:
        resp = await caller.call_json(
            base_prompt, tier=ModelTier.PRIMARY, temperature=0.1, max_tokens=512
        )
    except ScoreLLMError as e:
        logger.warning("%s LLM first attempt failed, retrying: %s", log_tag, e)
        # 第 2 次（升 temperature 防 transient 失败）
        resp = await caller.call_json(
            base_prompt, tier=ModelTier.PRIMARY, temperature=0.4, max_tokens=512
        )

    out = _build_score_output_strict(resp.parsed, scenes, require_evidence=True)
    if out.evidence_ref_ids:
        return out

    # evidence 缺失 → 重试一次
    logger.info("%s evidence 缺失，启动证据补强重试", log_tag)
    retry_prompt = base_prompt + _RETRY_HINT
    try:
        resp2 = await caller.call_json(
            retry_prompt, tier=ModelTier.PRIMARY, temperature=0.3, max_tokens=512
        )
    except ScoreLLMError as e:
        logger.warning("%s evidence retry LLM failed: %s", log_tag, e)
        return _INSUFFICIENT

    out2 = _build_score_output_strict(resp2.parsed, scenes, require_evidence=True)
    if out2.evidence_ref_ids:
        return out2

    logger.warning("%s evidence 二次仍空，按 rubric §6 标记证据不足", log_tag)
    return _INSUFFICIENT


def _build_score_output_strict(
    parsed,
    scenes: List[Scene],
    *,
    require_evidence: bool,
) -> ScoreOutput:
    """严格解析 LLM 输出：违反契约的字段直接抛 ScoreLLMError，不做静默 fallback。

    require_evidence=True：evidence_scene_nos 必须 ≥1 条且全部出自 scenes 集合，
    否则返回 evidence_ref_ids=[]（让上层 _call_with_evidence_retry 触发重试）。
    """
    if not isinstance(parsed, dict):
        raise ScoreLLMError(f"LLM 输出非 JSON 对象：{type(parsed).__name__}")

    raw_score = parsed.get("score")
    if raw_score is None:
        raise ScoreLLMError("LLM 输出缺 score 字段")
    try:
        score = int(raw_score)
    except (ValueError, TypeError) as e:
        raise ScoreLLMError(f"LLM score 非整数：{raw_score!r}") from e
    if score < 0 or score > 10:
        raise ScoreLLMError(f"LLM score 越界（0-10）：{score}")

    level_raw = str(parsed.get("level") or "").strip().lower()
    expected_level = _level_from_score(score)
    if level_raw not in ("high", "medium", "low"):
        # rubric §4.1 硬约束：score 与 level 必须落到同一档；如果 LLM 漏写 level，按 score 推断
        logger.info("LLM 输出 level=%r 非法，按 score=%d 推断为 %s", level_raw, score, expected_level)
        level = expected_level
    elif level_raw != expected_level:
        # 档位不一致 → 强制对齐 score（rubric §4.1 第 3 条）
        logger.info(
            "LLM score=%d 与 level=%s 档位不一致（应为 %s），按 score 推断",
            score, level_raw, expected_level,
        )
        level = expected_level
    else:
        level = level_raw

    reason = str(parsed.get("reason") or "").strip()[:200]
    if not reason:
        raise ScoreLLMError("LLM 输出 reason 为空（rubric §4.1 不允许）")

    evidence_ref_ids: List[str] = []
    if require_evidence or scenes:
        scene_no_to_id = {sc.scene_no: sc.id for sc in scenes if sc.id and sc.scene_no}
        raw_evidence = parsed.get("evidence_scene_nos", [])
        if isinstance(raw_evidence, list):
            for sno in raw_evidence:
                sid = scene_no_to_id.get(str(sno).strip())
                if sid and sid not in evidence_ref_ids:
                    evidence_ref_ids.append(sid)
                elif sno:
                    logger.info("LLM 给的 evidence_scene_no=%r 不在输入场号集，丢弃", sno)

    return ScoreOutput(
        score=score,
        level=level,
        reason=reason,
        evidence_ref_ids=evidence_ref_ids,
    )


def _level_from_score(score: int) -> str:
    if score >= 6:
        return "high"
    if score >= 3:
        return "medium"
    return "low"
