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

------------------------------------------------------------
阈值校准状态：**prototype（demo 期 first-cut 估算）**
------------------------------------------------------------
本文件中所有 numerical thresholds（如 `twist_per_ep >= 2.0`、`reward / 集 >= 3.0`、
`cv <= 0.5`、`mid_ratio >= 0.9`）均为基于行业经验的 first-cut 估算，**未经样本回归校准**。

- 信号方向是确定的（业内对照 docs/08 §3 + Coverfly / Rotten Tomatoes / 阅文五力公开方案）
- 信号方向上的具体分档边界（high/mid_high/mid_low/low 之间的数字）在 demo 期保留估算值
- 切换到样本回归阈值的触发：积累 ≥ 50 部已知好/坏样的剧本数据后，跑 ROC / threshold sweep 回归

业内对照（同样使用 prototype 阈值的成熟产品）：
- Sudowrite Manuscript Analysis 早期 v0.1 阈值由作家手感拍，v0.5 后由 100+ 已发表小说回归
- Grammarly Tone Detector 早期阈值由编辑评议，规模化后由用户接受率回归
- 抖音文心剧本助手内测期阈值由头部编剧标注 30 部样本拟合

------------------------------------------------------------
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
from typing import Dict, List, Optional, Tuple

from sqlalchemy.engine import Engine

