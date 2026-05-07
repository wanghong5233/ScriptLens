"""阅文五力评分（v2，docs/08-evaluation-framework.md §3）。

5 维：story / character / concept / emotion / pacing。
risk 不在本模块；合规审核走 risk_screener.py，落到 ReportPayload.compliance（与 scorecard 平级）。

设计：
- 每维以**规则评分**为骨架（信号来自上游 chain：beat_sheet / character_graph / coverage_card / motivation_chain
  决策回扫 / reward_events 统计），不再让 LLM 自己决定档位
- evidence_ref_ids 来自规则锚定的具体场景（beat anchor / 决策回扫 / reward 命中 / coverage anchor），
  不需要 LLM 二次重试给证据
- LLM 仍参与的是上游 chain（如 character_graph、motivation_chain.score_motivation），
  不再在本模块单独发评分 prompt

为什么不让 LLM 评档：
- v1 让 LLM 同时打分 + 写理由 + 给证据，三件事互相打架（LLM 给不出证据时 score 也变 None）
- 短剧评分维度的判据本身可量化（节拍完整性 / 反转密度 / OOC 计数 / 题材关键词），让 LLM 决定档
  位是把可解释性让给幻觉
- 评分稳定性：规则评分 100% 复现，CI 可断言；LLM 评分跑一次一个分数

依赖：
- score_story 需要 BeatSheet + reward_events + total_episodes
- score_character 需要 MotivationResult + CharacterGraph
- score_concept 需要 CoverageCard + 全剧前 N 场（题材关键词扫描）
- score_emotion 需要 reward_events + total_episodes
- score_pacing 需要 全剧 scenes + reward_events
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from sqlalchemy.engine import Engine

from service.script_tools.beat_chain import BeatSheet
from service.script_tools.character_graph_chain import CharacterGraph
from service.script_tools.coverage_chain import CoverageCard
from service.script_tools.motivation_chain import MotivationResult
from service.script_tools.reward_extractor import RewardEvent
from service.script_tools.scene_repo import (
    Scene,
    get_all_scenes,
    get_first_episode_scenes,
)
from utils.database import engine as default_engine

logger = logging.getLogger(__name__)


@dataclass
class ScoreOutput:
    """统一评分输出。score / level 在「证据不足」时为 None。"""

    score: Optional[int]
    level: Optional[str]  # high | medium | low
    reason: str
    evidence_ref_ids: List[str] = field(default_factory=list)


_INSUFFICIENT = ScoreOutput(score=None, level=None, reason="证据不足", evidence_ref_ids=[])


# ============================================================
# 共用：分档辅助
# ============================================================


def _level_from_score(score: int) -> str:
    if score >= 6:
        return "high"
    if score >= 3:
        return "medium"
    return "low"


def _score_from_signals(*, high: bool, mid_high: bool, mid_low: bool) -> Tuple[int, str]:
    """4 档锚点的统一映射。每档取该档中位作为分数，避免 9 / 6 这种边界点重复出现。"""
    if high:
        return 9, "high"
    if mid_high:
        return 7, "high"
    if mid_low:
        return 4, "medium"
    return 2, "low"


# ============================================================
# 1. score_story —— 故事力
# ============================================================
#
# rubric §3.1：核心主线清晰度 + 情节推进密度 + 反转密度
# 信号：
#   - 关键节拍完整性（opening / inciting / midpoint / climax / closing 五个）
#   - 反转事件密度（reward_events 中 reversal / face_slap / scheme_exposed / identity_reveal 计数）


_KEY_BEATS = ("opening", "inciting", "midpoint", "climax", "closing")
_TWIST_EVENT_TYPES = ("reversal", "face_slap", "scheme_exposed", "identity_reveal")


def score_story(
    *,
    beat_sheet: Optional[BeatSheet],
    reward_events: List[RewardEvent],
    total_episodes: int,
) -> ScoreOutput:
    if total_episodes <= 0:
        total_episodes = max(1, len({ev.episode_no for ev in reward_events if ev.episode_no}))

    present_beats: set[str] = set()
    anchor_by_type: dict[str, str] = {}
    if beat_sheet is not None:
        for act in beat_sheet.acts:
            for beat in act.beats:
                if beat.type in _KEY_BEATS and beat.anchor_scene_id:
                    present_beats.add(beat.type)
                    anchor_by_type.setdefault(beat.type, beat.anchor_scene_id)

    twist_count = sum(1 for ev in reward_events if ev.event_type in _TWIST_EVENT_TYPES)
    twist_per_ep = twist_count / total_episodes
    missing_beats = [b for b in _KEY_BEATS if b not in present_beats]

    if not present_beats and twist_count == 0:
        return ScoreOutput(
            score=None,
            level=None,
            reason="无三幕节拍且未识别到反转事件，故事力维度证据不足",
            evidence_ref_ids=[],
        )

    high = (not missing_beats) and twist_per_ep >= 2.0
    mid_high = len(missing_beats) <= 1 and twist_per_ep >= 1.0
    mid_low = len(missing_beats) <= 2 and twist_per_ep >= 0.3
    score, level = _score_from_signals(high=high, mid_high=mid_high, mid_low=mid_low)

    if missing_beats:
        beat_text = f"缺关键节拍：{ '/'.join(missing_beats) }"
    else:
        beat_text = "三幕关键节拍齐全"
    reason = f"{beat_text}；反转 / 集 = {twist_per_ep:.1f}（共 {twist_count} 处反转）"

    evidence_ref_ids: List[str] = []
    for beat_type in ("climax", "midpoint", "inciting", "closing"):
        sid = anchor_by_type.get(beat_type)
        if sid and sid not in evidence_ref_ids:
            evidence_ref_ids.append(sid)
        if len(evidence_ref_ids) >= 3:
            break

    return ScoreOutput(score=score, level=level, reason=reason, evidence_ref_ids=evidence_ref_ids)


# ============================================================
# 2. score_character —— 人物力
# ============================================================
#
# rubric §3.2：主角辨识度 + 动机弧光 + 关键关系冲突
# 信号：
#   - 关键决策铺垫充足度（motivation_chain 决策回扫：setup>=2 占比 / OOC 计数）
#   - 主角动机文本是否填充（character_graph protagonist 节点的 motivation 字段）
#   - 强关系数（weight >= 0.3 的 character_graph 边数）+ 是否含 1 条 negative 主对手


def score_character(
    *,
    motivation_result: Optional[MotivationResult],
    character_graph: Optional[CharacterGraph],
) -> ScoreOutput:
    if motivation_result is None and character_graph is None:
        return ScoreOutput(
            score=None,
            level=None,
            reason="动机回扫与人物图均缺失，人物力维度证据不足",
            evidence_ref_ids=[],
        )

    judged = list(getattr(motivation_result, "judged_decisions", None) or [])
    n_decisions = len(judged)
    setup2 = sum(1 for j in judged if j.setup_count >= 2)
    setup1plus = sum(1 for j in judged if j.setup_count >= 1)
    no_setup = sum(1 for j in judged if j.setup_count == 0)
    ooc = sum(1 for j in judged if j.is_ooc)
    setup2_ratio = setup2 / n_decisions if n_decisions else 0.0
    setup1_ratio = setup1plus / n_decisions if n_decisions else 0.0

    protagonist_motivation = ""
    antagonist_first_scene: Optional[str] = None
    strong_edges = 0
    has_negative_opponent = False
    if character_graph is not None:
        for node in character_graph.nodes:
            if node.role == "protagonist" and node.motivation.strip():
                protagonist_motivation = node.motivation
            if node.role == "antagonist" and antagonist_first_scene is None:
                antagonist_first_scene = node.first_scene_id
        for edge in character_graph.edges:
            if edge.weight >= 0.3:
                strong_edges += 1
                if edge.polarity == "negative":
                    has_negative_opponent = True

    high = (
        bool(protagonist_motivation)
        and setup2_ratio >= 0.8
        and ooc == 0
        and strong_edges >= 3
        and has_negative_opponent
    )
    mid_high = (
        bool(protagonist_motivation)
        and setup1_ratio >= 0.6
        and ooc <= 2
        and strong_edges >= 2
    )
    mid_low = (
        (no_setup / n_decisions if n_decisions else 1.0) <= 0.3
        and ooc <= 5
        and strong_edges <= 1
    ) or (n_decisions == 0 and strong_edges >= 2)
    score, level = _score_from_signals(high=high, mid_high=mid_high, mid_low=mid_low)

    parts: List[str] = []
    if n_decisions:
        parts.append(
            f"评估 {n_decisions} 个关键决策："
            f"{setup2} 个铺垫充足 / {no_setup} 个无铺垫 / {ooc} 个 OOC"
        )
    if protagonist_motivation:
        parts.append(f"主角动机已锚定：{protagonist_motivation[:24]}")
    elif character_graph is not None:
        parts.append("主角动机字段为空")
    if character_graph is not None:
        parts.append(
            f"强关系 {strong_edges} 条 · {'有' if has_negative_opponent else '缺'}主对手"
        )
    reason = "；".join(parts) or "人物维度仅有弱信号"

    evidence_ref_ids: List[str] = []
    for sid in getattr(motivation_result, "evidence_ref_ids", None) or []:
        if sid and sid not in evidence_ref_ids:
            evidence_ref_ids.append(sid)
    if antagonist_first_scene and antagonist_first_scene not in evidence_ref_ids:
        evidence_ref_ids.append(antagonist_first_scene)
    evidence_ref_ids = evidence_ref_ids[:5]

    return ScoreOutput(score=score, level=level, reason=reason, evidence_ref_ids=evidence_ref_ids)


# ============================================================
# 3. score_concept —— 题材力
# ============================================================
#
# rubric §3.3：赛道辨识度 + 卖点钩子 + 商业可行性
# 信号：
#   - genre 标签是否落到主流赛道
#   - core_value 是否非空（≤30 字差异化卖点）
#   - 首集前 3 场是否出现题材标识事件（关键词扫描）


_MAINSTREAM_GENRES = {
    "重生", "穿越", "复仇", "战神", "豪门", "甜宠", "逆袭", "战神归来",
    "都市重生", "总裁", "替身", "弃妇", "扮猪吃虎", "马甲", "认亲",
    "古言", "现言", "玄幻", "悬疑", "权谋",
}

_CONCEPT_KEYWORDS = (
    "死亡", "绝症", "离婚", "出轨", "重生", "穿越", "复仇", "退婚", "分手",
    "当众", "羞辱", "阴谋", "真相", "误会", "反目", "重逢", "追妻", "认亲",
)


def score_concept(
    *,
    coverage_card: Optional[CoverageCard],
    script_id: str,
    engine: Engine = default_engine,
    n_episodes_to_scan: int = 1,
    max_scenes_to_scan: int = 3,
) -> ScoreOutput:
    if coverage_card is None:
        return ScoreOutput(
            score=None,
            level=None,
            reason="速览卡未生成，题材力维度证据不足",
            evidence_ref_ids=[],
        )

    genre_tags = [g.strip() for g in (coverage_card.genre or []) if g and g.strip()]
    has_mainstream = any(
        any(key in tag for key in _MAINSTREAM_GENRES) for tag in genre_tags
    )
    has_core_value = bool((coverage_card.core_value or "").strip())

    head_scenes = get_first_episode_scenes(
        script_id=script_id,
        n_episodes=n_episodes_to_scan,
        engine=engine,
    )[:max_scenes_to_scan]

    keyword_hit_scene_id: Optional[str] = None
    for sc in head_scenes:
        text = sc.text or ""
        if any(kw in text for kw in _CONCEPT_KEYWORDS):
            keyword_hit_scene_id = sc.id
            break
    early_keyword_hit = keyword_hit_scene_id is not None

    high = has_mainstream and has_core_value and early_keyword_hit
    mid_high = has_mainstream and (has_core_value or early_keyword_hit)
    mid_low = bool(genre_tags) and not has_mainstream
    score, level = _score_from_signals(high=high, mid_high=mid_high, mid_low=mid_low)

    parts: List[str] = []
    if genre_tags:
        parts.append(f"题材：{', '.join(genre_tags[:3])}")
    else:
        parts.append("无题材标签")
    if has_core_value:
        parts.append(f"核心卖点：{coverage_card.core_value[:24]}")
    else:
        parts.append("核心卖点缺失")
    if early_keyword_hit:
        parts.append("首集 3 场内出现题材标识事件")
    else:
        parts.append("首集 3 场内无题材标识事件")
    reason = "；".join(parts)

    evidence_ref_ids: List[str] = []
    if keyword_hit_scene_id:
        evidence_ref_ids.append(keyword_hit_scene_id)
    for point in (coverage_card.strengths or [])[:2]:
        sid = point.anchor_scene_id
        if sid and sid not in evidence_ref_ids:
            evidence_ref_ids.append(sid)

    return ScoreOutput(score=score, level=level, reason=reason, evidence_ref_ids=evidence_ref_ids)


# ============================================================
# 4. score_emotion —— 情感力
# ============================================================
#
# rubric §3.4：情绪密度 + 爽点频率 + 共情触达（沿用 v1 reward_density 算法 + 改名）
# 信号：
#   - reward 事件 / 集数比值
#   - 最长连续无 reward 集数（情感塌陷段）


def score_emotion(
    *,
    reward_events: List[RewardEvent],
    total_episodes: int,
) -> ScoreOutput:
    if total_episodes <= 0:
        total_episodes = max(1, len(reward_events) // 2)

    n_rewards = len(reward_events)
    if n_rewards == 0:
        return ScoreOutput(
            score=1,
            level="low",
            reason=f"全剧未识别到 reward 事件（{total_episodes} 集），情感力极低",
            evidence_ref_ids=[],
        )

    ratio = n_rewards / total_episodes
    max_dry = _max_dry_streak(reward_events, total_episodes)

    high = ratio >= 3.0 and max_dry <= 2
    mid_high = ratio >= 1.5 and max_dry <= 4
    mid_low = ratio >= 0.5
    score, level = _score_from_signals(high=high, mid_high=mid_high, mid_low=mid_low)

    reason = (
        f"reward / 集 = {ratio:.1f}（{n_rewards} 个爽点 · {total_episodes} 集）"
        f"；最长连续无 reward {max_dry} 集"
    )

    evidence_ref_ids: List[str] = []
    for ev in reward_events[:3]:
        if ev.scene_id and ev.scene_id not in evidence_ref_ids:
            evidence_ref_ids.append(ev.scene_id)

    return ScoreOutput(score=score, level=level, reason=reason, evidence_ref_ids=evidence_ref_ids)


# ============================================================
# 5. score_pacing —— 叙事力
# ============================================================
#
# rubric §3.5：开场抓人速度 + 节奏方差 + 信息密度（v1 opening_hook 折叠进本维度）
# 信号：
#   - 首场 20 段内是否出现冲突事件（开场速度）
#   - 单集事件密度方差
#   - 中段（中间 1/3 集）平均事件数 / 全剧均值


_OPENING_CONFLICT_KEYWORDS = (
    "死", "绝症", "离婚", "出轨", "重生", "穿越", "复仇", "退婚", "分手",
    "当众", "羞辱", "阴谋", "真相", "反目", "打", "推倒", "巴掌",
)


def score_pacing(
    *,
    script_id: str,
    reward_events: List[RewardEvent],
    engine: Engine = default_engine,
) -> ScoreOutput:
    scenes = get_all_scenes(script_id=script_id, engine=engine)
    if not scenes:
        return ScoreOutput(
            score=None,
            level=None,
            reason="无场景数据可评（剧本切分可能异常）",
            evidence_ref_ids=[],
        )

    opening_fast = False
    opening_evidence_id: Optional[str] = None
    for sc in scenes[:5]:
        text = sc.text or ""
        head = text[:600]
        if any(kw in head for kw in _OPENING_CONFLICT_KEYWORDS):
            opening_fast = True
            opening_evidence_id = sc.id
            break

    by_ep_scene = _count_by_episode([s.episode_no for s in scenes])
    by_ep_reward = _count_by_episode([ev.episode_no for ev in reward_events])

    if not by_ep_scene:
        return ScoreOutput(
            score=None,
            level=None,
            reason="剧本无集号信息（fallback 切分），节奏维度不可评",
            evidence_ref_ids=[],
        )

    if len(by_ep_scene) < 3:
        return ScoreOutput(
            score=None,
            level=None,
            reason=f"剧本仅 {len(by_ep_scene)} 集，集数过少无法评节奏",
            evidence_ref_ids=[],
        )

    eps_sorted = sorted(by_ep_scene.keys())
    series = [by_ep_scene[e] + by_ep_reward.get(e, 0) for e in eps_sorted]
    n_eps = len(series)
    mean = statistics.fmean(series) if series else 0.0
    variance = statistics.pvariance(series) if len(series) > 1 else 0.0
    cv = (variance ** 0.5) / mean if mean > 0 else 0.0

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

    high = opening_fast and cv <= 0.5 and mid_ratio >= 0.9
    mid_high = (opening_fast or cv <= 0.6) and mid_ratio >= 0.8 and max_dry <= 3
    mid_low = mid_ratio >= 0.7 and max_dry <= 5
    score, level = _score_from_signals(high=high, mid_high=mid_high, mid_low=mid_low)

    parts = [
        f"开场{'快' if opening_fast else '慢'}",
        f"方差 {variance:.1f}（CV={cv:.2f}）",
        f"中段占均值 {mid_ratio:.0%}",
        f"最长低密度段 {max_dry} 集",
    ]
    reason = "；".join(parts)

    evidence_ref_ids: List[str] = []
    if opening_evidence_id:
        evidence_ref_ids.append(opening_evidence_id)

    return ScoreOutput(score=score, level=level, reason=reason, evidence_ref_ids=evidence_ref_ids)


# ============================================================
# 工具函数
# ============================================================


def _count_by_episode(episodes: List[Optional[int]]) -> dict[int, int]:
    out: dict[int, int] = {}
    for ep in episodes:
        if ep is None:
            continue
        out[ep] = out.get(ep, 0) + 1
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
    """伪 Scene 对象列表（仅用于历史 evidence 反查）。本模块当前不再用，
    保留是为了 `script_report_service.score_one_dimension` 这类老入口的兼容。"""
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