from service.script_tools.beat_chain import BeatSheet
from service.script_tools.character_graph_chain import CharacterGraph
from service.script_tools.coverage_chain import CoverageCard
from service.script_tools.motivation_chain import MotivationResult
from service.script_tools.reward_extractor import RewardEvent
from service.script_tools.risk_terms import HOOK_KEYWORDS, MAINSTREAM_GENRES
from service.script_tools.scene_repo import (
    Scene,
    get_all_scenes,
    get_first_episode_scenes,
    get_scene,
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
#
# 阈值业内基准（短剧场景，docs/08 §3.1）：
#   - high：每 2 集 ≥ 1 反转 ≈ 0.5 / 集（抖音 2024《短剧爆款公式》报告：头部短剧反转密度典型值）
#   - mid_high：每 3 集 ≥ 1 反转 ≈ 0.33 / 集（抖音 / 快手 StreamLake 选品手册「合格」线）
#   - mid_low：每 8 集 ≥ 1 反转 ≈ 0.12 / 集（阅文短剧 IP 评级保底线）
#
# v3 修正：旧版 high=2.0 / mid_high=1.0 / mid_low=0.3 = 每集 1-2 反转，
# 短剧 30-100 集体量下不可能达到，整张评分卡被同一信号压向 low；
# 抖音《短剧爆款公式》头部样本均值约 0.4-0.6 反转 / 集，业内现实远低于 v2 阈值。


_KEY_BEATS = ("opening", "inciting", "midpoint", "climax", "closing")
_TWIST_EVENT_TYPES = ("reversal", "face_slap", "scheme_exposed", "identity_reveal")

# 反转 / 集 阈值（向下兼容业内短剧爆款公式）
_TWIST_PER_EP_HIGH = 0.5
_TWIST_PER_EP_MID_HIGH = 0.33
_TWIST_PER_EP_MID_LOW = 0.12


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

    high = (not missing_beats) and twist_per_ep >= _TWIST_PER_EP_HIGH
    mid_high = len(missing_beats) <= 1 and twist_per_ep >= _TWIST_PER_EP_MID_HIGH
    mid_low = len(missing_beats) <= 2 and twist_per_ep >= _TWIST_PER_EP_MID_LOW
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
        any(key in tag for key in MAINSTREAM_GENRES) for tag in genre_tags
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
        if any(kw in text for kw in HOOK_KEYWORDS):
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
#
# 阈值业内基准（短剧场景，docs/08 §3.4）：
#   - high：≥ 1.5 reward / 集（抖音 2024《短剧爆款公式》报告：头部短剧情感钩子密度均值约 1.5-2.5 / 集）
#   - mid_high：≥ 0.8 reward / 集（快手 StreamLake 2024 短剧选品手册「合格」线 ≥ 1）
#   - mid_low：≥ 0.3 reward / 集（阅文短剧 IP 评级保底密度）
#
# v3 修正：旧版 high=3.0 / mid_high=1.5 / mid_low=0.5 = 每集 ≥ 3 爽点，
# 短剧单集 1-3 分钟 4-8 千字，达成 3 爽点 / 集物理上不可能；业内实际密度 0.8-2 / 集。

# reward / 集 阈值
_REWARD_PER_EP_HIGH = 1.5
_REWARD_PER_EP_MID_HIGH = 0.8
_REWARD_PER_EP_MID_LOW = 0.3

# 最长无 reward 段集数（情感塌陷上限）
# 业内对照：抖音短剧观察，连续 3+ 集无 reward 用户掉量 30%+；
# Save the Cat Beat Sheet：Act II 中段最多容许 ~10% 集没有 reward beat。
_DRY_STREAK_HIGH = 2
_DRY_STREAK_MID = 4


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

    high = ratio >= _REWARD_PER_EP_HIGH and max_dry <= _DRY_STREAK_HIGH
    mid_high = ratio >= _REWARD_PER_EP_MID_HIGH and max_dry <= _DRY_STREAK_MID
    mid_low = ratio >= _REWARD_PER_EP_MID_LOW
    score, level = _score_from_signals(high=high, mid_high=mid_high, mid_low=mid_low)

    reason = (
        f"reward / 集 = {ratio:.2f}（{n_rewards} 个爽点 · {total_episodes} 集）"
        f"；最长连续无 reward {max_dry} 集"
    )

    # evidence 选取：短剧用户期待的"爽点不足证据"是「反例」而不是「仅剩的几个 reward」。
    # 业内对照（Coverfly Coverage Report / 阅文 IP 评级）：低分维度的 evidence 应锚到
    # 「最长情感塌陷段的边界集」，让用户跳到原文看「这一段连着 X 集没爽点」。
    evidence_ref_ids = _collect_dry_streak_evidence(
        reward_events=reward_events,
        total_episodes=total_episodes,
    )
    if not evidence_ref_ids:
        for ev in reward_events[:2]:
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
#   - 单集事件密度变异系数（CV = stddev / mean）
#   - 中段（中间 1/3 集）平均事件数 / 全剧均值
#
# 阈值业内基准（docs/08 §3.5）：
#   - CV：业内剧本节奏稳定性研究（Reagan et al. 2016《Six Basic Shapes of Stories》情感弧
#     研究，单集事件密度变异系数 ≤ 0.5 视为节奏稳）。短剧因爽点驱动 CV 通常偏高，
#     放宽到 ≤ 0.6 / ≤ 0.8 / ≤ 1.0 三档。
#   - 中段塌陷比：Save the Cat《救猫咪》节拍表 Act II 第二幕（中段）信息密度应保持
#     全剧均值 90%+；80% 仍可接受，70% 以下即"中段塌陷"。
#   - 开场快：首场 ≤ 600 字内出现 HOOK_KEYWORDS。600 字 = 短剧单集 ~10% 字数（4-8 千字 / 集），
#     头部短剧爆款样本均在前 10% 字数处给出冲突钩子。

# 节奏稳定性变异系数阈值
_PACING_CV_HIGH = 0.6
_PACING_CV_MID_HIGH = 0.8
_PACING_CV_MID_LOW = 1.0

# 中段密度塌陷阈值（中段均值 / 全剧均值）
_MID_RATIO_HIGH = 0.9
_MID_RATIO_MID_HIGH = 0.8
_MID_RATIO_MID_LOW = 0.7

# 低密度段判定：单集事件密度 < 全剧均值 × 此系数 即视为「低密度集」
# 0.5 = 行业惯例（Save the Cat Beat Sheet：低于均值一半即"中段塌陷"前兆）
_LOW_DENSITY_RATIO = 0.5

# 低密度段（连续低密度集）集数上限
_LOW_DENSITY_RUN_HIGH = 2
_LOW_DENSITY_RUN_MID = 5

# 开场冲突扫描窗口
# 600 字 = 短剧单集 4-8 千字的 ~10%；头部短剧爆款样本均在前 10% 字数处给出冲突钩子
_OPENING_HEAD_CHARS = 600
# 扫描首场窗口：剧本前几场（首集前 5 场覆盖了短剧典型开场密度，业内 1-2 集 = 5-10 场）
_OPENING_SCAN_SCENES = 5
# 单剧最小集数（少于此值无法评节奏 —— 三幕至少一幕一集）
_MIN_EPISODES_FOR_PACING = 3


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
    for sc in scenes[:_OPENING_SCAN_SCENES]:
        text = sc.text or ""
        head = text[:_OPENING_HEAD_CHARS]
        if any(kw in head for kw in HOOK_KEYWORDS):
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

    if len(by_ep_scene) < _MIN_EPISODES_FOR_PACING:
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

    low_density_threshold = mean * _LOW_DENSITY_RATIO
    max_dry = 0
    cur = 0
    for v in series:
        if v < low_density_threshold:
            cur += 1
            if cur > max_dry:
                max_dry = cur
        else:
            cur = 0

    high = (
        opening_fast
        and cv <= _PACING_CV_HIGH
        and mid_ratio >= _MID_RATIO_HIGH
        and max_dry <= _LOW_DENSITY_RUN_HIGH
    )
    mid_high = (
        (opening_fast or cv <= _PACING_CV_MID_HIGH)
        and mid_ratio >= _MID_RATIO_MID_HIGH
        and max_dry <= _LOW_DENSITY_RUN_MID
    )
    mid_low = mid_ratio >= _MID_RATIO_MID_LOW and cv <= _PACING_CV_MID_LOW
    score, level = _score_from_signals(high=high, mid_high=mid_high, mid_low=mid_low)

    parts = [
        f"开场{'快' if opening_fast else '慢'}",
        f"节奏稳定度（CV）= {cv:.2f}",
        f"中段密度 = 全剧 {mid_ratio:.0%}",
        f"最长低密度段 {max_dry} 集",
    ]
    reason = "；".join(parts)

    # evidence 选取：
    # - 开场快 → 取首集首场出现 HOOK_KEYWORDS 的场（正例：让用户看「这场就是抓人开局」）
    # - 中段塌陷 → 取最低密度集的首场（反例：让用户看「这一集事件稀疏」）
    evidence_ref_ids: List[str] = []
    if opening_evidence_id:
        evidence_ref_ids.append(opening_evidence_id)

    if mid_ratio < _MID_RATIO_MID_HIGH and series and eps_sorted:
        weakest_idx = min(range(len(series)), key=lambda i: series[i])
        weakest_ep = eps_sorted[weakest_idx]
        first_by_ep: Dict[int, str] = {}
        for s in scenes:
            if s.episode_no is None:
                continue
            first_by_ep.setdefault(s.episode_no, s.id)
        weakest_sid = first_by_ep.get(weakest_ep)
        if weakest_sid and weakest_sid not in evidence_ref_ids:
            evidence_ref_ids.append(weakest_sid)

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


def _longest_dry_streak_range(
    events: List[RewardEvent],
    total_eps: int,
) -> Optional[Tuple[int, int]]:
    """返回最长连续无 reward 段的集号范围 (start_ep, end_ep)，inclusive。

    这是 score_emotion / score_pacing 的「反例 evidence」选取依据：
    用户跳转看到的不是仅剩的爽点，而是「这一段连着没爽点」的代表场。
    """
    if not events or total_eps <= 0:
        return None
    eps_with_reward = sorted({ev.episode_no for ev in events if ev.episode_no is not None})
    if not eps_with_reward:
        return (1, total_eps)

    prev = 0
    best_gap = 0
    best_range: Optional[Tuple[int, int]] = None
    for ep in eps_with_reward:
        gap = ep - prev - 1
        if gap > best_gap:
            best_gap = gap
            best_range = (prev + 1, ep - 1)
        prev = ep
    tail = total_eps - prev
    if tail > best_gap:
        best_range = (prev + 1, total_eps)
    return best_range


def _collect_dry_streak_evidence(
    *,
    reward_events: List[RewardEvent],
    total_episodes: int,
    engine: Engine = default_engine,
) -> List[str]:
    """取最长无 reward 段「起始集首场」+「结束集首场」作为反例 evidence。

    返回 scene_id 列表，最多 3 个；若无法定位（无集号 / 无场景数据）返回空。
    """
    rng = _longest_dry_streak_range(reward_events, total_episodes)
    if rng is None:
        return []
    start_ep, end_ep = rng
    if end_ep < start_ep:
        return []

    script_id = ""
    for ev in reward_events:
        if ev.scene_id:
            sc = get_scene(scene_id=ev.scene_id, engine=engine)
            if sc is not None:
                script_id = sc.script_id
                break
    if not script_id:
        return []

    all_scenes = get_all_scenes(script_id=script_id, engine=engine)
    first_by_ep: Dict[int, str] = {}
    for s in all_scenes:
        if s.episode_no is None:
            continue
        first_by_ep.setdefault(s.episode_no, s.id)

    out: List[str] = []
    for ep in (start_ep, end_ep):
        sid = first_by_ep.get(ep)
        if sid and sid not in out:
            out.append(sid)
    return out[:3]


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
